"""Encrypted roam bundle: one AES-256-GCM envelope around a zip of home files.

Port of vigil's secure_archive.py, simplified to a single envelope: magic
b'ROAM1', a 4-byte big-endian header length, the canonical-JSON header as AAD,
a 12-byte nonce, then ciphertext and tag. Every zip member is validated before
anything is written, and all crypto streams in bounded chunks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import struct
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .errors import ArchiveError, VerifyError
from .inventory import manifest_from_entries
from .model import DEFAULT_MAX_BUNDLE_BYTES, FileEntry, canonical_json

MAGIC = b"ROAM1"
MANIFEST_MEMBER = "roam/manifest.json"
NONCE_SIZE = 12
TAG_SIZE = 16

_HEADER_LEN = struct.Struct(">I")
_CHUNK = 1024 * 1024
_MAX_HEADER_BYTES = 16 * 1024
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    """Resource limits for bundle opening; max_total_bytes caps the bundle."""

    max_members: int = 100_000
    max_member_bytes: int = DEFAULT_MAX_BUNDLE_BYTES
    max_total_bytes: int = DEFAULT_MAX_BUNDLE_BYTES
    max_path_length: int = 1024

    def __post_init__(self) -> None:
        if min(self.max_members, self.max_member_bytes, self.max_total_bytes, self.max_path_length) < 1:
            raise ArchiveError("archive limits must be positive")


@dataclass(frozen=True, slots=True)
class BundleInfo:
    path: str
    size: int
    sha256: str
    plain_sha256: str
    plain_size: int
    header: dict


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_private_exclusive(path: Path):
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    return os.fdopen(descriptor, "wb")


def _hash_file(path: Path) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _safe_member_name(raw: str, *, max_length: int) -> PurePosixPath:
    if not raw or len(raw) > max_length:
        raise ArchiveError(f"unsafe bundle member name: {raw!r}")
    if "\x00" in raw or any(ord(char) < 32 for char in raw):
        raise ArchiveError(f"bundle member name contains control characters: {raw!r}")
    if "\\" in raw:
        raise ArchiveError(f"bundle member name contains a backslash: {raw!r}")
    if raw.startswith("/") or _WINDOWS_DRIVE_RE.match(raw):
        raise ArchiveError(f"bundle member name is absolute: {raw!r}")
    if any(part in ("", ".", "..") for part in raw.split("/")):
        raise ArchiveError(f"bundle member name is unsafe: {raw!r}")
    return PurePosixPath(raw)


def _is_unsafe_file_type(info: zipfile.ZipInfo) -> bool:
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    if stat.S_IFMT(unix_mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
        return True
    dos_attributes = info.external_attr & 0xFFFF
    return bool(dos_attributes & 0x0400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _inspect_members(infos: list[zipfile.ZipInfo], limits: ArchiveLimits) -> list[PurePosixPath]:
    if len(infos) > limits.max_members:
        raise ArchiveError("bundle contains too many members")
    paths: list[PurePosixPath] = []
    seen: set[str] = set()
    total = 0
    for info in infos:
        path = _safe_member_name(info.filename, max_length=limits.max_path_length)
        if path.as_posix() in seen:
            raise ArchiveError(f"bundle contains a duplicate member: {path}")
        seen.add(path.as_posix())
        if info.flag_bits & 0x1:
            raise ArchiveError(f"bundle member is zip-encrypted: {path}")
        if info.compress_type not in _ALLOWED_COMPRESSION:
            raise ArchiveError(f"unsupported compression for bundle member: {path}")
        if _is_unsafe_file_type(info):
            raise ArchiveError(f"bundle member is a symlink or special file: {path}")
        if info.file_size < 0 or info.compress_size < 0:
            raise ArchiveError(f"bundle member has invalid sizes: {path}")
        if info.file_size > limits.max_member_bytes:
            raise ArchiveError(f"bundle member exceeds the per-file size limit: {path}")
        total += info.file_size
        if total > limits.max_total_bytes:
            raise ArchiveError("bundle exceeds the total size limit")
        paths.append(path)
    return paths


def _verify_declared_sizes(
    archive: zipfile.ZipFile, infos: list[zipfile.ZipInfo], limits: ArchiveLimits
) -> None:
    """Decompress every member without writing and confirm it matches its declared size."""
    for info in infos:
        if info.is_dir():
            continue
        written = 0
        try:
            with archive.open(info, "r") as source:
                while chunk := source.read(_CHUNK):
                    written += len(chunk)
                    if written > info.file_size or written > limits.max_member_bytes:
                        raise ArchiveError(
                            f"bundle member expands beyond its declared size: {info.filename}"
                        )
        except ArchiveError:
            raise
        except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as exc:
            raise ArchiveError(f"bundle member is unreadable: {info.filename}") from exc
        if written != info.file_size:
            raise ArchiveError(f"bundle member size does not match its declaration: {info.filename}")


def _ensure_private_dirs(root: Path, parts: tuple[str, ...]) -> None:
    cursor = root
    for part in parts:
        cursor /= part
        cursor.mkdir(mode=0o700, exist_ok=True)
        if cursor.is_symlink() or not cursor.is_dir():
            raise ArchiveError(f"unsafe directory appeared during extraction: {cursor}")
        if os.name != "nt":
            cursor.chmod(0o700)


def _extract_members(
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    root: Path,
    limits: ArchiveLimits,
) -> None:
    total = 0
    for info in infos:
        path = _safe_member_name(info.filename, max_length=limits.max_path_length)
        if info.is_dir():
            _ensure_private_dirs(root, path.parts)
            continue
        _ensure_private_dirs(root, path.parts[:-1])
        target = root.joinpath(*path.parts)
        # Real recorded permission bits, capped at 0755: a private 0600 file
        # stays private, and group/other bits are never widened beyond what
        # the bundle recorded (nothing is ORed in, only the recorded mask).
        archived_mode = ((info.external_attr >> 16) & 0o7777) or 0o600
        final_mode = min(archived_mode & 0o777, 0o755)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(target, flags, 0o600)
        written = 0
        try:
            with archive.open(info, "r") as source, os.fdopen(descriptor, "wb") as sink:
                descriptor = -1
                while chunk := source.read(_CHUNK):
                    written += len(chunk)
                    total += len(chunk)
                    if written > info.file_size or total > limits.max_total_bytes:
                        raise ArchiveError(f"bundle member expanded beyond limits: {path}")
                    sink.write(chunk)
                sink.flush()
                os.fsync(sink.fileno())
        except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as exc:
            raise ArchiveError(f"failed to extract bundle member: {path}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if written != info.file_size:
            raise ArchiveError(f"bundle member size changed during extraction: {path}")
        if os.name != "nt":
            target.chmod(final_mode)
    _fsync_directory(root)


def _seal_envelope(plain_path: Path, out_path: Path, *, key: bytes, header_bytes: bytes) -> None:
    """Encrypt plain_path into out_path as one AES-256-GCM envelope."""
    nonce = os.urandom(NONCE_SIZE)
    prefix = MAGIC + _HEADER_LEN.pack(len(header_bytes)) + header_bytes
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(prefix)
    temporary = out_path.parent / f".{out_path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with plain_path.open("rb") as source, _open_private_exclusive(temporary) as sink:
            sink.write(prefix)
            sink.write(nonce)
            while chunk := source.read(_CHUNK):
                sink.write(encryptor.update(chunk))
            sink.write(encryptor.finalize())
            sink.write(encryptor.tag)
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(temporary, out_path)
        _fsync_directory(out_path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _read_envelope(path: Path) -> tuple[bytes, dict]:
    with path.open("rb") as source:
        if source.read(len(MAGIC)) != MAGIC:
            raise ArchiveError("bundle has an invalid magic value")
        raw_length = source.read(_HEADER_LEN.size)
        if len(raw_length) != _HEADER_LEN.size:
            raise ArchiveError("bundle has a truncated header length")
        (length,) = _HEADER_LEN.unpack(raw_length)
        if length < 2 or length > _MAX_HEADER_BYTES:
            raise ArchiveError("bundle header length is invalid")
        header_bytes = source.read(length)
        if len(header_bytes) != length:
            raise ArchiveError("bundle header is truncated")
    try:
        header = json.loads(header_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveError("bundle header is not valid JSON") from exc
    if not isinstance(header, dict):
        raise ArchiveError("bundle header must be a JSON object")
    return header_bytes, header


def read_header(path: Path) -> dict:
    """Read the authenticated bundle header; no key needed."""
    return _read_envelope(Path(path))[1]


def _write_member(
    archive: zipfile.ZipFile,
    written: set[str],
    relpath: str,
    *,
    source: Path | None = None,
    payload: bytes | None = None,
    mode: int = 0o644,
) -> None:
    _safe_member_name(relpath, max_length=1024)
    if relpath in written:
        return
    written.add(relpath)
    info = zipfile.ZipInfo(relpath)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | (mode & 0o7777)) << 16
    if payload is not None:
        archive.writestr(info, payload)
        return
    if source is None:
        raise ArchiveError(f"bundle member has no source: {relpath}")
    try:
        with source.open("rb") as src, archive.open(info, "w", force_zip64=True) as sink:
            shutil.copyfileobj(src, sink, _CHUNK)
    except OSError as exc:
        raise ArchiveError(f"bundle source file is unreadable: {relpath}") from exc


def build_bundle(
    home: Path,
    entries: list[FileEntry],
    out_path: Path,
    *,
    key: bytes,
    header: dict,
    extra_files: dict[str, bytes],
) -> BundleInfo:
    """Zip the entries (extra_files override same-relpath on-disk files), then seal."""
    home = Path(home)
    out_path = Path(out_path)
    if len(key) != 32:
        raise ArchiveError("bundle key must be exactly 32 bytes")
    extra = dict(extra_files or {})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".roam-build-", dir=out_path.parent)
    os.close(descriptor)
    zip_path = Path(temp_name)
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            written: set[str] = set()
            _write_member(
                zf, written, MANIFEST_MEMBER, payload=canonical_json(manifest_from_entries(entries))
            )
            for entry in sorted(entries, key=lambda item: item.relpath):
                _write_member(
                    zf,
                    written,
                    entry.relpath,
                    source=None if entry.relpath in extra else home / entry.relpath,
                    payload=extra.get(entry.relpath),
                    mode=entry.mode,
                )
            for relpath, payload in sorted(extra.items()):
                _write_member(zf, written, relpath, payload=payload)
        plain_size, plain_sha256 = _hash_file(zip_path)
        full_header = {"v": 1, **dict(header), "plain_sha256": plain_sha256, "plain_size": plain_size}
        _seal_envelope(zip_path, out_path, key=key, header_bytes=canonical_json(full_header))
    finally:
        zip_path.unlink(missing_ok=True)
    size, sha256 = _hash_file(out_path)
    return BundleInfo(str(out_path), size, sha256, plain_sha256, plain_size, full_header)


def open_bundle(path: Path, *, key: bytes, dest_dir: Path, limits: ArchiveLimits) -> dict:
    """Verify, decrypt, validate, and extract a bundle; returns the manifest dict."""
    path = Path(path)
    dest_dir = Path(dest_dir)
    if len(key) != 32:
        raise ArchiveError("bundle key must be exactly 32 bytes")
    header_bytes, header = _read_envelope(path)
    plain_size = header.get("plain_size")
    plain_sha256 = header.get("plain_sha256")
    if not isinstance(plain_size, int) or isinstance(plain_size, bool) or plain_size < 0:
        raise ArchiveError("bundle header has an invalid plain_size")
    if not isinstance(plain_sha256, str) or not _HEX64_RE.fullmatch(plain_sha256):
        raise ArchiveError("bundle header has an invalid plain_sha256")
    if plain_size > limits.max_total_bytes:
        raise ArchiveError(
            f"bundle plaintext exceeds the size limit ({plain_size} > {limits.max_total_bytes})"
        )
    prefix_size = len(MAGIC) + _HEADER_LEN.size + len(header_bytes)
    cipher_size = path.stat().st_size - prefix_size - NONCE_SIZE - TAG_SIZE
    if cipher_size < 0 or cipher_size != plain_size:
        raise ArchiveError("bundle length does not match its header")
    with path.open("rb") as source:
        source.seek(prefix_size)
        nonce = source.read(NONCE_SIZE)
        source.seek(-TAG_SIZE, os.SEEK_END)
        tag = source.read(TAG_SIZE)
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_zip = dest_dir.parent / f".roam-open-{uuid.uuid4().hex}.zip"
    decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).decryptor()
    decryptor.authenticate_additional_data(
        MAGIC + _HEADER_LEN.pack(len(header_bytes)) + header_bytes
    )
    digest = hashlib.sha256()
    written = 0
    try:
        with path.open("rb") as source, _open_private_exclusive(temp_zip) as sink:
            source.seek(prefix_size + NONCE_SIZE)
            remaining = cipher_size
            while remaining:
                chunk = source.read(min(_CHUNK, remaining))
                if not chunk:
                    raise ArchiveError("bundle ciphertext is truncated")
                remaining -= len(chunk)
                clear = decryptor.update(chunk)
                written += len(clear)
                digest.update(clear)
                sink.write(clear)
            clear = decryptor.finalize_with_tag(tag)
            written += len(clear)
            digest.update(clear)
            sink.write(clear)
            sink.flush()
            os.fsync(sink.fileno())
    except InvalidTag as exc:
        temp_zip.unlink(missing_ok=True)
        raise VerifyError("bundle authentication failed: wrong key or tampered bundle") from exc
    except (OSError, EOFError, RuntimeError) as exc:
        temp_zip.unlink(missing_ok=True)
        raise ArchiveError("bundle could not be decrypted") from exc
    if written != plain_size or digest.hexdigest() != plain_sha256:
        temp_zip.unlink(missing_ok=True)
        raise VerifyError("decrypted bundle does not match its header digest")
    try:
        return _extract_bundle(temp_zip, dest_dir, limits)
    finally:
        temp_zip.unlink(missing_ok=True)


def _extract_bundle(zip_path: Path, dest_dir: Path, limits: ArchiveLimits) -> dict:
    try:
        archive = zipfile.ZipFile(zip_path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ArchiveError("bundle plaintext is not a valid ZIP") from exc
    with archive:
        infos = archive.infolist()
        _inspect_members(infos, limits)
        _verify_declared_sizes(archive, infos, limits)
        if dest_dir.is_symlink():
            raise ArchiveError("extraction destination cannot be a symlink")
        if dest_dir.exists():
            if not dest_dir.is_dir():
                raise ArchiveError("extraction destination is not a directory")
            if any(dest_dir.iterdir()):
                raise ArchiveError("extraction destination must be empty")
        else:
            dest_dir.mkdir(parents=True, mode=0o700)
        root = dest_dir.resolve(strict=True)
        if os.name != "nt":
            root.chmod(0o700)
        _extract_members(archive, infos, root, limits)
    manifest_path = root / PurePosixPath(MANIFEST_MEMBER)
    if not manifest_path.is_file():
        raise ArchiveError("bundle has no roam/manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveError("bundle manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise ArchiveError("bundle manifest must be a JSON object")
    return manifest
