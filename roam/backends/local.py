"""Local directory backend: HEAD is one file, blobs are immutable files."""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path

from ..errors import BackendError, BlobExists, ConfigError, HeadConflict
from ..model import HEAD_KEY
from . import validate_key

_COPY_CHUNK = 1024 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    # Windows cannot open a directory for fsync (PermissionError); NTFS
    # journals the rename itself, so there is nothing to flush there.
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _temp_sibling(path: Path) -> Path:
    return path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")


class LocalBackend:
    """Filesystem store for tests and single-host use.

    HEAD is the file ``HEAD`` and its version token is the sha256 of its
    bytes. Updates take an exclusive lock, compare, then write a temp file
    and ``os.replace`` it in. Blobs are placed with ``O_EXCL`` semantics, so
    an existing key raises ``BlobExists``.
    """

    name = "local"

    def __init__(self, root: Path) -> None:
        # fcntl is POSIX-only and imported lazily everywhere else, so the s3
        # and git backends keep working on Windows; only THIS backend refuses
        # to construct there, with a named error.
        try:
            import fcntl  # noqa: F401
        except ImportError as exc:
            raise ConfigError(
                "the local backend needs a POSIX system (no fcntl module); "
                "use the s3 or git backend instead"
            ) from exc
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks = self.root / ".locks"
        self._locks.mkdir(exist_ok=True)

    def __repr__(self) -> str:
        return f"LocalBackend(root={str(self.root)!r})"

    def _path(self, key: str) -> Path:
        parts = validate_key(key).split("/")
        return self.root.joinpath(*parts)

    @contextmanager
    def _head_lock(self):
        import fcntl  # lazy: POSIX-only, keeps this module importable on Windows

        lock_path = self._locks / "HEAD.lock"
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def get_head(self, *, deadline: float | None = None) -> tuple[bytes, str] | None:
        """``deadline`` (R8) is accepted for protocol symmetry and ignored:
        no operation here leaves the local filesystem."""
        head = self._path(HEAD_KEY)
        if not head.is_file():
            return None
        return head.read_bytes(), _sha256_file(head)

    def put_head(
        self, data: bytes, expected: str | None, *, deadline: float | None = None
    ) -> str:
        head = self._path(HEAD_KEY)
        with self._head_lock():
            current = _sha256_file(head) if head.is_file() else None
            if expected is None:
                if current is not None:
                    raise HeadConflict("a head already exists; pull before pushing again")
            elif current != expected:
                raise HeadConflict("the head changed since it was read; pull before pushing again")
            temp = _temp_sibling(head)
            try:
                with temp.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, head)
                _fsync_dir(head.parent)
            finally:
                temp.unlink(missing_ok=True)
        return hashlib.sha256(data).hexdigest()

    def put_blob(
        self, key: str, source: Path, *, deadline: float | None = None
    ) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        src = Path(source)
        if not src.is_file() or src.is_symlink():
            raise BackendError(f"bundle source must be a regular file: {src}")
        temp = _temp_sibling(path)
        try:
            with src.open("rb") as reader, temp.open("xb") as writer:
                while chunk := reader.read(_COPY_CHUNK):
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            try:
                os.link(temp, path)  # O_EXCL placement: fails when the key exists
            except FileExistsError:
                raise BlobExists(f"bundle already exists: {key}") from None
            except OSError:
                # Filesystems without hard links (FAT, some network mounts):
                # fall back to an O_EXCL copy of the staged temp file, which
                # keeps the create-once semantics.
                try:
                    with temp.open("rb") as reader, path.open("xb") as writer:
                        shutil.copyfileobj(reader, writer, _COPY_CHUNK)
                        writer.flush()
                        os.fsync(writer.fileno())
                except FileExistsError:
                    raise BlobExists(f"bundle already exists: {key}") from None
            temp.unlink()
            _fsync_dir(path.parent)
        finally:
            temp.unlink(missing_ok=True)

    def get_blob(
        self,
        key: str,
        dest: Path,
        *,
        max_bytes: int,
        deadline: float | None = None,
    ) -> None:
        path = self._path(key)
        if not path.is_file():
            raise BackendError(f"bundle not found: {key}")
        size = path.stat().st_size
        if size > max_bytes:
            raise BackendError(f"bundle {key} is {size} bytes, over the {max_bytes} byte limit")
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = _temp_sibling(target)
        try:
            with path.open("rb") as reader, temp.open("xb") as writer:
                written = 0
                while chunk := reader.read(_COPY_CHUNK):
                    written += len(chunk)
                    if written > max_bytes:
                        raise BackendError(f"bundle {key} grew past the {max_bytes} byte limit")
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            if written != size:
                raise BackendError(f"bundle {key} changed while it was copied")
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def blob_exists(self, key: str, *, deadline: float | None = None) -> bool:
        return self._path(key).is_file()

    def list_blobs(self, prefix: str, *, deadline: float | None = None) -> list[str]:
        """Sorted store keys under ``prefix`` (e.g. ``bundles/``).

        0.4 history/restore: the only listing surface; never follows symlinks,
        never descends more than 8 levels, and only returns keys that pass
        ``validate_key``. ``deadline`` is accepted for protocol symmetry and
        ignored (no operation leaves the local filesystem).
        """
        if not isinstance(prefix, str) or not prefix:
            raise ConfigError(f"invalid blob prefix {prefix!r}: prefixes must be non-empty strings")
        normalized = prefix.rstrip("/")
        try:
            validate_key(normalized)
        except ConfigError:
            raise ConfigError(
                f"invalid blob prefix {prefix!r}: use a relative POSIX prefix such as 'bundles/'"
            ) from None
        base = self.root.joinpath(*normalized.split("/"))
        if not base.is_dir() or base.is_symlink():
            return []
        found: list[str] = []

        def scan(directory: Path, relative: str, depth: int) -> None:
            if depth > 8:
                return
            try:
                children = sorted(os.scandir(directory), key=lambda entry: entry.name)
            except OSError:
                return
            for entry in children:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    scan(Path(entry.path), f"{relative}/{entry.name}", depth + 1)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                key = f"{relative}/{entry.name}"
                try:
                    validate_key(key)
                except ConfigError:
                    continue
                found.append(key)

        scan(base, normalized, 0)
        return sorted(found)

    def ping(self, *, deadline: float | None = None) -> str:
        if not self.root.is_dir() or not os.access(self.root, os.W_OK):
            raise BackendError(f"local backend root is not a writable directory: {self.root}")
        free = shutil.disk_usage(self.root).free
        return f"local backend ready: {self.root} ({free // (1024 * 1024)} MiB free)"
