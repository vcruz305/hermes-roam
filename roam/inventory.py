"""Home walk and scope classification for the sync manifest."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from . import scope
from .model import FileEntry, Scope
from .scope import Rules

_CHUNK = 1024 * 1024


def classify(relpath: str, rules: Rules) -> Scope:
    """Scope for one POSIX relpath inside the home; trailing '/' marks a directory."""
    return scope.classify_path(relpath, rules)


def sha256_file(path: str | os.PathLike) -> str:
    """Streaming SHA-256 of a file; never loads it into memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def walk_home(home: Path, rules: Rules, *, include: set[Scope]) -> list[FileEntry]:
    """List regular files under home whose scope is in include, sorted by relpath.

    Symlinks and non-regular files are skipped, symlinked directories are never
    followed, and directory trees that no included-scope rule can ever match
    (all LOCAL/IGNORED trees under the production include set) are pruned at
    the directory level, so they are never traversed, listed, or opened.
    """
    home = Path(home)
    if not home.is_dir():
        raise FileNotFoundError(f"home does not exist: {home}")
    included = set(include) - {Scope.IGNORED}  # IGNORED is never listable
    entries: list[FileEntry] = []
    for root, directories, files in os.walk(home, topdown=True, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(home).as_posix()
        kept: list[str] = []
        for name in sorted(directories):
            child = root_path / name
            if child.is_symlink():
                continue  # never follow, never list
            rel = f"{rel_root}/{name}/" if rel_root != "." else f"{name}/"
            if scope.dir_prunable(rel, rules, included):
                continue  # nothing included can ever live below: never traverse
            kept.append(name)
        directories[:] = kept
        for name in sorted(files):
            child = root_path / name
            rel = f"{rel_root}/{name}" if rel_root != "." else name
            scope_value = scope.classify_path(rel, rules)
            if scope_value not in included:
                continue
            try:
                info = os.lstat(child)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue  # skips symlinks, sockets, fifos, devices
            try:
                digest = sha256_file(child)
            except OSError:
                continue
            mode = info.st_mode & 0o777  # real permission bits; never widened
            entries.append(FileEntry(rel, digest, info.st_size, mode, scope_value))
    entries.sort(key=lambda entry: entry.relpath)
    return entries


def manifest_from_entries(entries: list[FileEntry]) -> dict[str, dict]:
    """relpath -> FileEntry.to_dict() for every entry."""
    return {entry.relpath: entry.to_dict() for entry in entries}


def diff_manifests(
    base: dict[str, dict], other: dict[str, dict]
) -> tuple[list[str], list[str], list[str]]:
    """(added, changed, removed) relpath lists, sorted, by sha256 comparison."""
    base_keys = set(base)
    other_keys = set(other)
    added = sorted(other_keys - base_keys)
    removed = sorted(base_keys - other_keys)
    changed = sorted(
        key for key in base_keys & other_keys if base[key].get("sha256") != other[key].get("sha256")
    )
    return added, changed, removed
