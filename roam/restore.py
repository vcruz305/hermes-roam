"""Journaled transactional install of pulled files (simplified port of vigil's restore journal)."""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import stat
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import cron_install, winfs
from .errors import RestoreError
from .model import Scope, canonical_json
from .scope import is_config_path as _default_is_config

PHASE_PREPARED = "prepared"
PHASE_INSTALLING = "installing"
PHASE_COMMITTED = "committed"
PHASE_ROLLED_BACK = "rolled_back"
PHASE_UNRECOVERABLE = "unrecoverable"
MANIFEST_MEMBER = "roam/manifest.json"
_BACKUP_TS_FORMAT = "%Y%m%d-%H%M%S"
_CHUNK = 1 << 20


@dataclass(frozen=True, slots=True)
class PlannedFile:
    """One staged file approved for installation into the home."""

    relpath: str
    staged_path: Path
    sha256: str
    size: int
    mode: int
    scope: Scope

    def to_dict(self) -> dict:
        return {
            "relpath": self.relpath,
            "sha256": self.sha256,
            "size": self.size,
            "mode": self.mode,
            "scope": self.scope.value,
        }


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """What a pull would change in the home; produced by plan_install, consumed by apply."""

    to_write: tuple[PlannedFile, ...] = ()
    to_delete: tuple[str, ...] = ()
    # (relpath, staged, mode) config files to merge; mode is "merge" (the
    # remote shared projection wins for shared keys) or "first_pull" (union:
    # remote-only shared keys are added, local values win where both set)
    configs: tuple[tuple[str, Path, str], ...] = ()
    skipped: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()  # local-vs-remote divergences kept local
    unchanged: tuple[str, ...] = ()  # staged content identical to local: never rewritten
    config_conflicts: tuple[str, ...] = ()  # configs whose shared content diverged on both sides
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "to_write": [f.to_dict() for f in self.to_write],
            "to_delete": list(self.to_delete),
            "configs": [[relpath, mode] for relpath, _, mode in self.configs],
            "skipped": list(self.skipped),
            "conflicts": list(self.conflicts),
            "unchanged": list(self.unchanged),
            "config_conflicts": list(self.config_conflicts),
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class AppliedResult:
    files_written: int
    files_deleted: int
    config_merged: bool
    backup_dir: str
    journal_path: str

    def to_dict(self) -> dict:
        return {
            "files_written": self.files_written,
            "files_deleted": self.files_deleted,
            "config_merged": self.config_merged,
            "backup_dir": self.backup_dir,
            "journal_path": self.journal_path,
        }


def _safe_relpath(relpath: str) -> PurePosixPath:
    """Validate a POSIX relpath; refuse anything that could escape the home."""
    if not isinstance(relpath, str) or "\\" in relpath or "\x00" in relpath:
        raise RestoreError(f"unsafe relpath: {relpath!r}")
    path = PurePosixPath(relpath)
    if path.is_absolute() or not path.parts or any(p in {"", ".", ".."} for p in path.parts):
        raise RestoreError(f"unsafe relpath: {relpath!r}")
    return path


def _entry_scope(entry: dict) -> Scope | None:
    raw = entry.get("scope") if isinstance(entry, dict) else None
    try:
        return Scope(raw) if raw is not None else None
    except ValueError:
        return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
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


def _copy_atomic(target: Path, source: Path, mode: int) -> None:
    """Copy source to target via a temp sibling + os.replace, fsynced, with the given mode."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "xb") as out, open(source, "rb") as inp:
            while chunk := inp.read(_CHUNK):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, mode & 0o777)
        winfs.replace(tmp, target)
        _fsync_dir(target.parent)
    except BaseException:
        winfs.unlink(tmp)
        raise


def _write_atomic(target: Path, data: bytes, mode: int) -> None:
    """Write bytes to target via a temp sibling + os.replace, fsynced, with the given mode."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode & 0o777)
        winfs.replace(tmp, target)
        _fsync_dir(target.parent)
    except BaseException:
        winfs.unlink(tmp)
        raise


def _install_from_staged(target: Path, staged: Path, mode: int) -> None:
    """Install one staged/backup file into the home (temp sibling + os.replace)."""
    _copy_atomic(target, staged, mode)


