"""Honest pipeline fake: real classification table, real zip + real AES-GCM envelope.

Mirrors the signatures Lane A1 owns (roam.inventory / roam.scope / roam.archive) so the
engine's verification paths (bundle digests, wrong-key rejection, config merge) are real.
Classification delegates to the REAL roam.scope allowlist, so the fake can never drift
from the production table.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from roam import scope as real_scope
from roam.errors import ArchiveError, VerifyError
from roam.model import FileEntry, Scope, canonical_json

MANIFEST_MEMBER = "roam/manifest.json"
MAGIC = b"ROAM1"

DEFAULT_HOST_KEYS = (
    "terminal",
    "paths",
    "display.interface",
    "browser.executable",
    "mcp_servers.*.command",
    "plugins.entries.hermes-roam",
)

_ABS_USER_PATH_RULES = ("/root/", "/home/", "/Users/")


@dataclass(frozen=True)
class FakeRules:
    """Stand-in for roam.scope.Rules: carries the merged host-key list."""

    host_keys: tuple[str, ...] = DEFAULT_HOST_KEYS


@dataclass(frozen=True)
class FakeBundleInfo:
    """Stand-in for roam.archive.BundleInfo."""

    path: Path
    size: int
    sha256: str
    plain_sha256: str
    plain_size: int
    header: dict = field(default_factory=dict)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def classify(relpath: str, rules: FakeRules) -> Scope:
    """The REAL allowlist classifier on the default rule set (the fake never
    loads a real home's rules.yaml or hub lock, and engine fixtures carry none)."""
    return real_scope.classify_path(relpath, real_scope.Rules.default())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def walk_home(home: Path, rules: FakeRules, *, include: set[Scope]) -> list[FileEntry]:
    """Walk the home, prune local/ignored dirs, skip symlinks, keep only included scopes."""
    home = Path(home)
    results: list[FileEntry] = []
    for dirpath, dirnames, filenames in os.walk(home, followlinks=False):
        rel_dir = os.path.relpath(dirpath, home).replace(os.sep, "/")
        if rel_dir == ".":
            rel_dir = ""
        keep: list[str] = []
        for name in sorted(dirnames):
            rel_d = f"{rel_dir}/{name}" if rel_dir else name
            if os.path.islink(Path(dirpath) / name):
                continue
            if rel_d.split("/")[0] == "roam":
                keep.append(name)  # file-level classification handles roam/ contents
                continue
            # Mirror the real walk: only IGNORED directories are pruned
            # (never traversed); LOCAL directories are walked and their files
            # are filtered by file-level classification, exactly like roam.inventory.
            if classify(rel_d + "/", rules) == Scope.IGNORED:
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            scope = classify(rel, rules)
            if scope not in include:
                continue
            mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
            results.append(
                FileEntry(rel, _sha256_file(path), path.stat().st_size, mode, scope)
            )
    results.sort(key=lambda entry: entry.relpath)
    return results


def generate_installed_plugins(home: Path) -> dict:
    """Read plugins/*/plugin.yaml names+versions and any pinned git sha (no git runs)."""
    plugins = Path(home) / "plugins"
    if not plugins.is_dir():
        return {}
    out: dict[str, dict] = {}
    for directory in sorted(plugins.iterdir()):
        if not directory.is_dir():
            continue
        meta = directory / "plugin.yaml"
        name, version = directory.name, ""
        if meta.is_file():
            try:
                parsed = yaml.safe_load(meta.read_text()) or {}
            except yaml.YAMLError:
                parsed = {}
            name = str(parsed.get("name") or directory.name)
            version = str(parsed.get("version") or "")
        sha = ""
        git_dir = directory / ".git"
        head_file = git_dir / "HEAD"
        if head_file.is_file():
            ref = head_file.read_text().strip()
            if ref.startswith("ref:"):
                ref_file = git_dir / ref.split(" ", 1)[1]
                if ref_file.is_file():
                    sha = ref_file.read_text().strip()
        out[name] = {"version": version, "sha": sha}
    return out


def _is_abs_user_path(value: str) -> bool:
    if value.startswith(_ABS_USER_PATH_RULES):
        return True
    if len(value) >= 3 and value[1] == ":" and value[0].isalpha() and value[2] == "\\":
        return True
    return False


def _iter_leaves(node: dict, prefix: tuple[str, ...] = ()):
    for key, value in node.items():
        path = prefix + (str(key),)
        if isinstance(value, dict) and value:
            yield from _iter_leaves(value, path)
        else:
            yield path, value


def _is_host_key(path: tuple[str, ...], host_keys) -> bool:
    for pattern in host_keys:
        parts = pattern.split(".")
        if len(path) < len(parts):
            continue
        if all(p == "*" or p == t for p, t in zip(parts, path, strict=False)):
            return True
    return False


def _get_path(node: dict, path: tuple[str, ...]):
    current = node
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _set_path(node: dict, path: tuple[str, ...], value) -> None:
    current = node
    for part in path[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[path[-1]] = value


def _remove_path(node: dict, path: tuple[str, ...]) -> None:
    parent = _get_path(node, path[:-1]) if path[:-1] else node
    if isinstance(parent, dict):
        parent.pop(path[-1], None)


def _is_host_scoped(path: tuple[str, ...], value, host_keys) -> bool:
    if _is_host_key(path, host_keys):
        return True
    return isinstance(value, str) and _is_abs_user_path(value)


def strip_host_keys(config_text: str, host_keys) -> str:
    """Remove host-key subtrees and absolute user-home paths (what push bundles)."""
    data = yaml.safe_load(config_text) or {}
    if not isinstance(data, dict):
        return ""

    def prune(node: dict, prefix: tuple[str, ...]) -> dict:
        out: dict = {}
        for key, value in node.items():
            path = prefix + (str(key),)
            if isinstance(value, dict) and value:
                sub = prune(value, path)
                if sub:
                    out[key] = sub
                continue
            if _is_host_scoped(path, value, host_keys):
                continue
            out[key] = value
        return out

    pruned = prune(data, ())
    return yaml.safe_dump(pruned, sort_keys=False) if pruned else ""


def merge_config(remote_text: str, local_text: str, host_keys, *, first_pull: bool = False) -> str:
    """Remote wins, except host-scoped leaves where the local value always
    survives. With ``first_pull`` the union keeps local precedence instead:
    local wins everywhere and the remote contributes only missing
    non-host-scoped leaves (the 0.4.2 first-pull contract)."""
    remote = yaml.safe_load(remote_text) or {}
    local = yaml.safe_load(local_text) or {}
    if not isinstance(remote, dict):
        remote = {}
    if not isinstance(local, dict):
        local = {}
    if first_pull:
        result = json.loads(json.dumps(local))  # deep copy without yaml aliasing
        for path, value in _iter_leaves(remote):
            if _is_host_scoped(path, value, host_keys):
                continue
            if _get_path(result, path) is None:
                _set_path(result, path, value)
        return yaml.safe_dump(result, sort_keys=False) if result else ""
    result = json.loads(json.dumps(remote))  # deep copy without yaml aliasing
    remote_leaves = list(_iter_leaves(result))
    for path, value in remote_leaves:
        if _is_host_scoped(path, value, host_keys):
            local_value = _get_path(local, path)
            if local_value is not None:
                _set_path(result, path, local_value)
            else:
                _remove_path(result, path)
    for path, value in _iter_leaves(local):
        if _is_host_scoped(path, value, host_keys) and _get_path(result, path) is None:
            _set_path(result, path, value)
    return yaml.safe_dump(result, sort_keys=False) if result else ""


def build_bundle(
    home: Path,
    entries: list[FileEntry],
    out_path: Path,
    *,
    key: bytes,
    header: dict,
    extra_files: dict[str, bytes],
) -> FakeBundleInfo:
    """Zip walk entries (extras override same-relpath files) into one AES-256-GCM envelope."""
    home = Path(home)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plain = io.BytesIO()
    manifest: dict[str, dict] = {}
    with zipfile.ZipFile(plain, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in entries:
            if entry.relpath in extra_files:
                continue  # the extra file overrides the on-disk content
            data = (home / entry.relpath).read_bytes()
            archive.writestr(entry.relpath, data)
            manifest[entry.relpath] = {
                "sha256": _sha256(data), "size": len(data), "mode": entry.mode
            }
        for relpath, data in extra_files.items():
            archive.writestr(relpath, data)
            manifest[relpath] = {
                "sha256": _sha256(data), "size": len(data), "mode": 0o644
            }
        archive.writestr(MANIFEST_MEMBER, canonical_json(manifest))
    plain_bytes = plain.getvalue()
    final_header = dict(header)
    final_header.update(
        plain_sha256=_sha256(plain_bytes),
        plain_size=len(plain_bytes),
        key_id=_sha256(key)[:16],
    )
    header_bytes = canonical_json(final_header)
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plain_bytes, header_bytes)
    blob = MAGIC + len(header_bytes).to_bytes(4, "big") + header_bytes + nonce + ciphertext
    tmp = out_path.with_name(f".{out_path.name}.tmp")
    with open(tmp, "xb") as handle:
        handle.write(blob)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, out_path)
    return FakeBundleInfo(
        path=out_path,
        size=len(blob),
        sha256=_sha256(blob),
        plain_sha256=final_header["plain_sha256"],
        plain_size=len(plain_bytes),
        header=final_header,
    )


def read_header(path: Path) -> dict:
    """Parse the envelope header without the key."""
    data = Path(path).read_bytes()
    if not data.startswith(MAGIC):
        raise ArchiveError("bundle has a bad magic marker")
    length = int.from_bytes(data[5:9], "big")
    try:
        return json.loads(data[9 : 9 + length])
    except ValueError as exc:
        raise ArchiveError("bundle header is not valid JSON") from exc


def open_bundle(path: Path, *, key: bytes, dest_dir: Path, limits: object) -> dict:
    """Verify the envelope, decrypt, validate every member, extract into an empty dir."""
    data = Path(path).read_bytes()
    dest_dir = Path(dest_dir)
    if not data.startswith(MAGIC):
        raise ArchiveError("bundle has a bad magic marker")
    length = int.from_bytes(data[5:9], "big")
    header_bytes = data[9 : 9 + length]
    try:
        header = json.loads(header_bytes)
    except ValueError as exc:
        raise ArchiveError("bundle header is not valid JSON") from exc
    nonce = data[9 + length : 9 + length + 12]
    ciphertext = data[9 + length + 12 :]
    try:
        plain = AESGCM(key).decrypt(nonce, ciphertext, header_bytes)
    except InvalidTag as exc:
        raise VerifyError("bundle failed AES-GCM verification (wrong key or tampered)") from exc
    if _sha256(plain) != header.get("plain_sha256") or len(plain) != header.get("plain_size"):
        raise VerifyError("bundle plaintext does not match its header")
    max_total = getattr(limits, "max_total_bytes", None)
    if max_total is not None and len(plain) > int(max_total):
        raise ArchiveError(f"bundle plaintext exceeds the {max_total} byte limit")
    max_members = getattr(limits, "max_members", None)
    if dest_dir.exists() and any(dest_dir.iterdir()):
        raise ArchiveError("extraction destination is not empty")
    max_member_bytes = getattr(limits, "max_member_bytes", None)
    with zipfile.ZipFile(io.BytesIO(plain)) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            raise ArchiveError("bundle contains duplicate member names")
        if max_members is not None and len(names) > int(max_members):
            raise ArchiveError(f"bundle contains more than {max_members} members")
        for name in names:
            if name.startswith(("/", "//")) or ".." in name.split("/") or "\\" in name:
                raise ArchiveError(f"unsafe bundle member name: {name!r}")
        if MANIFEST_MEMBER not in names:
            raise ArchiveError("bundle is missing its manifest")
        manifest = json.loads(archive.read(MANIFEST_MEMBER))
        for name in names:
            member = archive.read(name)
            if name == MANIFEST_MEMBER:
                continue
            entry = manifest.get(name)
            if entry is None:
                raise ArchiveError(f"bundle member is not in the manifest: {name}")
            if _sha256(member) != entry.get("sha256") or len(member) != entry.get("size"):
                raise ArchiveError(f"bundle member does not match the manifest: {name}")
            if max_member_bytes is not None and len(member) > int(max_member_bytes):
                raise ArchiveError(f"bundle member exceeds the {max_member_bytes} byte limit")
        dest_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(dest_dir, 0o700)
        for name in names:
            target = dest_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "xb") as handle:
                handle.write(archive.read(name))
                handle.flush()
                os.fsync(handle.fileno())
            mode = 0o644
            if name != MANIFEST_MEMBER:
                mode = int(manifest[name].get("mode", 0o644)) & 0o777
            os.chmod(target, mode)
    return manifest


def install_tree_stats(root: Path) -> dict:
    """Helper for tests: relpath -> (sha256, mode) for every file under root."""
    out: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            out[path.relative_to(root).as_posix()] = (
                _sha256_file(path),
                stat.S_IMODE(path.stat().st_mode),
            )
    return out


class Pipeline:
    """Duck-typed stand-in for roam.engine.Pipeline bound to the fake functions."""

    def __init__(self) -> None:
        self.walk_home = walk_home
        self.classify = classify
        self.load_rules = lambda home: FakeRules()
        self.build_bundle = build_bundle
        self.open_bundle = open_bundle
        self.read_header = read_header
        self.merge_config = merge_config
        self.strip_host_keys = strip_host_keys
        self.generate_installed_plugins = generate_installed_plugins


PIPELINE = Pipeline()
