"""Push/pull/status/diff orchestration. Collaborators are injected; nothing here imports Hermes."""

from __future__ import annotations

import fnmatch
import hashlib
import importlib
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Relative imports only: the package loads both as top-level ``roam`` (tests,
# source checkout) and as ``hermes_plugins.<slug>.roam`` inside Hermes, and
# every sibling must resolve to the same module object in either identity.
from . import head as head_mod
from . import restore
from . import session_archive as archive_mod
from .backends.base import Backend
from .errors import (
    ArchiveError,
    BackendError,
    BlobExists,
    CarryForwardError,
    ConfigError,
    HeadConflict,
    RemoteAhead,
    RoamError,
    VerifyError,
    VersionMismatch,
)
from .model import (
    Check,
    DeviceInfo,
    Diff,
    Head,
    LocalState,
    PullResult,
    PushResult,
    RoamSettings,
    Scope,
    Status,
    canonical_json,
    home_state_dir,
)

EXTRA_CONFIG = "config.yaml"
EXTRA_INSTALLED = "roam/installed-plugins.json"
MANIFEST_MEMBER = "roam/manifest.json"
LAST_BUNDLE = "last.roam"
FLOOR_FILE = "floor.json"
STATE_FILE = "state.json"
_CHUNK = 1 << 20
# R8: one budget per user operation — push/pull get the operation budget,
# the read-only surfaces get a short one.
_SHORT_BUDGET = 60.0


def _is_config_path(relpath: str) -> bool:
    """Whether relpath is a Hermes config file that must be stripped on push
    and merged on pull. Prefers the canonical classifier from the scope
    module; falls back to the known shapes when it is unavailable."""
    classifier = getattr(_sibling("scope"), "is_config_path", None)
    if callable(classifier):
        try:
            return bool(classifier(relpath))
        except Exception:  # noqa: BLE001 - a broken classifier falls back below
            pass
    if relpath == EXTRA_CONFIG:
        return True
    parts = relpath.split("/")
    return len(parts) == 3 and parts[0] == "profiles" and parts[2] == EXTRA_CONFIG


def _sibling(name: str):
    """Import a roam sibling under the current package identity."""
    return importlib.import_module(f"{__package__}.{name}")


def _lazy(name: str, attr: str) -> Callable[..., Any]:
    """Wrapper that imports the real Lane A1 function by name at CALL time."""

    def call(*args: Any, **kwargs: Any) -> Any:
        function = getattr(_sibling(name), attr)
        return function(*args, **kwargs)

    call.__name__ = attr
    return call


@dataclass(frozen=True, slots=True)
class Pipeline:
    """Injectable Lane A1 surface. Defaults resolve the real modules only when called."""

    walk_home: Callable[..., Any] = _lazy("inventory", "walk_home")
    classify: Callable[..., Any] = _lazy("inventory", "classify")
    load_rules: Callable[..., Any] = _lazy("scope", "load_rules")
    build_bundle: Callable[..., Any] = _lazy("archive", "build_bundle")
    open_bundle: Callable[..., Any] = _lazy("archive", "open_bundle")
    read_header: Callable[..., Any] = _lazy("archive", "read_header")
    merge_config: Callable[..., Any] = _lazy("scope", "merge_config")
    strip_host_keys: Callable[..., Any] = _lazy("scope", "strip_host_keys")
    generate_installed_plugins: Callable[..., Any] = _lazy("scope", "generate_installed_plugins")
    # 0.4.1: the opt-in session archive export (Hermes's own `hermes sessions
    # export` subprocess). Injectable so tests never spawn a real CLI.
    export_sessions: Callable[..., Any] = _lazy("session_archive", "collect_export")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _comparable(manifest: dict | None) -> dict:
    """Sync-relevant projection of a manifest: content digests only, no scope."""
    out: dict[str, tuple] = {}
    for key, value in (manifest or {}).items():
        if key == EXTRA_INSTALLED or not isinstance(value, dict):
            continue
        out[key] = (value.get("sha256"), value.get("size"), value.get("mode"))
    return out


def _version_pair(version: str) -> tuple[int, int]:
    parts = str(version).split(".")[:2]
    numbers: list[int] = []
    for part in parts:
        try:
            numbers.append(int(part))
        except ValueError:
            numbers.append(0)
    while len(numbers) < 2:
        numbers.append(0)
    return (numbers[0], numbers[1])


def _prefix_allowed(relpath: str, prefixes: list[str] | None) -> bool:
    """Whether relpath is inside the include-prefix set of a restricted pull.

    A literal directory prefix (trailing '/') matches by startswith, a literal
    file prefix by equality, and a wildcard prefix segment-by-segment so e.g.
    profiles/*/skills/ matches profiles/<name>/skills/... only.
    """
    if not prefixes:
        return True
    parts = relpath.split("/")
    for prefix in prefixes:
        if "*" not in prefix:
            if prefix.endswith("/") and relpath.startswith(prefix):
                return True
            if not prefix.endswith("/") and relpath == prefix:
                return True
            continue
        segments = prefix.rstrip("/").split("/")
        if len(parts) >= len(segments) and all(
            fnmatch.fnmatchcase(part, segment)
            for part, segment in zip(parts, segments, strict=False)
        ):
            return True
    return False


def _rollback_error(signed_generation: int, floor: int) -> VerifyError:
    return VerifyError(
        f"rollback: the remote head is at generation {signed_generation}, behind this "
        f"device's last sync point {floor}; the remote was rolled back or is serving "
        "stale data — confirm the rollback with your fleet before accepting it "
        "(pull with allow_rollback)"
    )


class Engine:
    """The only roam component that talks to a Backend. Never prints, never spawns Hermes."""

    def __init__(
        self,
        home: Path,
        settings: RoamSettings,
        backend: Backend,
        hermes_version: str,
        *,
        clock: Callable[[], float] = time.time,
        pipeline: Pipeline | None = None,
    ) -> None:
        self.home = Path(home)
        self.settings = settings
        self.backend = backend
        self.hermes_version = hermes_version
        self.clock = clock
        self.pipeline = pipeline or Pipeline()
        self._deadline: float | None = None  # R8: armed per user operation

    # ------------------------------------------------------------------ state

    def _arm(self, budget: float) -> None:
        """R8: arm ONE absolute deadline for the whole user operation."""
        self._deadline = time.monotonic() + float(budget)

    def _backend_call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Call a backend method under the operation's absolute deadline.

        R8: every merged backend method takes ``deadline`` and the engine
        always passes the armed budget — a backend method that does not
        accept one, or an engine path that forgot to arm the operation, is
        a regression and fails loudly instead of running unbounded.
        """
        function = getattr(self.backend, name)
        deadline = self._deadline
        if deadline is None:
            raise ConfigError(
                "internal error: the operation deadline was not armed"
            )
        try:
            parameters = inspect.signature(function).parameters
        except (TypeError, ValueError):
            parameters = {}
        if not ("deadline" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )):
            raise ConfigError(
                f"internal error: backend method {name} does not accept a deadline"
            )
        kwargs["deadline"] = deadline
        return function(*args, **kwargs)

    # ------------------------------------------------------------------ state

    def _roam_dir(self) -> Path:
        path = home_state_dir(self.home)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _roam_subdir(self, name: str) -> Path:
        path = self._roam_dir() / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def load_state(self) -> LocalState:
        """Read <home>/roam/state.json; a missing file means a fresh device."""
        path = self._roam_dir() / "state.json"
        if not path.is_file():
            return LocalState()
        try:
            data = json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ConfigError(f"{path} is unreadable; delete it or restore it, then retry") from exc
        return LocalState.from_dict(data)

    def save_state(self, state: LocalState) -> None:
        """Write state.json atomically.

        The tmp file has a unique name and is replaced atomically, so a crash
        mid-save (or a stale fixed-name tmp from an older roam) can never wedge
        this device: leftover tmps are swept before every save. Push and pull
        hold the process lock while saving; init is the only other writer.
        """
        path = self._roam_dir() / "state.json"
        for stale in path.parent.glob(f".{path.name}*.tmp"):
            stale.unlink(missing_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "xb") as handle:
                handle.write(canonical_json(state.to_dict()))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        _fsync_dir(path.parent)

    def _lock_record(self) -> dict | None:
        """Parse the lock file's holder JSON; None when absent or unreadable."""
        try:
            raw = (self._roam_dir() / "lock").read_bytes()
        except OSError:
            return None
        try:
            record = json.loads(raw)
        except ValueError:
            return None
        return record if isinstance(record, dict) else None

    def lock_holder(self) -> dict | None:
        """R9: the holder of the per-home lock as {pid, started_at, what}, or
        None when no other roam command holds it (a stale record left by a
        killed process does not count: the flock is what matters)."""
        lock_path = self._roam_dir() / "lock"
        if not lock_path.is_file():
            return None
        try:
            descriptor = os.open(lock_path, os.O_RDWR)
        except OSError:
            return None
        try:
            try:
                import fcntl
            except ImportError:  # pragma: no cover - non-POSIX platforms
                return None
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                record = self._lock_record()
                if record is None:
                    return {
                        "pid": None,
                        "started_at": None,
                        "what": "another roam command",
                    }
                return record
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return None
        finally:
            os.close(descriptor)

    @contextmanager
    def _process_lock(
        self, what: str = "roam command", *, skip_when_busy: bool = False
    ) -> Iterator[bool]:
        """Serialize push/pull per home; a concurrent command fails fast.

        R9: the lock file records {pid, started_at, what} while it is held,
        and the busy refusal names what holds it and since when. The lock is
        taken non-blocking: a second hermes roam command on the same home gets
        an immediate, named refusal instead of interleaving installs with
        another process. Where fcntl does not exist the lock is a no-op
        (single-process platforms). With ``skip_when_busy`` the lock yields
        False instead of raising — used by the automatic push, which must
        skip rather than wait or fail.
        """
        lock_path = self._roam_dir() / "lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                import fcntl
            except ImportError:  # pragma: no cover - non-POSIX platforms
                fcntl = None
            if fcntl is None:
                yield True
                return
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if skip_when_busy:
                    yield False
                    return
                holder = self.lock_holder() or {}
                held_what = str(holder.get("what") or "another roam command")
                started = holder.get("started_at")
                since = (
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))
                    if isinstance(started, (int, float))
                    else "an unknown time"
                )
                pid = holder.get("pid")
                who = f"pid {pid}" if isinstance(pid, int) and pid > 0 else "an unknown process"
                raise RoamError(
                    f"another hermes roam command is running on this home — a "
                    f"'{held_what}' command from {who} has held it since {since}; "
                    "wait for it to finish and retry"
                ) from exc
            # R9: record who holds the lock so the next command's refusal can say so
            os.ftruncate(descriptor, 0)
            os.write(
                descriptor,
                canonical_json({"pid": os.getpid(), "started_at": time.time(), "what": what}),
            )
            os.fsync(descriptor)
            try:
                yield True
            finally:
                os.ftruncate(descriptor, 0)
                os.fsync(descriptor)
        finally:
            os.close(descriptor)  # closing the fd releases the flock

    # ------------------------------------------------- rollback floor (R4)

    def _remote_identity(self) -> str:
        """R4: the remote identity a rollback floor is keyed by — backend plus
        bucket/prefix, git remote (userinfo stripped) plus branch, or the
        local directory."""
        backend = self.settings.backend
        if backend == "s3":
            return f"s3:{self.settings.s3_bucket}/{self.settings.s3_prefix}"
        if backend == "git":
            remote = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/@]+@", "", self.settings.git_remote)
            remote = re.sub(r"^[^/@]+@", "", remote)  # scp-like user@host:path
            return f"git:{remote}#{self.settings.git_branch}"
        if backend == "local":
            return f"local:{self.settings.local_dir}"
        return f"{backend}:{getattr(self.backend, 'name', backend)}"

    def _load_floors(self) -> dict[str, int]:
        try:
            data = json.loads((self._roam_dir() / FLOOR_FILE).read_bytes())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): value
            for key, value in data.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }

    def _rollback_floor(self, state: LocalState | None = None) -> int:
        """R4: the highest generation this device ever observed on THIS remote
        identity — from state.json AND from the floor.json file that outlives
        it (deleting state.json does not reset the guard; a new remote starts a
        new floor)."""
        state = state or self.load_state()
        floors = self._load_floors()
        return max(
            state.last_seen_generation,
            state.last_pulled_generation,
            floors.get(self._remote_identity(), 0),
        )

    def _raise_floor(self, generation: int) -> None:
        """Record the observed generation in floor.json; the floor never decreases."""
        identity = self._remote_identity()
        floors = self._load_floors()
        if floors.get(identity, 0) >= generation:
            return
        floors[identity] = generation
        path = self._roam_dir() / FLOOR_FILE
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "xb") as handle:
                handle.write(canonical_json(floors))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        _fsync_dir(path.parent)

    # ------------------------------------------------- carry-forward (R1)

    def _last_roam_path(self) -> Path:
        return self._roam_dir() / LAST_BUNDLE

    def _save_last_roam(self, bundle_path: Path) -> None:
        """R1: keep the just-published/pulled bundle (already AES-GCM
        encrypted) as this device's carry-forward source — 0600, dir 0700."""
        target = self._last_roam_path()
        try:
            target.parent.chmod(0o700)
        except OSError:  # pragma: no cover - platforms without chmod
            pass
        try:
            os.replace(bundle_path, target)
        except OSError:
            shutil.copyfile(bundle_path, target)
            bundle_path.unlink(missing_ok=True)
        os.chmod(target, 0o600)
        _fsync_dir(target.parent)

    def _extract_last_roam_members(self, relpaths: list[str]) -> dict[str, bytes]:
        """Decrypt roam/last.roam into a scratch dir and return the requested
        members' bytes. Raises CarryForwardError when the file is missing or
        unreadable: the device must pull first (R1)."""
        source = self._last_roam_path()
        if not source.is_file():
            raise CarryForwardError(
                "this push must copy content this device cannot see from "
                f"{source}, but that file is missing — run `hermes roam pull` "
                "first"
            )
        scratch = self._roam_subdir("tmp") / f"carry-{uuid.uuid4().hex}"
        try:
            try:
                self.pipeline.open_bundle(
                    source,
                    key=self.settings.archive_key,
                    dest_dir=scratch,
                    limits=self._archive_limits(),
                )
            except (OSError, RoamError) as exc:
                raise CarryForwardError(
                    f"the carry-forward bundle {source} could not be opened "
                    f"({exc}); run `hermes roam pull` first"
                ) from exc
            out: dict[str, bytes] = {}
            for relpath in relpaths:
                member = scratch / relpath
                if not member.is_file():
                    raise CarryForwardError(
                        f"the carry-forward bundle {source} does not contain "
                        f"{relpath!r} — run `hermes roam pull` first"
                    )
                out[relpath] = member.read_bytes()
            return out
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _invisible_base_entries(
        self, manifest: dict[str, dict], base_files: object, rules: object
    ) -> list[str]:
        """R1: base entries whose path this device CANNOT see under its own
        rules and opt-ins — they must be carried forward on push, never dropped."""
        base = base_files if isinstance(base_files, dict) else {}
        out: list[str] = []
        for relpath, entry in base.items():
            if relpath == MANIFEST_MEMBER or not isinstance(entry, dict):
                continue
            if relpath in manifest:
                continue  # freshly seen or freshly generated by this device
            try:
                scope = self.pipeline.classify(relpath, rules)
            except Exception:  # noqa: BLE001 - a broken classifier fails closed below
                out.append(relpath)
                continue
            if scope in (Scope.LOCAL, Scope.IGNORED) or scope not in self._push_include():
                out.append(relpath)
        return sorted(out)

    def _carry_forward_bytes(self, relpaths: list[str], base_files: dict) -> dict[str, bytes]:
        """Extract the carry-forward members and verify them against the last
        sync point; any mismatch is a named refusal to pull first (R1)."""
        members = self._extract_last_roam_members(relpaths)
        out: dict[str, bytes] = {}
        for relpath in relpaths:
            data = members.get(relpath)
            entry = base_files.get(relpath) if isinstance(base_files, dict) else None
            entry = entry if isinstance(entry, dict) else {}
            if data is None or _sha256_bytes(data) != entry.get("sha256") or len(data) != entry.get("size"):
                raise CarryForwardError(
                    f"the carry-forward copy of {relpath!r} does not match this "
                    "device's last sync point — run `hermes roam pull` first"
                )
            out[relpath] = data
        return out

    def _push_include(self) -> set[Scope]:
        """Scope classes a PUSH may ever bundle from this home (0.4.1).

        The session archive namespace is deliberately absent: no home path
        classifies into it (the raw sessions/ folder is LOCAL), and the
        opt-in archive is published as generated bundle members, never as
        walked files. The SESSIONS class here would only widen the walk.
        """
        classes = {Scope.SHARED}
        if self.settings.sync_cron:
            classes.add(Scope.CRON)
        return classes

    def _pull_default_classes(self) -> set[Scope]:
        """Default pull classes (0.4.1). The read-only session archive is
        always among them: READING what another device published needs no
        opt-in — only PUBLISHING does (sync_sessions on the pushing device)
        — and the install lands read-only under <home>/roam/session-archive/,
        never in the home tree and never re-pushable."""
        classes = {Scope.SHARED, Scope.SESSIONS}
        if self.settings.sync_cron:
            classes.add(Scope.CRON)
        return classes

    def _host_keys(self, rules: object) -> list[str]:
        """Merged default + user host keys; the Rules object always carries them."""
        keys = getattr(rules, "host_keys", None)
        if not keys:
            raise ConfigError("scope rules carry no host_keys; rules.yaml is malformed")
        return list(keys)

    def _collect(self) -> tuple[object, list, dict[str, bytes], dict[str, dict]]:
        """Walk the home and build the exact manifest a push would bundle."""
        rules = self.pipeline.load_rules(self.home)
        include = self._push_include()
        entries = self.pipeline.walk_home(self.home, rules, include=include)
        host_keys = self._host_keys(rules)
        extras: dict[str, bytes] = {}
        # Every config file travels STRIPPED of host values, never verbatim:
        # top-level config.yaml and profiles/<name>/config.yaml alike.
        for entry in entries:
            if not _is_config_path(entry.relpath):
                continue
            source = self.home / entry.relpath
            stripped = self.pipeline.strip_host_keys(source.read_text(), host_keys)
            extras[entry.relpath] = stripped.encode("utf-8")
        installed = self.pipeline.generate_installed_plugins(self.home)
        extras[EXTRA_INSTALLED] = canonical_json(installed)
        manifest = {entry.relpath: entry.to_dict() for entry in entries}
        for relpath, data in extras.items():
            manifest[relpath] = {
                "sha256": _sha256_bytes(data),
                "size": len(data),
                "mode": 0o644,
                "scope": self.pipeline.classify(relpath, rules).value,
            }
        return rules, entries, extras, manifest

    # ------------------------------------------------------------------ public

    def status(self) -> Status:
        """Local + remote summary; tolerant of backend and verification failures."""
        self._arm(_SHORT_BUDGET)
        recovered = self._crash_recovery()
        state = self.load_state()
        rules, _, _, local_manifest = self._collect()
        error = ""
        remote_generation: int | None = None
        remote_device = ""
        remote_created_at = 0.0
        devices: list[DeviceInfo] = []
        try:
            pair = self._backend_call("get_head")
            if pair is not None:
                signed = head_mod.verify_head(pair[0], self.settings.archive_key)
                remote_generation = signed.generation
                remote_device = signed.device
                remote_created_at = signed.created_at
                devices = [
                    DeviceInfo.from_dict(name, entry)
                    for name, entry in sorted(signed.devices.items())
                ]
        except (BackendError, VerifyError) as exc:
            error = str(exc)
        synced_locally = bool(state.last_pushed_generation or state.last_pulled_generation)
        if synced_locally and not any(d.name == self.settings.device_name for d in devices):
            # Pull-only devices never appear in the remote head's roster; show
            # this device from local state so `hermes roam devices` lists it.
            devices.append(
                DeviceInfo(
                    name=self.settings.device_name,
                    last_push_generation=state.last_pushed_generation,
                    last_pull_generation=state.last_pulled_generation,
                    hermes_version=self.hermes_version,
                    platform=sys.platform,
                    seen_at=max(state.last_push_at, state.last_pull_at),
                )
            )
        devices.sort(key=lambda d: d.name)
        if error:
            # Never claim the remote is ahead when it could not be read.
            remote_ahead = False
        elif remote_generation is None:
            remote_ahead = False
        else:
            floor = self._rollback_floor(state)
            if remote_generation < floor:
                error = (
                    f"the remote head is at generation {remote_generation}, behind this "
                    f"device's last sync point {floor}; the remote was rolled back or is "
                    "serving stale data and nothing from it will be applied"
                )
                remote_ahead = False
            else:
                remote_ahead = remote_generation != state.last_seen_generation
        local_cmp = _comparable(local_manifest)
        base_cmp = _comparable(state.base_files)
        # R1: base entries for paths this device CANNOT see under its own
        # rules and opt-ins are carried-forward fleet state, not local dirt —
        # this device can neither publish nor delete them, and push answers
        # "nothing to push" for exactly the same state. Counting them would
        # pin local_dirty=True forever on any device that ever pulled a
        # generation whose manifest carries such a path.
        invisible = set(
            self._invisible_base_entries(local_manifest, state.base_files, rules)
        )
        dirty_count = sum(
            1
            for key in set(local_cmp) | set(base_cmp)
            if key not in invisible and local_cmp.get(key) != base_cmp.get(key)
        )
        return Status(
            device=self.settings.device_name,
            backend=self.backend.name,
            local=state,
            remote_generation=remote_generation,
            remote_device=remote_device,
            remote_created_at=remote_created_at,
            remote_ahead=remote_ahead,
            local_dirty=dirty_count > 0,
            dirty_count=dirty_count,
            devices=devices,
            error=error,
            recovered=recovered,
            opt_ins=self._opt_ins(),
        )

    def _opt_ins(self) -> dict:
        """The optional sync items' on/off state (0.4.1): local settings,
        never remote-controlled text, shown by every status surface."""
        return {
            "session_archive": bool(self.settings.sync_sessions),
            "session_archive_max_mb": int(self.settings.sessions_max_mb),
            "cron_jobs": bool(self.settings.sync_cron),
        }

    def diff(self) -> Diff:
        """What a pull would change, grouped by scope class (matches PullResult.plan)."""
        self._arm(_SHORT_BUDGET)
        self._crash_recovery()
        state = self.load_state()
        pair = self._backend_call("get_head")
        if pair is None:
            raise BackendError(
                "no remote head to diff against; run `hermes roam init --create-remote` first"
            )
        signed = head_mod.verify_head(pair[0], self.settings.archive_key)
        floor = self._rollback_floor(state)
        if signed.generation < floor:
            raise _rollback_error(signed.generation, floor)
        rules, _, _, local_manifest = self._collect()
        added: dict[Scope, list[str]] = {}
        changed: dict[Scope, list[str]] = {}
        removed: dict[Scope, list[str]] = {}

        def group(target: dict[Scope, list[str]], scope: Scope, relpath: str) -> None:
            target.setdefault(scope, []).append(relpath)

        for relpath in sorted(set(local_manifest) | set(signed.files)):
            if relpath in (EXTRA_INSTALLED, MANIFEST_MEMBER):
                continue
            local_entry = local_manifest.get(relpath)
            remote_entry = signed.files.get(relpath)
            # this device's local rules classify everything a pull could touch
            scope = self.pipeline.classify(relpath, rules)
            # 0.4.1: session archive members are shown at the read-only install
            # path they would actually land at, never at the bundle namespace
            shown = archive_mod.install_relpath(relpath)
            if local_entry is None:
                # remote-only: a pull would add it
                group(added, scope, shown)
            elif remote_entry is None:
                # local-only: a pull would delete it only if roam managed it at the last sync
                if relpath in (state.base_files or {}):
                    group(removed, scope, shown)
            elif _comparable({relpath: local_entry})[relpath] != _comparable(
                {relpath: remote_entry}
            )[relpath]:
                group(changed, scope, shown)
        return Diff(added=added, changed=changed, removed=removed,
                    remote_generation=signed.generation)

    def estimate_bundle_bytes(self) -> int:
        """Upper-bound estimate of the next bundle size (zip + envelope overhead).

        0.4.1: with the session archive opted in, the export is capped at
        ``sessions_max_mb``, so the cap is the honest upper bound of what the
        archive can add on top of the walked home.
        """
        _, entries, extras, _ = self._collect()
        total = sum(entry.size for entry in entries) + sum(len(data) for data in extras.values())
        if self.settings.sync_sessions:
            total += self.settings.sessions_max_mb * 1024 * 1024
        members = len(entries) + len(extras) + 1
        return total + members * 1024 + 65536

    def push(self, *, force: bool = False, dry_run: bool = False, automatic: bool = False) -> PushResult:
        """Build and publish a bundle, then CAS the head. Blob first, head last.

        R9: with ``automatic=True`` (the session-end hook) the lock is taken
        non-blocking under the label "automatic push" and a busy lock is a
        skip, never a wait.
        """
        self._arm(self.settings.operation_timeout)
        if automatic:
            with self._process_lock(what="automatic push", skip_when_busy=True) as acquired:
                if not acquired:
                    return PushResult(
                        generation=0,
                        bundle_key="",
                        bundle_size=0,
                        file_count=0,
                        dry_run=dry_run,
                        skipped_reason=(
                            "another roam command holds this home's lock; the "
                            "automatic push was skipped"
                        ),
                    )
                return self._push(force=force, dry_run=dry_run)
        with self._process_lock(what="push"):
            return self._push(force=force, dry_run=dry_run)

    def _session_archive_extras(self) -> tuple[dict[str, bytes], list[str], bool]:
        """The opt-in session archive members a push would bundle.

        With ``sync_sessions`` enabled, EVERY home of the ROOT home is
        exported by Hermes's own documented CLI — ``hermes sessions export
        --format md --redact`` — run as a disclosed argv subprocess with
        ``HERMES_HOME`` set to the home being exported (the ROOT home as
        profile ``default``, then each ``profiles/<name>/``), stdin closed
        and a hard timeout inside the armed operation deadline. All
        profiles share one cap (``sessions_max_mb``, NEWEST first). Any
        failure degrades to a clear notice: the push of everything else
        still succeeds and the previously published archive is kept by the
        caller.
        """
        if not self.settings.sync_sessions:
            return {}, [], True
        notes: list[str] = []
        failures: list[str] = []
        entries: list[dict[str, Any]] = []
        scratch = self._roam_subdir("tmp") / f"sess-{uuid.uuid4().hex}"
        try:
            hermes_exe = archive_mod.find_hermes()
            if not hermes_exe:
                failures.append("the hermes executable was not found on PATH")
            else:
                for profile, home_dir in archive_mod.export_homes(self.home):
                    remaining = (self._deadline or 0.0) - time.monotonic()
                    if remaining <= 1.0:
                        failures.append(
                            "the operation deadline left no time for the export"
                        )
                        break
                    profile_entries, reason = self.pipeline.export_sessions(
                        home_dir,
                        scratch / profile,
                        hermes_exe,
                        min(remaining, archive_mod.EXPORT_TIMEOUT_CAP),
                    )
                    if reason:
                        failures.append(f"profile {profile}: {reason}")
                        continue
                    entries.extend({"profile": profile, **item} for item in profile_entries)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if failures:
            notes.append(
                "the session archive could not be exported ("
                + "; ".join(failures)
                + "); this push continues without publishing session transcripts "
                "and keeps the previously published archive"
            )
            return {}, notes, False
        kept, dropped = archive_mod.cap_entries(
            entries, self.settings.sessions_max_mb * 1024 * 1024
        )
        extras: dict[str, bytes] = {}
        for entry in kept:
            try:
                relpath = archive_mod.bundle_relpath(
                    self.settings.device_name,
                    str(entry["profile"]),
                    str(entry["name"]),
                )
            except (KeyError, ValueError):
                continue
            extras[relpath] = entry["data"]
        if dropped:
            notes.append(
                f"the session archive cap (sessions_max_mb="
                f"{self.settings.sessions_max_mb}) dropped {dropped} older "
                "session(s) from this push"
            )
        covered = sorted({str(entry["profile"]) for entry in kept}) or ["none"]
        notes.append(
            f"published {len(extras)} session transcript(s) covering profile(s) "
            f"{', '.join(covered)} in this device's read-only archive namespace "
            f"({archive_mod.ARCHIVE_PREFIX}{self.settings.device_name}/)"
        )
        return extras, notes, True

    def _push(self, *, force: bool = False, dry_run: bool = False) -> PushResult:
        # R10: the init gate lives in the engine, so every surface inherits it.
        if not (self._roam_dir() / STATE_FILE).is_file():
            raise RoamError("no local roam state — run `hermes roam init` first")
        # R6: the device name is validated exactly like a head's device field.
        if not head_mod.is_valid_device_name(self.settings.device_name):
            raise ConfigError(
                "settings.device_name must match ^[a-z0-9][a-z0-9-]{0,39}$ "
                f"(got {self.settings.device_name!r})"
            )
        recovered = self._crash_recovery()
        state = self.load_state()
        pair = self._backend_call("get_head")
        remote_head: Head | None = None
        if pair is not None:
            remote_head = head_mod.verify_head(pair[0], self.settings.archive_key)
        remote_generation = remote_head.generation if remote_head else 0
        if not isinstance(remote_generation, int):
            raise VerifyError("the remote head carries a non-integer generation")
        floor = self._rollback_floor(state)
        if remote_generation < floor:
            raise _rollback_error(remote_generation, floor)
        if remote_generation != state.last_seen_generation and not force:
            raise RemoteAhead(
                f"the remote head is at generation {remote_generation} but this device last "
                f"saw {state.last_seen_generation}; pull first (or push with --force)"
            )
        rules, entries, extras, manifest = self._collect()
        # 0.4.1: the opt-in session archive — generated bundle members, never
        # walked files. A failed export degrades to a notice; the caller then
        # keeps this device's previously published archive (below).
        session_extras, session_notes, session_export_ok = self._session_archive_extras()
        extras.update(session_extras)
        for relpath, data in session_extras.items():
            manifest[relpath] = {
                "sha256": _sha256_bytes(data),
                "size": len(data),
                "mode": 0o444,  # read-only wherever it lands
                "scope": Scope.SESSIONS.value,
            }
        # R1: base entries this device cannot see under its own rules and
        # opt-ins are carried forward byte-identical from the last synced
        # bundle — only a device that can see a path can delete it.
        carried = self._invisible_base_entries(manifest, state.base_files, rules)
        # 0.4.1 namespace policy: a device may only replace its OWN
        # archive/sessions/<device>/ namespace. Other devices' namespaces are
        # carry-forward fleet state; this device's own namespace follows the
        # opt-in — a successful export replaces it, a FAILED export keeps the
        # published archive, and turning sync_sessions off unpublishes it.
        own_prefix = f"{archive_mod.ARCHIVE_PREFIX}{self.settings.device_name}/"
        own_carried = [relpath for relpath in carried if relpath.startswith(own_prefix)]
        carried = [relpath for relpath in carried if not relpath.startswith(own_prefix)]
        if self.settings.sync_sessions and not session_export_ok:
            carried = sorted(set(carried) | set(own_carried))
        if carried:
            if not self._last_roam_path().is_file():
                raise CarryForwardError(
                    f"this push must carry {len(carried)} path(s) this device "
                    f"cannot see (e.g. {carried[0]!r}) from "
                    f"{self._last_roam_path()}, but that file is missing — "
                    "run `hermes roam pull` first"
                )
            for relpath in carried:
                entry = state.base_files.get(relpath)
                if isinstance(entry, dict):
                    manifest[relpath] = dict(entry)
        if not force and _comparable(manifest) == _comparable(state.base_files):
            return PushResult(
                generation=remote_generation,
                bundle_key="",
                bundle_size=0,
                file_count=len(manifest),
                dry_run=dry_run,
                skipped_reason="nothing to push",
                notes=session_notes,
                recovered=recovered,
            )
        if dry_run:
            # R11: the dry run reports the real estimate, carried content included.
            # 0.5.1: the session archive is already exported at this point, so
            # the manifest holds every member's real size — no need for the
            # sessions_max_mb upper bound that doctor's pre-flight estimate uses.
            manifest_bytes = sum(
                int((entry or {}).get("size") or 0) for entry in manifest.values()
            )
            return PushResult(
                generation=remote_generation + 1,
                bundle_key="",
                bundle_size=manifest_bytes + (len(manifest) + 1) * 1024 + 65536,
                file_count=len(manifest),
                files=sorted(manifest),
                notes=session_notes,
                dry_run=True,
                recovered=recovered,
            )
        if carried:
            extras.update(self._carry_forward_bytes(carried, state.base_files))
        now = self.clock()
        generation = remote_generation + 1
        parent = remote_head.generation if remote_head else -1
        # 0.4 head chaining: the signed head and the bundle header both name
        # the previous head's bundle key so the store chain is verifiable.
        parent_bundle_key = remote_head.bundle_key if remote_head else ""
        tmp_dir = self._roam_subdir("tmp")
        bundle_path = tmp_dir / f"bundle-{generation}.roam"
        info = self.pipeline.build_bundle(
            self.home,
            entries,
            bundle_path,
            key=self.settings.archive_key,
            header={
                "v": 1,
                "device": self.settings.device_name,
                "generation": generation,
                "created_at": now,
                "parent_bundle_key": parent_bundle_key,
            },
            extra_files=extras,
        )
        bundle_key = head_mod.bundle_key_for(
            generation, self.settings.device_name, info.plain_sha256
        )
        # fail closed before anything is stored: a bundle over the cap would be
        # stored but refused by every get_blob/open_bundle forever, bricking all
        # pulls on all devices with no rollback path
        bundle_size = bundle_path.stat().st_size
        limit = self.settings.max_bundle_bytes
        if bundle_size > limit:
            bundle_path.unlink(missing_ok=True)
            raise ArchiveError(
                f"the bundle is {bundle_size} bytes and exceeds the {limit} byte "
                "limit; nothing was stored — exclude files via roam rules.yaml "
                "or raise max_bundle_bytes"
            )
        try:
            try:
                self._backend_call("put_blob", bundle_key, bundle_path)
            except BlobExists as exc:
                raise RemoteAhead(
                    f"a bundle from an earlier attempt of generation {generation} already "
                    "exists on the remote; pull, then push again"
                ) from exc
            devices = dict(remote_head.devices) if remote_head else {}
            devices[self.settings.device_name] = {
                "last_push_generation": generation,
                # This device's own last pull from LOCAL state: pulls never
                # write the roster, so the next push must carry the truth —
                # copying the previous remote entry showed "pulled 0" forever.
                "last_pull_generation": state.last_pulled_generation,
                "hermes_version": self.hermes_version,
                "platform": sys.platform,
                "seen_at": now,
            }
            new_head = Head(
                generation=generation,
                device=self.settings.device_name,
                hermes_version=self.hermes_version,
                created_at=now,
                bundle_key=bundle_key,
                bundle_sha256=info.sha256,
                bundle_size=info.size,
                plain_sha256=info.plain_sha256,
                files=manifest,
                parent_generation=parent,
                parent_bundle_key=parent_bundle_key,
                devices=devices,
            )
            payload = head_mod.encode_head(
                head_mod.sign_head(new_head, self.settings.archive_key)
            )
            try:
                self._backend_call("put_head", payload, pair[1] if pair else None)
            except HeadConflict:
                raise RemoteAhead(
                    f"another device pushed first ({self._other_device()}); pull, then push again"
                ) from None
            # R1: the published bundle becomes this device's carry-forward
            # source; R4: the published generation raises the rollback floor.
            self._save_last_roam(bundle_path)
            self._raise_floor(generation)
        finally:
            bundle_path.unlink(missing_ok=True)
        updated = state.to_dict()
        # 0.4.2: a push publishes this device's own stripped config, so the
        # published shared projections become the accepted config base — a
        # publisher must never be treated as never-synced for configs.
        published_config_base = dict(state.config_base_digests or {})
        projection = self._shared_projection_fn(self._host_keys(rules))
        for relpath, data in extras.items():
            if _is_config_path(relpath):
                # the canonical digest of the published stripped projection
                # — the same function a pull uses, so publisher and puller
                # agree on one accepted base (0.4.2)
                published_config_base[relpath] = projection(
                    data.decode("utf-8", errors="replace")
                )
        updated.update(
            last_pushed_generation=generation,
            last_seen_generation=generation,
            last_push_at=now,
            base_files=manifest,
            config_base_digests=published_config_base,
        )
        self.save_state(LocalState.from_dict(updated))
        return PushResult(
            generation=generation,
            bundle_key=bundle_key,
            bundle_size=info.size,
            file_count=len(manifest),
            files=sorted(manifest),
            notes=session_notes,
            dry_run=False,
            recovered=recovered,
        )

    def pull(
        self,
        *,
        apply: bool = False,
        classes: set[Scope] | None = None,
        dry_run: bool = False,
        include_prefixes: list[str] | None = None,
        allow_rollback: bool = False,
        overwrite_conflicts: bool = False,
    ) -> PullResult:
        """Download, verify, stage and (with apply=True) install the remote generation.

        ``include_prefixes`` restricts the pulled file set to relpaths inside
        the given prefixes; everything else (always including config.yaml) is
        left for a full pull and the local seen-generation is NOT advanced.
        ``allow_rollback`` accepts a validly signed head that is behind this
        device's last sync point; without it such a head is refused.
        ``overwrite_conflicts`` lets the install overwrite local files that
        diverged since the last sync point (a backup is kept either way).
        """
        self._arm(self.settings.operation_timeout)
        with self._process_lock(what="pull"):
            return self._pull(
                apply=apply,
                classes=classes,
                dry_run=dry_run,
                include_prefixes=include_prefixes,
                allow_rollback=allow_rollback,
                overwrite_conflicts=overwrite_conflicts,
            )

    def _pull(
        self,
        *,
        apply: bool,
        classes: set[Scope] | None,
        dry_run: bool,
        include_prefixes: list[str] | None,
        allow_rollback: bool,
        overwrite_conflicts: bool,
    ) -> PullResult:
        recovered = self._crash_recovery()
        state = self.load_state()
        pair = self._backend_call("get_head")
        if pair is None:
            raise BackendError(
                "no remote head to pull; run `hermes roam init --create-remote` on one device"
            )
        signed = head_mod.verify_head(pair[0], self.settings.archive_key)
        floor = self._rollback_floor(state)
        if signed.generation < floor and not allow_rollback:
            raise _rollback_error(signed.generation, floor)
        # R4: this device has now observed this generation on this remote.
        self._raise_floor(signed.generation)
        self._check_version(signed.hermes_version)
        if not signed.bundle_key:
            empty = Diff({}, {}, {}, signed.generation)
            return PullResult(
                generation=signed.generation,
                applied=False,
                plan=empty,
                notes=["the remote head has no bundle yet (generation 0); nothing to pull"],
                recovered=recovered,
            )
        selected = set(classes) if classes else self._pull_default_classes()
        rules = self.pipeline.load_rules(self.home)
        if dry_run:
            apply = False
            with tempfile.TemporaryDirectory(prefix="roam-pull-") as scratch:
                return self._pull_once(
                    state,
                    signed,
                    rules,
                    selected,
                    download_dir=Path(scratch),
                    staging_dir=Path(scratch) / "staging",
                    apply=False,
                    include_prefixes=include_prefixes,
                    recovered=recovered,
                    overwrite_conflicts=overwrite_conflicts,
                )
        staging_root = self._roam_subdir("staging")
        staging_dir = staging_root / str(signed.generation)
        return self._pull_once(
            state,
            signed,
            rules,
            selected,
            download_dir=self._roam_subdir("tmp"),
            staging_dir=staging_dir,
            apply=apply,
            include_prefixes=include_prefixes,
            recovered=recovered,
            overwrite_conflicts=overwrite_conflicts,
        )

    def init(self, *, create_remote: bool) -> str:
        """Validate settings, ping the backend, optionally create the generation-0 head."""
        self._arm(_SHORT_BUDGET)
        # crash recovery runs at the start of every command — init included
        self._crash_recovery()
        key = self.settings.archive_key
        if len(key) != 32:
            raise ConfigError(
                "ROAM_ARCHIVE_KEY must decode to exactly 32 bytes (base64 or 64 hex characters)"
            )
        if len(set(key)) == 1:
            raise ConfigError(
                "weak key: ROAM_ARCHIVE_KEY decodes to 32 identical bytes and has no "
                "entropy; generate a real key with "
                "'python -c \"import secrets;print(secrets.token_urlsafe(32))\"'"
            )
        # R6: heads with invalid device fields are rejected wholesale, so a
        # device name that would not verify can never initialise at all.
        if not head_mod.is_valid_device_name(self.settings.device_name):
            raise ConfigError(
                "settings.device_name must match ^[a-z0-9][a-z0-9-]{0,39}$ "
                f"(got {self.settings.device_name!r})"
            )
        required = {"s3": "s3_bucket", "git": "git_remote", "local": "local_dir"}
        if self.settings.backend in required and not getattr(
            self.settings, required[self.settings.backend]
        ):
            raise ConfigError(
                f"the {self.settings.backend} backend requires settings.roam."
                f"{required[self.settings.backend]}"
            )
        ping = self._backend_call("ping")
        pair = self._backend_call("get_head")
        if pair is not None:
            signed = head_mod.verify_head(pair[0], self.settings.archive_key)
            self.save_state(self.load_state())
            return (
                f"connected to the {self.backend.name} backend ({ping}); the remote head is at "
                f"generation {signed.generation}, last pushed by '{signed.device}'; pull before "
                "pushing anything"
            )
        if not create_remote:
            self.save_state(self.load_state())
            return (
                "no remote head found; run `hermes roam init --create-remote` on the first "
                "device to create one"
            )
        now = self.clock()
        head0 = Head(
            generation=0,
            device=self.settings.device_name,
            hermes_version=self.hermes_version,
            created_at=now,
            devices={
                self.settings.device_name: {
                    "last_push_generation": 0,
                    "last_pull_generation": 0,
                    "hermes_version": self.hermes_version,
                    "platform": sys.platform,
                    "seen_at": now,
                }
            },
        )
        payload = head_mod.encode_head(head_mod.sign_head(head0, self.settings.archive_key))
        try:
            self._backend_call("put_head", payload, None)
        except HeadConflict as exc:
            raise RemoteAhead(
                "another device created the remote head first; pull instead of initialising"
            ) from exc
        self.save_state(self.load_state())
        return (
            f"created the generation-0 remote head on the {self.backend.name} backend and "
            f"registered device '{self.settings.device_name}'"
        )

    def doctor(self) -> list[Check]:
        """The CONTRACT.md checks as Check objects."""
        self._arm(_SHORT_BUDGET)
        recovered = self._crash_recovery()
        checks: list[Check] = [Check(
            "crash recovery",
            recovered is None,
            "no interrupted installs to roll back"
            if recovered is None
            else f"an interrupted install was rolled back at the start of this check: {recovered}",
        )]
        key = self.settings.archive_key
        key_ok = len(key) == 32 and len(set(key)) > 1
        if key_ok:
            key_detail = "ROAM_ARCHIVE_KEY decodes to 32 bytes"
        elif len(key) == 32:
            key_detail = (
                "weak key: ROAM_ARCHIVE_KEY decodes to 32 identical bytes and has no "
                "entropy; generate a real key with "
                "'python -c \"import secrets;print(secrets.token_urlsafe(32))\"'"
            )
        else:
            key_detail = "ROAM_ARCHIVE_KEY must decode to exactly 32 bytes (base64 or 64 hex chars)"
        checks.append(Check("archive key", key_ok, key_detail))
        try:
            ping = self._backend_call("ping")
            checks.append(Check("backend reachability", True, ping))
        except BackendError as exc:
            checks.append(Check("backend reachability", False, str(exc)))
        remote_generation: int | None = None
        remote_version = ""
        head_ok = False
        head_detail = "no remote head exists yet"
        try:
            pair = self._backend_call("get_head")
            if pair is not None:
                signed = head_mod.verify_head(pair[0], self.settings.archive_key)
                remote_generation = signed.generation
                remote_version = signed.hermes_version
                head_ok = True
                head_detail = f"remote head signature verifies (generation {signed.generation})"
        except (BackendError, VerifyError) as exc:
            head_detail = str(exc)
        checks.append(Check("remote head signature", head_ok, head_detail))
        # R4: doctor must see what pull/push see — a remote head that was
        # rolled back below this device's floor bricks every sync command.
        floor_ok = True
        floor_detail = "the remote head is not behind this device's rollback floor"
        if remote_generation is not None:
            floor = self._rollback_floor()
            if remote_generation < floor:
                floor_ok = False
                floor_detail = (
                    f"the remote head is at generation {remote_generation}, behind this "
                    f"device's last sync point {floor}; the remote was rolled back or "
                    "is serving stale data — confirm the rollback with your fleet "
                    "before accepting it (pull with allow_rollback)"
                )
        checks.append(Check("rollback floor", floor_ok, floor_detail))
        if remote_generation is None:
            checks.append(Check("hermes version", True, "no remote head to compare versions with"))
        elif self._newer_remote(remote_version):
            checks.append(
                Check(
                    "hermes version",
                    False,
                    f"the remote head was pushed by Hermes {remote_version} but this device "
                    f"runs {self.hermes_version}; upgrade Hermes before pulling",
                )
            )
        else:
            checks.append(
                Check("hermes version", True, f"remote Hermes version {remote_version} is compatible")
            )
        estimate = self.estimate_bundle_bytes()
        limit = self.settings.max_bundle_bytes
        checks.append(
            Check(
                "bundle size",
                estimate <= limit,
                f"estimated bundle is {estimate} bytes of the {limit} byte limit"
                if estimate <= limit
                else f"estimated bundle is {estimate} bytes and exceeds the {limit} byte limit; "
                "exclude files via roam rules.yaml",
            )
        )
        for name in ("staging", "backups"):
            probe = self._roam_subdir(name) / ".roam-probe"
            try:
                probe.write_bytes(b"probe")
                probe.unlink()
                checks.append(Check(f"{name} dir writable", True, f"{name} directory is writable"))
            except OSError as exc:
                checks.append(Check(f"{name} dir writable", False, str(exc)))
        if self.settings.backend == "git":
            present = shutil.which("git") is not None
            checks.append(
                Check(
                    "git present",
                    present,
                    "git is installed"
                    if present
                    else "the git backend needs the git CLI installed",
                )
            )
        return checks

    def recover(self) -> list[str]:
        """Roll back any install journal left mid-flight by a crash."""
        self._arm(_SHORT_BUDGET)
        with self._process_lock(what="recover"):
            return restore.recover(self.home, self._roam_subdir("journals"))

    # ------------------------------------------------- 0.4 history/restore

    def _verified_remote_head(self) -> Head | None:
        """The current remote head, signature-checked; None when absent."""
        pair = self._backend_call("get_head")
        if pair is None:
            return None
        return head_mod.verify_head(pair[0], self.settings.archive_key)

    def _store_bundle_keys(self, signed: Head | None) -> list[str]:
        """Candidate bundle keys: the signed chain anchors plus every key the
        store lists under ``bundles/``. Backends without a listing surface
        contribute nothing (pre-0.4 stores stop at the head's own bundle)."""
        keys: list[str] = []
        if signed is not None:
            for anchor in (
                signed.bundle_key,
                getattr(signed, "parent_bundle_key", "") or "",
            ):
                if anchor and anchor not in keys:
                    keys.append(anchor)
        try:
            listed = self._backend_call("list_blobs", "bundles/")
        except AttributeError:
            listed = []
        for key in listed:
            if key not in keys:
                keys.append(key)
        return keys

    def _peek_store_bundle(self, key: str, scratch_root: Path) -> tuple[Path, dict] | None:
        """Download one store bundle and read its (still unauthenticated)
        header; None when the key is unreadable or does not match its own
        header. Trust is established only by _open_store_bundle."""
        bundle_path = scratch_root / f"bundle-{uuid.uuid4().hex}.roam"
        try:
            self._backend_call(
                "get_blob", key, bundle_path, max_bytes=self.settings.max_bundle_bytes
            )
        except BackendError:
            return None
        try:
            header = self.pipeline.read_header(bundle_path)
        except (OSError, RoamError):
            bundle_path.unlink(missing_ok=True)
            return None
        generation = header.get("generation")
        device = header.get("device")
        plain_sha256 = header.get("plain_sha256")
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 0
            or not head_mod.is_valid_device_name(device)
            or not isinstance(plain_sha256, str)
        ):
            bundle_path.unlink(missing_ok=True)
            return None
        try:
            if head_mod.bundle_key_for(generation, device, plain_sha256) != key:
                bundle_path.unlink(missing_ok=True)
                return None
        except ValueError:
            bundle_path.unlink(missing_ok=True)
            return None
        return bundle_path, header

    def _open_store_bundle(
        self, key: str, bundle_path: Path, header: dict, scratch_root: Path
    ) -> dict:
        """Fully verify one downloaded store bundle (AES-GCM tag + plaintext
        digest) and extract it; VerifyError when it is not this fleet's data."""
        staging = scratch_root / f"open-{uuid.uuid4().hex}"
        try:
            manifest = self.pipeline.open_bundle(
                bundle_path,
                key=self.settings.archive_key,
                dest_dir=staging,
                limits=self._archive_limits(),
            )
        except (OSError, RoamError) as exc:
            bundle_path.unlink(missing_ok=True)
            raise VerifyError(
                f"bundle {key} failed verification with this fleet's key and is not trusted "
                f"({type(exc).__name__})"
            ) from exc
        bundle_path.unlink(missing_ok=True)
        created_at = header.get("created_at")
        return {
            "key": key,
            "generation": header.get("generation"),
            "device": header.get("device"),
            "created_at": created_at if isinstance(created_at, (int, float)) else 0.0,
            "parent_bundle_key": header.get("parent_bundle_key") or "",
            "manifest": manifest if isinstance(manifest, dict) else {},
            "staging": staging,
        }

    def history(self, path: str | None = None, *, limit: int = 50) -> list[dict]:
        """Every version the remote store still holds and this fleet's key
        verifies, newest generation first: one entry per path change as
        {generation, device, created_at, path, sha256, size, deleted}.

        Entries come only from bundles that decrypt and self-match under the
        archive key; unsigned or undecryptable bundles are never trusted. The
        signed head anchors the chain (its parent bundle key must match the
        previous generation's bundle when both exist); stores that predate
        head chaining fall back to the backend's bundle listing.
        """
        limit = max(1, min(int(limit), 500))
        if path is not None:
            restore._safe_relpath(path)
        self._arm(self.settings.operation_timeout)
        self._crash_recovery()
        signed = self._verified_remote_head()
        if signed is None:
            raise BackendError(
                "no remote head; run `hermes roam init --create-remote` on one device"
            )
        scratch_root = self._roam_subdir("tmp") / f"history-{uuid.uuid4().hex}"
        scratch_root.mkdir(parents=True, exist_ok=True)
        verified: list[dict] = []
        try:
            for key in self._store_bundle_keys(signed):
                peeked = self._peek_store_bundle(key, scratch_root)
                if peeked is None:
                    continue
                bundle_path, header = peeked
                try:
                    verified.append(
                        self._open_store_bundle(key, bundle_path, header, scratch_root)
                    )
                except VerifyError:
                    continue
        finally:
            shutil.rmtree(scratch_root, ignore_errors=True)
        # A bundle above the signed head was never published (a lost CAS race
        # leaves it in the store); it is not part of the chain.
        index = {
            record["generation"]: record
            for record in verified
            if isinstance(record["generation"], int)
            and record["generation"] <= signed.generation
        }
        for generation in sorted(index):
            parent_key = index[generation]["parent_bundle_key"]
            previous = index.get(generation - 1)
            if parent_key and previous is not None and previous["key"] != parent_key:
                raise VerifyError(
                    f"the remote bundle chain does not verify: generation {generation}'s "
                    "signed parent pointer does not match the previous generation's bundle"
                )
        head_parent = getattr(signed, "parent_bundle_key", "") or ""
        if head_parent:
            previous = index.get(signed.generation - 1)
            if previous is not None and previous["key"] != head_parent:
                raise VerifyError(
                    "the remote bundle chain does not verify: the signed head's parent "
                    "pointer does not match the previous generation's bundle"
                )

        def include(relpath: object) -> bool:
            if not isinstance(relpath, str) or relpath in (MANIFEST_MEMBER, EXTRA_INSTALLED):
                return False
            if _is_config_path(relpath):
                return False  # configs travel stripped and merge key-by-key
            if archive_mod.is_archive_relpath(relpath):
                return False  # read-only archive members are not restorable
            return path is None or relpath == path

        def entry(record: dict, relpath: str, content: dict, *, deleted: bool) -> dict:
            return {
                "generation": record["generation"],
                "device": record["device"],
                "created_at": record["created_at"],
                "path": relpath,
                "sha256": str(content.get("sha256") or ""),
                "size": int(content.get("size") or 0),
                "deleted": deleted,
            }

        entries: list[dict] = []
        for generation in sorted(index, reverse=True):
            record = index[generation]
            previous = index.get(generation - 1)
            if previous is None:
                # No verifiable predecessor (chain start or a pruned gap):
                # every member of this bundle is a restorable version.
                for relpath in sorted(record["manifest"]):
                    if include(relpath):
                        entries.append(
                            entry(record, relpath, record["manifest"][relpath], deleted=False)
                        )
                continue
            for relpath in sorted(previous["manifest"]):
                if include(relpath) and relpath not in record["manifest"]:
                    entries.append(
                        entry(record, relpath, previous["manifest"][relpath], deleted=True)
                    )
            for relpath in sorted(record["manifest"]):
                if not include(relpath):
                    continue
                old = previous["manifest"].get(relpath)
                new = record["manifest"][relpath]
                if not isinstance(old, dict) or old.get("sha256") != new.get("sha256"):
                    entries.append(entry(record, relpath, new, deleted=False))
            if len(entries) >= limit:
                break
        return entries[:limit]

    def restore(self, path: str, *, generation: int, apply: bool = False) -> dict:
        """Recover one file version from the remote store into this home.

        With ``apply=False`` nothing is written: the result reports the
        version that would be restored. With ``apply=True`` the version goes
        through the normal journaled install (a backup of the current file is
        taken first) and is marked as a local change — state.json is not
        touched, so the next push publishes it. Paths that are LOCAL or
        IGNORED under this device's rules are refused; configs are never
        written verbatim: their shared projection merges with this host's
        local keys.
        """
        self._arm(self.settings.operation_timeout)
        with self._process_lock(what="restore"):
            return self._restore(path, generation=generation, apply=apply)

    def _restore(self, path: str, *, generation: int, apply: bool) -> dict:
        recovered = self._crash_recovery()
        restore._safe_relpath(path)
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise RoamError(
                "generation must be a positive integer; run `hermes roam history` "
                "for the recoverable list"
            )
        rules = self.pipeline.load_rules(self.home)
        if archive_mod.is_archive_relpath(path):
            raise RoamError(
                "restore refuses session archive paths: archived transcripts are "
                "read-only — a pull installs them under "
                "<home>/roam/session-archive/, and `hermes roam sessions` lists "
                "them; there is nothing to restore into the home"
            )
        scope = self.pipeline.classify(path, rules)
        if scope in (Scope.LOCAL, Scope.IGNORED) or scope not in self._pull_default_classes():
            raise RoamError(
                f"restore refuses this path: {path!r} is {scope.value} under this "
                "device's rules; only shared or opted-in paths can be restored"
            )
        signed = self._verified_remote_head()
        if signed is None:
            raise BackendError(
                "no remote head; run `hermes roam init --create-remote` on one device"
            )
        if generation > signed.generation:
            raise RoamError(
                f"generation {generation} is ahead of the published chain (the remote "
                f"head is at {signed.generation}); that bundle was never published"
            )
        scratch_root = self._roam_subdir("tmp") / f"restore-{uuid.uuid4().hex}"
        scratch_root.mkdir(parents=True, exist_ok=True)
        try:
            target = self._open_generation_bundle(generation, signed, scratch_root)
            return self._restore_from(
                target, path, scope, rules, apply=apply, recovered=recovered
            )
        finally:
            shutil.rmtree(scratch_root, ignore_errors=True)

    def _open_generation_bundle(
        self, generation: int, signed: Head, scratch_root: Path
    ) -> dict:
        """The fully verified store bundle of ``generation``; RoamError when
        no bundle of that generation is recoverable, VerifyError when the
        only candidate does not verify with this fleet's key."""
        for key in self._store_bundle_keys(signed):
            peeked = self._peek_store_bundle(key, scratch_root)
            if peeked is None:
                continue
            bundle_path, header = peeked
            if header.get("generation") != generation:
                bundle_path.unlink(missing_ok=True)
                continue
            return self._open_store_bundle(key, bundle_path, header, scratch_root)
        raise RoamError(
            f"generation {generation} is not recoverable: no bundle of that "
            "generation exists in the remote store; run `hermes roam history` "
            "for the recoverable list"
        )

    def _restore_from(
        self,
        target: dict,
        path: str,
        scope: Scope,
        rules: object,
        *,
        apply: bool,
        recovered: str | None,
    ) -> dict:
        manifest_entry = target["manifest"].get(path)
        staged = target["staging"] / path
        if _is_config_path(path):
            if not staged.is_file():
                raise RoamError(
                    f"generation {target['generation']}'s bundle does not carry "
                    f"{path!r}; nothing to restore"
                )
            plan = restore.InstallPlan(configs=((path, staged, "merge"),))
            sha = ""
            size = 0
        else:
            if not isinstance(manifest_entry, dict) or not staged.is_file():
                raise RoamError(
                    f"{path!r} has no version in generation {target['generation']}'s "
                    "bundle; run `hermes roam history` for the recoverable list"
                )
            size, sha = _hash_file(staged)
            if sha != manifest_entry.get("sha256") or size != manifest_entry.get("size"):
                raise VerifyError(
                    "the restored member does not match its bundle manifest"
                )
            mode = int(manifest_entry.get("mode") or 0o644) & 0o777
            plan = restore.InstallPlan(
                to_write=(restore.PlannedFile(path, staged, sha, size, mode, scope),)
            )
        info = {
            "path": path,
            "generation": target["generation"],
            "device": target["device"],
            "created_at": target["created_at"],
            "sha256": sha,
            "size": size,
            "applied": False,
            "backup_dir": "",
            "files_written": 0,
            "config_merged": False,
            "recovered": recovered,
        }
        if not apply:
            return info
        backup_dir = self._roam_subdir("backups") / time.strftime(
            "%Y%m%d-%H%M%S", time.gmtime(self.clock())
        )
        applied = restore.apply(
            self.home,
            plan,
            journal_dir=self._roam_subdir("journals"),
            backup_dir=backup_dir,
            merge_config_fn=self.pipeline.merge_config,
            host_keys=self._host_keys(rules),
        )
        self._prune_backups()
        info.update(
            applied=True,
            backup_dir=applied.backup_dir,
            files_written=applied.files_written,
            config_merged=applied.config_merged,
        )
        return info

    def _crash_recovery(self) -> str | None:
        """Consume crash journals at the start of every entry point.

        A power loss or SIGKILL mid pull --apply leaves a PHASE_INSTALLING
        journal and a half-applied home; the next roam command must roll the
        home back before doing anything else. Returns a human summary of what
        was rolled back, or None when there was nothing to recover.
        """
        outcomes = restore.recover(self.home, self._roam_subdir("journals"))
        if not outcomes:
            return None
        return "; ".join(outcomes)

    # ------------------------------------------------------------------ helpers

    def _pull_once(
        self,
        state: LocalState,
        signed: Head,
        rules: object,
        classes: set[Scope],
        *,
        download_dir: Path,
        staging_dir: Path,
        apply: bool,
        include_prefixes: list[str] | None = None,
        recovered: str | None = None,
        overwrite_conflicts: bool = False,
    ) -> PullResult:
        download_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = download_dir / f"bundle-{signed.generation}.roam"
        try:
            self._backend_call(
                "get_blob", signed.bundle_key, bundle_path, max_bytes=self.settings.max_bundle_bytes
            )
            size, digest = _hash_file(bundle_path)
            if size != signed.bundle_size or digest != signed.bundle_sha256:
                raise VerifyError("the downloaded bundle does not match the signed head")
            envelope_header = self.pipeline.read_header(bundle_path)
            if envelope_header.get("plain_sha256") != signed.plain_sha256:
                raise VerifyError("the bundle plaintext digest does not match the signed head")
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
            raw_manifest = self.pipeline.open_bundle(
                bundle_path,
                key=self.settings.archive_key,
                dest_dir=staging_dir,
                limits=self._archive_limits(),
            )
            manifest: dict[str, dict] = {}
            skipped_local: list[str] = []
            unapproved: list[str] = []
            archive_planned = 0
            # The planning manifest is the SIGNED HEAD's file list, not the
            # bundle's inner zip manifest: carried members (R1) and the
            # stripped configs travel as bundle members outside the zip
            # manifest, and nothing the head did not approve is ever planned.
            for relpath in sorted(set(raw_manifest) | set(signed.files)):
                if relpath == MANIFEST_MEMBER or not _prefix_allowed(relpath, include_prefixes):
                    continue
                entry = signed.files.get(relpath)
                if entry is None:
                    # present in the bundle but not approved by the signed head
                    unapproved.append(relpath)
                    skipped_local.append(relpath)
                    continue
                if not isinstance(entry, dict):
                    continue
                # This device's own rules classify every remote path: a scope the
                # remote manifest claims never wins over local rules.yaml.
                scope = self.pipeline.classify(relpath, rules)
                if _is_config_path(relpath):
                    # the stripped config is routed to the plan's config merge
                    # (never written verbatim); it is listed in the plan (R11)
                    manifest[relpath] = {**entry, "scope": scope.value}
                    continue
                if scope in (Scope.LOCAL, Scope.IGNORED) or scope not in classes:
                    skipped_local.append(relpath)
                    continue
                # 0.4.1: session archive members never land in the home tree.
                # The staged member is relocated to its read-only install path
                # (<home>/roam/session-archive/<device>/...): everything under
                # roam/ is LOCAL, so a receiver can never re-push them.
                install_path = archive_mod.install_relpath(relpath)
                staged_member = staging_dir / relpath
                if install_path != relpath and staged_member.is_file():
                    target_member = staging_dir / install_path
                    target_member.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged_member, target_member)
                    staged_member = target_member
                    archive_planned += 1
                if staged_member.is_file():
                    manifest[install_path] = {**entry, "scope": scope.value}
                else:
                    from .textsafe import untrusted

                    raise VerifyError(
                        "the remote head lists a path the bundle does not carry: "
                        + untrusted(relpath, 60)
                    )
            base_manifest: dict[str, dict] = {}
            for relpath, entry in (state.base_files or {}).items():
                if not _prefix_allowed(relpath, include_prefixes):
                    continue
                enriched = dict(entry) if isinstance(entry, dict) else {}
                # classification on the HEAD's path; the install key is the
                # read-only remap for session archive members (0.4.1)
                enriched["scope"] = self.pipeline.classify(relpath, rules).value
                base_manifest[archive_mod.install_relpath(relpath)] = enriched
            for relpath, enriched in base_manifest.items():
                if relpath in manifest:
                    continue  # already classified (and possibly refused) above
                # the scope was classified on the HEAD's path above; for session
                # archive members the install key lives under roam/ and would
                # misclassify (0.4.1), so the stored verdict is authoritative
                try:
                    scope = Scope(str(enriched.get("scope")))
                except ValueError:
                    scope = Scope.LOCAL
                if scope in (Scope.LOCAL, Scope.IGNORED) or scope not in classes:
                    # a remote deletion this device's local policy refuses to perform
                    skipped_local.append(relpath)
            # R1: this verified bundle becomes the carry-forward source — but
            # only after the head/bundle cross-check above passed. The BASE
            # config digests are the ACCEPTED ones from the local state first
            # (0.4.2): a bundle this device only downloaded or reviewed is
            # never a sync point for configs.
            config_paths = [rel for rel in manifest if _is_config_path(rel)]
            config_base_digests = self._config_base_digests(config_paths, state)
            self._save_last_roam(bundle_path)
        finally:
            bundle_path.unlink(missing_ok=True)
        plan = self._plan_install(
            staging_dir,
            manifest,
            classes,
            rules,
            base_manifest,
            overwrite_conflicts=overwrite_conflicts,
            config_base_digests=config_base_digests,
            shared_projection_fn=self._shared_projection_fn(self._host_keys(rules)),
            first_pull_conflict_keys_fn=self._first_pull_conflict_keys_fn(self._host_keys(rules)),
        )
        # R1 replaced the roster 'syncs' guard: a publisher that cannot see a
        # path now carries it forward byte-identical, so a pull never needs to
        # guess what the last publisher was able to delete.
        conflicts = sorted(set(plan.conflicts) | set(plan.config_conflicts))
        plan_diff = self._plan_diff(plan, base_manifest, rules, signed.generation)
        # 0.4.2: the config shared projections this device is ACCEPTING with
        # this pull — merged configs and configs whose local copy already
        # carries the remote projection. Refused configs (config_conflicts)
        # never land here, so they keep their previous base and stay real
        # conflicts instead of turning into phantom local-only changes.
        accepted_config_base = dict(state.config_base_digests or {})
        projection = self._shared_projection_fn(self._host_keys(rules))
        accepted_grew = False
        for relpath in plan.unchanged:
            if not _is_config_path(relpath):
                continue
            staged_member = staging_dir / relpath
            if not staged_member.is_file():
                continue
            digest = projection(
                staged_member.read_text(encoding="utf-8", errors="replace")
            )
            if accepted_config_base.get(relpath) != digest:
                accepted_config_base[relpath] = digest
                accepted_grew = True
        plugins = self._plugins_to_install(staging_dir) if include_prefixes is None else []
        # R3: the plan's per-path outcomes (kept-local configs, kept-deleted
        # paths) must reach the user; the engine relays them verbatim.
        notes: list[str] = list(plan.notes)
        if include_prefixes is not None:
            notes.append(
                "restricted pull: only paths under the include prefixes were considered; "
                "run a full `hermes roam pull` for everything else"
            )
        if skipped_local:
            shown = ", ".join(sorted(set(skipped_local))[:10])
            notes.append(
                f"{len(skipped_local)} remote path(s) are refused by this device's local "
                f"rules or opt-in settings and were not written or deleted: {shown}"
            )
        if archive_planned:
            notes.append(
                f"{archive_planned} session archive member(s) land READ-ONLY under "
                "<home>/roam/session-archive/<device>/ — never in the home tree, never "
                "re-pushable, never resumable; list them with `hermes roam sessions`"
            )
        if unapproved:
            notes.append(
                f"{len(unapproved)} bundled path(s) are not listed in the signed head "
                "and were not installed"
            )
        for relpath, _staged, mode in plan.configs:
            # R11: the plan lists the config merge the apply would perform
            if mode == "first_pull":
                notes.append(
                    f"would first-pull merge {relpath} (remote-only shared keys added, "
                    "local values win where both are set, host-scoped and secret keys "
                    "stay local)"
                )
            else:
                notes.append(f"would merge {relpath} (host-scoped keys stay local)")
        if conflicts:
            shown = ", ".join(conflicts[:10])
            notes.append(
                f"{len(conflicts)} path(s) changed locally since the last sync and "
                f"remotely at the same time (conflicts) were kept local: {shown}; "
                "resolve by pushing (this device's version wins) or by pulling "
                "with overwrite after reviewing the backup"
            )
            if not state.last_pulled_generation and not state.last_pushed_generation:
                # 0.4.2: a brand-new device carries Hermes's freshly seeded
                # SOUL.md and an empty shared history, so its first pull
                # legitimately conflicts on files it never wrote. The seed
                # text is Hermes-internal and cannot be detected from the
                # plugin, so the honest answer is to say it out loud: a
                # brand-new device takes the remote home wholesale.
                notes.append(
                    "this device has never synced with this remote; if it is a "
                    "brand-new device whose local files are only Hermes's fresh "
                    "seed content, run `hermes roam pull --apply "
                    "--overwrite-conflicts` to take the remote home (a backup "
                    "of everything replaced is kept)"
                )
        if plan.skipped:
            notes.append(
                f"{len(plan.skipped)} bundled path(s) skipped (local, ignored, or unselected)"
            )
        unchanged = plan.unchanged
        if unchanged:
            notes.append(
                f"{len(unchanged)} bundled path(s) are unchanged and were neither "
                "rewritten nor backed up"
            )
        if not apply:
            return PullResult(
                generation=signed.generation,
                applied=False,
                plan=plan_diff,
                plugins_to_install=plugins,
                notes=notes,
                conflicts=conflicts,
                skipped_local=sorted(set(skipped_local)),
                recovered=recovered,
            )
        if not (plan.to_write or plan.to_delete or plan.configs):
            # R3: a converged pull plans nothing — no backup directory is
            # created, no journal is written, nothing touches the home, and
            # the result reports an empty backup_dir (restore.apply would
            # refuse the empty plan anyway; skipping it here keeps the
            # backups/journals trees untouched).
            applied = restore.AppliedResult(
                files_written=0,
                files_deleted=0,
                config_merged=False,
                backup_dir="",
                journal_path="",
            )
            # 0.4.2 note: the home is untouched (R3 converged no-op), but the
            # state update below still records the config projections this
            # pull proved the local configs already carry — state.json is
            # roam's own bookkeeping, not a home file.
        else:
            backup_dir = self._roam_subdir("backups") / time.strftime(
                "%Y%m%d-%H%M%S", time.gmtime(self.clock())
            )
            applied = restore.apply(
                self.home,
                plan,
                journal_dir=self._roam_subdir("journals"),
                backup_dir=backup_dir,
                merge_config_fn=self.pipeline.merge_config,
                host_keys=self._host_keys(rules),
            )
            for relpath, staged_member, mode in plan.configs:
                if mode == "first_pull":
                    # the union kept local values: the remote projection was
                    # NOT accepted, so no base is recorded — a later
                    # --overwrite-conflicts must still be able to take it
                    continue
                accepted_config_base[relpath] = projection(
                    staged_member.read_text(encoding="utf-8", errors="replace")
                )
                accepted_grew = True
            # R5: prune the backup window once the install committed.
            self._prune_backups()
        updated = state.to_dict()
        updated.update(last_pull_at=self.clock())
        if accepted_grew:
            updated.update(config_base_digests=accepted_config_base)
        # Conflicting paths keep their last-sync entry (or, when never synced,
        # the signed one) so the local version stays visibly dirty and the
        # next push from this device carries it — the "local wins" resolution.
        new_base = dict(signed.files)
        for relpath in conflicts:
            if relpath in (state.base_files or {}):
                new_base[relpath] = state.base_files[relpath]
        if include_prefixes is None:
            updated.update(
                last_pulled_generation=signed.generation,
                last_seen_generation=signed.generation,
                base_files=new_base,
            )
        else:
            # Partial install: pin only the pulled subset and keep the local
            # seen-generation so the remaining remote changes stay visible.
            merged_base = dict(state.base_files or {})
            for relpath, entry in new_base.items():
                if _prefix_allowed(relpath, include_prefixes):
                    merged_base[relpath] = entry
            for relpath in list(merged_base):
                if _prefix_allowed(relpath, include_prefixes) and relpath not in new_base:
                    merged_base.pop(relpath)
            for relpath in conflicts:
                if (
                    _prefix_allowed(relpath, include_prefixes)
                    and relpath in (state.base_files or {})
                ):
                    merged_base[relpath] = state.base_files[relpath]
            updated.update(base_files=merged_base)
        self.save_state(LocalState.from_dict(updated))
        notes = list(notes)
        if applied.config_merged:
            if any(mode == "first_pull" for _, _, mode in plan.configs):
                notes.append(
                    "config.yaml first-pull merged: the remote's shared keys were added "
                    "where the local file lacked them, local values won for shared keys "
                    "both sides set, and host-scoped and secret keys stayed local"
                )
            else:
                notes.append("config.yaml merged; host-scoped keys kept from the local config")
        notes.append(f"{applied.files_written} file(s) written, {applied.files_deleted} deleted")
        return PullResult(
            generation=signed.generation,
            applied=True,
            plan=plan_diff,
            backup_dir=applied.backup_dir,
            config_merged=applied.config_merged,
            plugins_to_install=plugins,
            notes=notes,
            conflicts=conflicts,
            skipped_local=sorted(set(skipped_local)),
            recovered=recovered,
        )

    def _plan_install(
        self,
        staging_dir: Path,
        manifest: dict,
        classes: set[Scope],
        rules: object,
        base_manifest: dict,
        *,
        overwrite_conflicts: bool,
        config_base_digests: dict[str, str] | None,
        shared_projection_fn: object,
        first_pull_conflict_keys_fn: object,
    ) -> Any:
        """Call restore.plan_install with the pre-agreed three-way signature
        (base digests + shared-projection function for config merges)."""
        return restore.plan_install(
            self.home,
            staging_dir,
            manifest,
            classes=classes,
            rules=rules,
            base_manifest=base_manifest,
            config_base_digests=config_base_digests,
            shared_projection_fn=shared_projection_fn,
            first_pull_conflict_keys_fn=first_pull_conflict_keys_fn,
            overwrite_conflicts=overwrite_conflicts,
        )

    def _config_base_digests(
        self, config_paths: list[str], state: LocalState
    ) -> dict[str, str] | None:
        """R3 wiring: digests of the BASE (last-ACCEPTED) shared projection of
        each config, from the config_base_digests field of the local state
        (0.4.2). A digest is recorded only when this device actually
        accepted a config's shared projection — a push of its own stripped
        config, or a pull apply that merged/already carried it — never when a
        bundle was merely downloaded or a plan reviewed, so a refused or
        plan-only pull cannot poison the base and turn later remote changes
        into phantom local-only changes. None when nothing was accepted yet
        (a true first pull)."""
        accepted = state.config_base_digests if isinstance(state.config_base_digests, dict) else {}
        out = {relpath: accepted[relpath] for relpath in config_paths if relpath in accepted}
        return out or None

    def _first_pull_conflict_keys_fn(self, host_keys: list[str]) -> Any:
        """0.4.2 wiring: the shared-key conflict reporter bound to this
        device's host keys, so the first-pull merge can list every local
        value that wins instead of being silently replaced."""
        conflict_fn = _sibling("scope").first_pull_conflict_keys

        def conflicts(remote_text: str, local_text: str) -> list[str]:
            return conflict_fn(remote_text, local_text, host_keys)

        return conflicts

    def _shared_projection_fn(self, host_keys: list[str]) -> Any:
        """R3 wiring: the shared-projection digest function bound to this
        device's host keys, for g2's plan_install to project local and staged
        config text; None until g2 lands shared_projection_digest."""
        digest_fn = _sibling("scope").shared_projection_digest

        def project(text: str) -> str:
            return digest_fn(text, host_keys)

        return project

    def _prune_backups(self) -> None:
        """R5 wiring: keep only the newest settings.backup_keep backup dirs and
        prune committed/rolled_back journals older than the kept window."""
        try:
            restore.prune_backups(
                self._roam_subdir("backups"),
                self._roam_subdir("journals"),
                keep=self.settings.backup_keep,
            )
        except Exception:  # noqa: BLE001 - a failed prune never fails the install
            pass

    def _plan_diff(
        self, plan: restore.InstallPlan, base_manifest: dict, rules: object, generation: int
    ) -> Diff:
        added: dict[Scope, list[str]] = {}
        changed: dict[Scope, list[str]] = {}
        removed: dict[Scope, list[str]] = {}
        for item in plan.to_write:
            group = changed if (self.home / item.relpath).exists() else added
            group.setdefault(item.scope, []).append(item.relpath)
        for relpath in plan.to_delete:
            scope = self.pipeline.classify(relpath, rules)
            removed.setdefault(scope, []).append(relpath)
        for relpath, _staged, _mode in plan.configs:
            # R11: the pull plan lists the config merges the apply would perform
            scope = self.pipeline.classify(relpath, rules)
            group = changed if (self.home / relpath).exists() else added
            group.setdefault(scope, []).append(relpath)
        return Diff(added=added, changed=changed, removed=removed, remote_generation=generation)

    def _plugins_to_install(self, staging_dir: Path) -> list[str]:
        remote_file = staging_dir / EXTRA_INSTALLED
        if not remote_file.is_file():
            return []
        try:
            remote = json.loads(remote_file.read_bytes())
        except (OSError, ValueError):
            return []
        if not isinstance(remote, dict):
            return []
        local = self.pipeline.generate_installed_plugins(self.home)
        return sorted(set(remote) - set(local))

    def _archive_limits(self) -> object:
        """ArchiveLimits for open_bundle, built from settings.max_bundle_bytes."""
        from .archive import ArchiveLimits  # local import: keeps module identity

        cap = self.settings.max_bundle_bytes
        return ArchiveLimits(max_members=100_000, max_member_bytes=cap, max_total_bytes=cap)

    def _check_version(self, remote_version: str) -> None:
        if not remote_version or remote_version == "unknown":
            return
        if self._newer_remote(remote_version):
            raise VersionMismatch(
                f"the remote home was pushed by Hermes {remote_version} but this device runs "
                f"{self.hermes_version}; upgrade Hermes, then pull again"
            )

    def _newer_remote(self, remote_version: str) -> bool:
        if not remote_version or remote_version == "unknown":
            return False
        if not self.hermes_version or self.hermes_version == "unknown":
            return False
        return _version_pair(remote_version) > _version_pair(self.hermes_version)

    def _other_device(self) -> str:
        from .textsafe import untrusted

        try:
            pair = self._backend_call("get_head")
            if pair is not None:
                # R7: a head field placed in a message goes through untrusted()
                return untrusted(
                    head_mod.verify_head(pair[0], self.settings.archive_key).device, 40
                )
        except (BackendError, VerifyError):
            pass
        return "another device"