def _write_journal(path: Path, record: dict) -> None:
    """Persist the journal atomically and fsynced before any filesystem change."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "xb") as handle:
            handle.write(canonical_json(record))
            handle.flush()
            os.fsync(handle.fileno())
        winfs.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        winfs.unlink(tmp)
        raise


def _entry_sha(entry: object) -> str | None:
    if not isinstance(entry, dict):
        return None
    value = entry.get("sha256")
    return value if isinstance(value, str) else None


def _local_state_of(home: Path, relpath: str) -> tuple[bool, str | None]:
    """(exists, sha256-or-None) of the local file at relpath; never follows a
    symlink out of the home and never raises on an unreadable file."""
    path = home / relpath
    try:
        if not os.path.lexists(path):
            return False, None
        if path.is_symlink() or not path.is_file():
            return True, None  # exists but is not comparable content: always a divergence
        return True, _sha256_file(path)
    except OSError:
        return True, None


def _local_config_text(home: Path, relpath: str) -> str:
    path = home / relpath
    try:
        if path.is_symlink() or not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def plan_install(
    home: Path,
    staging_dir: Path,
    manifest: dict,
    *,
    classes: set[Scope],
    rules: object,
    base_manifest: dict,
    config_base_digests: dict[str, str] | None = None,
    shared_projection_fn: Callable[[str], str] | None = None,
    first_pull_conflict_keys_fn: Callable[[str, str], list[str]] | None = None,
    overwrite_conflicts: bool = False,
    is_config: Callable[[str], bool] | None = None,
) -> InstallPlan:
    """Compute what a pull would change. Three-way everywhere (R3).

    Every path is compared with the last sync point (base_manifest):

    - a base-managed file that is MISSING locally is a local deletion =
      diverged: a conflict when the remote changed it since the last sync,
      otherwise it stays deleted (never reinstalled) and the next push
      from this device publishes the deletion;
    - local content that diverged since the last sync is a conflict — kept
      local and listed in ``conflicts`` unless ``overwrite_conflicts``;
    - staged content identical to the local file is ``unchanged``: never
      rewritten, never backed up, never counted.

    Config files are never written or deleted verbatim. With a
    ``shared_projection_fn`` each config's shared projection digest is
    compared local vs base vs remote: a remote-only change merges
    (``configs``, mode ``merge``); a local-only change stays local (the
    next push publishes it); a change on both sides is a
    ``config_conflict`` kept local unless ``overwrite_conflicts``; identical
    shared content is ``unchanged``. A config with NO accepted base (a
    first pull) merges as a union instead (mode ``first_pull``): the
    remote's shared keys are added where the local file lacks them, every
    local value wins where both sides set the same key (listed in
    ``notes`` via ``first_pull_conflict_keys_fn``), and host-scoped and
    secret keys stay local; ``overwrite_conflicts`` takes the remote
    projection wholesale instead. Without ``shared_projection_fn`` configs
    merge as today (legacy callers).
    """
    home = Path(home)
    staging_dir = Path(staging_dir)
    detect_config = is_config if is_config is not None else _default_is_config
    base = base_manifest if isinstance(base_manifest, dict) else {}
    base_digests = config_base_digests if isinstance(config_base_digests, dict) else {}
    to_write: list[PlannedFile] = []
    to_delete: list[str] = []
    skipped: list[str] = []
    conflicts: list[str] = []
    unchanged: list[str] = []
    config_conflicts: list[str] = []
    notes: list[str] = []
    configs: list[tuple[str, Path, str]] = []
    for relpath in sorted(manifest):
        if relpath == MANIFEST_MEMBER or not isinstance(relpath, str):
            continue
        _safe_relpath(relpath)
        entry = manifest[relpath] if isinstance(manifest[relpath], dict) else {}
        if detect_config(relpath):
            scope = _entry_scope(entry)
            if scope is None or scope in (Scope.LOCAL, Scope.IGNORED):
                skipped.append(relpath)
                notes.append(f"{relpath}: config refused by this device's local rules")
                continue
            if scope not in classes:
                skipped.append(relpath)
                notes.append(f"{relpath}: scope {scope.value} not selected for this pull")
                continue
            staged = staging_dir / relpath
            if not staged.is_file():
                raise RestoreError(f"the staged config file is missing: {relpath}")
            if shared_projection_fn is None:
                # legacy caller: no three-way config data available
                configs.append((relpath, staged, "merge"))
                continue
            staged_text = staged.read_text(encoding="utf-8", errors="replace")
            local_text = _local_config_text(home, relpath)
            remote_digest = shared_projection_fn(staged_text)
            local_digest = shared_projection_fn(local_text)
            if local_digest == remote_digest:
                unchanged.append(relpath)
                continue
            base_digest = base_digests.get(relpath)
            if base_digest is None:
                # first pull: this device never accepted a shared
                # projection for this config. The merge is a union — the
                # remote's shared keys are added where the local file lacks
                # them and every local value wins where both sides set the
                # same key — unless the user explicitly takes the remote
                # projection wholesale with --overwrite-conflicts.
                if overwrite_conflicts:
                    configs.append((relpath, staged, "merge"))
                    continue
                configs.append((relpath, staged, "first_pull"))
                kept_keys = (
                    first_pull_conflict_keys_fn(staged_text, local_text)
                    if first_pull_conflict_keys_fn is not None
                    else []
                )
                if kept_keys:
                    shown = ", ".join(kept_keys[:10])
                    more = f" (+{len(kept_keys) - 10} more)" if len(kept_keys) > 10 else ""
                    notes.append(
                        f"{relpath}: never synced here; the remote's shared keys are "
                        f"added where this file lacks them, and the local value is kept "
                        f"for shared key(s) already set here: {shown}{more} "
                        "(push from this device to publish them, or pull with "
                        "--overwrite-conflicts to take the remote values)"
                    )
                else:
                    notes.append(
                        f"{relpath}: never synced here; the remote's shared keys are "
                        "added; host-scoped and secret keys stay local"
                    )
                continue
            if remote_digest == base_digest:
                # local-only change: keep local; the next push publishes it
                notes.append(
                    f"{relpath}: changed locally since the last sync; kept local "
                    "(push from this device to publish it)"
                )
                continue
            if local_digest == base_digest:
                # remote-only change: merge as today
                configs.append((relpath, staged, "merge"))
                continue
            if overwrite_conflicts:
                configs.append((relpath, staged, "merge"))
            else:
                config_conflicts.append(relpath)
            continue
        scope = _entry_scope(entry)
        if scope is None:
            skipped.append(relpath)
            notes.append(f"{relpath}: manifest entry carries no scope; refused")
            continue
        if scope in (Scope.LOCAL, Scope.IGNORED):
            skipped.append(relpath)
            continue
        if scope not in classes:
            skipped.append(relpath)
            notes.append(f"{relpath}: scope {scope.value} not selected for this pull")
            continue
        staged = staging_dir / relpath
        if not staged.is_file():
            raise RestoreError(f"the staged file is missing: {relpath}")
        sha = _sha256_file(staged)
        size = staged.stat().st_size
        if sha != entry.get("sha256") or size != entry.get("size"):
            raise RestoreError(f"the staged file does not match the bundle manifest: {relpath}")
        exists, local_sha = _local_state_of(home, relpath)
        if exists and local_sha == sha:
            # identical staged and local content: a rewrite would change
            # nothing, so none is planned, backed up, or counted (R3)
            unchanged.append(relpath)
            continue
        if not exists and relpath in base:
            base_sha = _entry_sha(base.get(relpath))
            if sha == base_sha:
                # R3: the local deletion stands whatever the caller asked —
                # the remote never changed the file — and the next push
                # from this device publishes the deletion
                notes.append(
                    f"{relpath}: deleted locally since the last sync; kept "
                    "deleted (push from this device to publish the deletion)"
                )
                continue
            if not overwrite_conflicts:
                # local deletion vs a remote change since the last sync
                conflicts.append(relpath)
                continue
            # overwrite_conflicts: the remote change wins over the deletion
        if not overwrite_conflicts:
            if exists:
                if relpath in base:
                    diverged = local_sha != _entry_sha(base.get(relpath))
                else:
                    # never synced locally: a remote file colliding with
                    # different local content is a conflict too
                    diverged = local_sha != sha
                if diverged:
                    conflicts.append(relpath)
                    continue
        to_write.append(
            PlannedFile(relpath, staged, sha, size, int(entry.get("mode", 0o644)) & 0o777, scope)
        )
    for relpath in sorted(base):
        if detect_config(relpath) or relpath == MANIFEST_MEMBER or relpath in manifest:
            continue
        entry = base[relpath] if isinstance(base[relpath], dict) else {}
        scope = _entry_scope(entry)
        if scope is None or scope in (Scope.LOCAL, Scope.IGNORED) or scope not in classes:
            continue
        _safe_relpath(relpath)
        if not overwrite_conflicts:
            exists, local_sha = _local_state_of(home, relpath)
            if exists and local_sha != _entry_sha(entry):
                # the user recreated or edited the file after the remote
                # deleted it: destroying that work would be data loss
                conflicts.append(relpath)
                continue
        to_delete.append(relpath)
    return InstallPlan(
        to_write=tuple(to_write),
        to_delete=tuple(to_delete),
        configs=tuple(configs),
        skipped=tuple(sorted(set(skipped))),
        conflicts=tuple(sorted(set(conflicts))),
        unchanged=tuple(sorted(set(unchanged))),
        config_conflicts=tuple(sorted(set(config_conflicts))),
        notes=tuple(notes),
    )


def _backup_file(home: Path, relpath: str, backup_dir: Path) -> bool:
    source = home / relpath
    if not source.is_file():
        return False
    mode = stat.S_IMODE(source.stat().st_mode) or 0o644
    _copy_atomic(backup_dir / relpath, source, mode)
    return True


def _rollback(
    home: Path,
    record: dict,
    backup_dir: Path,
    locks: cron_install.CronLocks | None = None,
) -> tuple[int, int, int]:
    """Restore every backed-up file, remove files roam itself created, and
    NEVER unlink a pre-existing file whose backup is missing (R5).

    ``record["preexisting"]`` lists the affected paths that existed in the
    home before the install; a path without a backup that is NOT in that
    list was created by the aborted install and is removed. A pre-existing
    path without a backup (pruned backups, symlink targets) is left in
    place and counted ``unrecoverable`` instead of being destroyed.
    Journals written before the field existed are treated as fully
    pre-existing: the safe, non-destructive reading.

    0.5.3: a cron jobs.json is only ever restored under Hermes's own cron
    lock. When the caller already holds the locks (a failed in-process
    install) they are re-asserted; crash recovery acquires them with the
    same bounded wait, and a lock held by another process leaves the file
    in place and counted unrecoverable — never written unlocked.

    Returns (restored, removed, unrecoverable).
    """
    restored = 0
    removed = 0
    unrecoverable = 0
    preexisting = record.get("preexisting")
    if not isinstance(preexisting, list):
        # journal written before the field existed: assume everything was
        # pre-existing — the safe, non-destructive reading
        preexisting = list(record.get("to_write", [])) + list(record.get("to_delete", []))
    preexisting = set(preexisting)
    cron_paths = {
        relpath
        for relpath in list(record.get("to_write", [])) + list(record.get("to_delete", []))
        if cron_install.is_cron_jobs_path(relpath)
    }
    own_locks: cron_install.CronLocks | None = None
    if cron_paths and locks is None:
        own_locks = cron_install.CronLocks(home)
    try:
        for relpath in record.get("to_write", []):
            backup = backup_dir / relpath
            target = home / relpath
            if relpath in cron_paths and not _restore_cron_locked(
                home, relpath, backup, locks if locks is not None else own_locks
            ):
                unrecoverable += 1
                continue
            if backup.is_file():
                _install_from_staged(
                    target, backup, stat.S_IMODE(backup.stat().st_mode) or 0o644
                )
                restored += 1
            elif relpath in preexisting:
                # R5: no backup for a file that existed before the install —
                # unlinking it would destroy the pre-pull version forever
                unrecoverable += 1
            elif target.is_file() or target.is_symlink():
                winfs.unlink(target)
                removed += 1
        for relpath in record.get("to_delete", []):
            if relpath not in preexisting:
                continue  # did not exist before the pull: nothing to restore
            backup = backup_dir / relpath
            if relpath in cron_paths and not _restore_cron_locked(
                home, relpath, backup, locks if locks is not None else own_locks
            ):
                unrecoverable += 1
                continue
            if backup.is_file():
                _install_from_staged(
                    home / relpath, backup, stat.S_IMODE(backup.stat().st_mode) or 0o644
                )
                restored += 1
            else:
                # the pre-pull file cannot be brought back: say so, never
                # pretend the rollback was clean
                unrecoverable += 1
        return restored, removed, unrecoverable
    finally:
        if own_locks is not None:
            own_locks.release()


def _restore_cron_locked(
    home: Path, relpath: str, backup: Path, locks: cron_install.CronLocks | None
) -> bool:
    """Whether the cron lock for relpath is (or could be) held by this
    rollback. False means the restore must be skipped and counted
    unrecoverable — the file is left in place, never written unlocked."""
    if locks is None:
        return False
    try:
        locks.acquire(relpath)
    except cron_install.CronLockError:
        return False
    return True


def apply(
    home: Path,
    plan: InstallPlan,
    *,
    journal_dir: Path,
    backup_dir: Path,
    merge_config_fn: Callable[..., str],
    host_keys: list[str],
) -> AppliedResult:
    """Install a plan into the home, journaled and rollback-safe.

    0.5.3: when the plan touches a Hermes cron store (``cron/jobs.json`` or
    ``profiles/<name>/cron/jobs.json``), every such store's lock — Hermes's
    own cross-process ``.jobs.lock`` flock — is taken BEFORE anything is
    written, with the same bounded wait Hermes's cron store uses. A lock
    held by another process fails the whole install CLOSED with a clear
    error and nothing written; while the install runs (and if it is rolled
    back) the locks stay held, so no cron store is ever written unlocked.
    """
    home = Path(home)
    journal_dir = Path(journal_dir)
    backup_dir = Path(backup_dir)
    affected = [f.relpath for f in plan.to_write] + list(plan.to_delete)
    for relpath, _staged, _mode in plan.configs:
        affected.append(relpath)
    for relpath in affected:
        _safe_relpath(relpath)
    if not affected:
        # R3: nothing would change — no journal, no backup directory, no FS
        # writes. A converged pull must be a true no-op.
        return AppliedResult(
            files_written=0,
            files_deleted=0,
            config_merged=False,
            backup_dir="",
            journal_path="",
        )
    cron_paths = sorted(
        {relpath for relpath in affected if cron_install.is_cron_jobs_path(relpath)}
    )
    locks = cron_install.CronLocks(home)
    try:
        for relpath in cron_paths:
            locks.acquire(relpath)
        txid = uuid.uuid4().hex[:16]
        now = time.time()
        record = {
            "v": 1,
            "transaction_id": txid,
            "phase": PHASE_PREPARED,
            "home": str(home),
            "backup_dir": str(backup_dir),
            "to_write": sorted(f.relpath for f in plan.to_write)
            + sorted(relpath for relpath, _staged, _mode in plan.configs),
            "to_delete": sorted(plan.to_delete),
            # R5: which affected paths existed before the install; the rollback
            # removes only what roam itself created and never unlinks a
            # pre-existing path whose backup is missing.
            "preexisting": sorted(
                relpath for relpath in affected if os.path.lexists(home / relpath)
            ),
            "created_at": now,
            "updated_at": now,
            "error": None,
        }
        journal_dir.mkdir(parents=True, exist_ok=True)
        backup_dir.mkdir(parents=True, exist_ok=True)
        journal_path = journal_dir / f"{int(now)}-{txid}.json"
        _write_journal(journal_path, record)
        try:
            for relpath in sorted(set(record["to_write"] + record["to_delete"])):
                _backup_file(home, relpath, backup_dir)
        except Exception as exc:
            record.update(
                phase=PHASE_ROLLED_BACK,
                error=f"backup pass failed: {exc}",
                updated_at=time.time(),
            )
            _write_journal(journal_path, record)
            raise RestoreError(
                f"backup pass failed ({exc}); nothing was installed; journal marked rolled_back"
            ) from exc
        record.update(phase=PHASE_INSTALLING, updated_at=time.time())
        _write_journal(journal_path, record)
        written = 0
        deleted = 0
        config_merged = False
        try:
            for item in plan.to_write:
                if cron_install.is_cron_jobs_path(item.relpath):
                    # 0.5.3: cron stores install through Hermes's own cron
                    # lock (held for this whole install) with the
                    # shrink-merge guard that preserves a concurrent local
                    # create.
                    cron_install.install_jobs_file(
                        home,
                        item.relpath,
                        item.staged_path.read_bytes(),
                        item.mode,
                        locks,
                    )
                else:
                    _install_from_staged(home / item.relpath, item.staged_path, item.mode)
                written += 1
            for relpath in plan.to_delete:
                target = home / relpath
                if target.is_file() or target.is_symlink():
                    winfs.unlink(target)
                    _fsync_dir(target.parent)
                    deleted += 1
            for relpath, staged_path, mode_flag in plan.configs:
                local_config = home / relpath
                # keep the local file's mode; a config that does not exist yet is
                # private (it will hold this host's secrets after the merge)
                try:
                    mode = stat.S_IMODE(local_config.stat().st_mode)
                except OSError:
                    mode = 0o600
                if not mode:
                    mode = 0o600
                local_text = local_config.read_text() if local_config.is_file() else ""
                merged = merge_config_fn(
                    staged_path.read_text(), local_text, host_keys,
                    first_pull=(mode_flag == "first_pull"),
                )
                _write_atomic(local_config, merged.encode("utf-8"), mode)
                config_merged = True
            record.update(phase=PHASE_COMMITTED, updated_at=time.time())
            _write_journal(journal_path, record)
        except Exception as exc:
            restored, removed, unrecoverable = _rollback(
                home, record, backup_dir, locks
            )
            if unrecoverable:
                # R5: content could not be restored — the home is NOT back to its
                # pre-pull state and the journal must say so forever
                record.update(
                    phase=PHASE_UNRECOVERABLE,
                    error=(
                        f"{type(exc).__name__}: {exc}; rollback restored {restored} file(s), "
                        f"removed {removed} new file(s), but {unrecoverable} pre-existing "
                        "file(s) had no backup and were left in place"
                    ),
                    updated_at=time.time(),
                )
                _write_journal(journal_path, record)
                raise RestoreError(
                    f"install failed ({type(exc).__name__}: {exc}); rollback restored "
                    f"{restored} file(s) and removed {removed} new file(s), but "
                    f"{unrecoverable} file(s) could not be restored (no backup) and "
                    "were left in place; the journal is marked unrecoverable — "
                    "restore the affected files from a real backup"
                ) from exc
            record.update(
                phase=PHASE_ROLLED_BACK,
                error=f"{type(exc).__name__}: {exc}",
                updated_at=time.time(),
            )
            _write_journal(journal_path, record)
            raise RestoreError(
                f"install failed ({type(exc).__name__}: {exc}); rollback restored {restored} "
                f"file(s) and removed {removed} new file(s)"
            ) from exc
        return AppliedResult(
            files_written=written,
            files_deleted=deleted,
            config_merged=config_merged,
            backup_dir=str(backup_dir),
            journal_path=str(journal_path),
        )
    finally:
        locks.release()


def recover(home: Path, journal_dir: Path) -> list[str]:
    """Roll back any journal stuck mid-install (crash recovery). Returns human outcomes.

    R5: recovery is non-destructutive. A pre-existing file whose backup is
    missing (pruned backup windows) is NEVER unlinked: it is left in place,
    the journal is marked ``unrecoverable``, and the outcome says so.
    """
    home = Path(home)
    journal_dir = Path(journal_dir)
    outcomes: list[str] = []
    if not journal_dir.is_dir():
        return outcomes
    for journal_path in sorted(journal_dir.glob("*.json")):
        try:
            record = json.loads(journal_path.read_bytes())
        except (OSError, ValueError):
            outcomes.append(f"{journal_path.name}: unreadable journal; left in place")
            continue
        if not isinstance(record, dict):
            outcomes.append(f"{journal_path.name}: malformed journal; left in place")
            continue
        phase = record.get("phase")
        if phase == PHASE_INSTALLING:
            backup_dir = Path(str(record.get("backup_dir", "")))
            restored, removed, unrecoverable = _rollback(home, record, backup_dir)
            if unrecoverable:
                record.update(
                    phase=PHASE_UNRECOVERABLE,
                    error=(
                        record.get("error")
                        or "recovery found no backup for pre-existing file(s)"
                    ),
                    updated_at=time.time(),
                )
                _write_journal(journal_path, record)
                outcomes.append(
                    f"{journal_path.name}: NOT rolled back — {unrecoverable} file(s) "
                    "had no backup and were left in place (nothing was destroyed); "
                    "marked unrecoverable, restore them from a real backup"
                )
            else:
                record.update(
                    phase=PHASE_ROLLED_BACK,
                    error=record.get("error") or "rolled back by recovery",
                    updated_at=time.time(),
                )
                _write_journal(journal_path, record)
                outcomes.append(
                    f"{journal_path.name}: rolled back ({restored} restored, {removed} removed)"
                )
        elif phase == PHASE_UNRECOVERABLE:
            outcomes.append(
                f"{journal_path.name}: still unrecoverable: {record.get('error') or 'no backup'}; "
                "the affected files were left in place — restore them from a real backup"
            )
        elif phase == PHASE_PREPARED:
            record.update(
                phase=PHASE_ROLLED_BACK,
                error=record.get("error") or "closed by recovery before install",
                updated_at=time.time(),
            )
            _write_journal(journal_path, record)
            outcomes.append(
                f"{journal_path.name}: nothing had been installed; closed as rolled_back"
            )
    return outcomes


def _backup_dir_epoch(name: str) -> float | None:
    """UTC epoch of a %Y%m%d-%H%M%S backup dir name, or None if foreign."""
    try:
        return float(calendar.timegm(time.strptime(name, _BACKUP_TS_FORMAT)))
    except (ValueError, OverflowError):
        return None


def prune_backups(backups_root: Path, journals_dir: Path, keep: int) -> int:
    """R5 retention: keep only the newest ``keep`` backup directories and
    prune committed/rolled_back journals older than the kept window.

    Live evidence is never pruned: prepared/installing/unrecoverable
    journals and unreadable files stay whatever their age, and so do
    directories that do not carry the backup timestamp format. Returns the
    number of backup directories and journal files removed.
    """
    backups_root = Path(backups_root)
    journals_dir = Path(journals_dir)
    pruned = 0
    timestamped: list[tuple[float, Path]] = []
    if backups_root.is_dir():
        for child in backups_root.iterdir():
            if not child.is_dir() or child.is_symlink():
                continue
            epoch = _backup_dir_epoch(child.name)
            if epoch is not None:
                timestamped.append((epoch, child))
    timestamped.sort()
    stale_dirs = timestamped[: max(len(timestamped) - max(keep, 0), 0)]
    for _, stale in stale_dirs:
        winfs.rmtree(stale, ignore_errors=True)
        pruned += 1
    kept = timestamped[len(stale_dirs):]
    if keep <= 0:
        boundary: float | None = float("inf")
    elif kept:
        boundary = kept[0][0]
    else:
        boundary = None  # nothing to keep and nothing pruned: no window yet
    if boundary is not None and journals_dir.is_dir():
        for journal_path in sorted(journals_dir.glob("*.json")):
            head, _, _ = journal_path.name.partition("-")
            try:
                created = float(head)
            except ValueError:
                continue  # not a roam journal: never touched
            try:
                record = json.loads(journal_path.read_bytes())
            except (OSError, ValueError):
                continue  # unreadable: live evidence, never pruned
            if not isinstance(record, dict):
                continue
            if record.get("phase") not in (PHASE_COMMITTED, PHASE_ROLLED_BACK):
                continue  # prepared/installing/unrecoverable: never pruned
            if created < boundary:
                journal_path.unlink(missing_ok=True)
                pruned += 1
    return pruned
