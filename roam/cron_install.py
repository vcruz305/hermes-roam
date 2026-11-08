"""Cron jobs.json install policy: pulled jobs land PAUSED, under Hermes's lock.

Hermes 0.22.5 keeps its cron store at ``<home>/cron/jobs.json`` plus one per
named profile at ``profiles/<name>/cron/jobs.json``, and guards every write
with a cross-process lock on the cron directory's ``.jobs.lock`` file
(fcntl on POSIX, msvcrt on Windows) with a bounded timeout. This module
replicates that contract without importing any Hermes module (the catalog
import rules, docs/DESIGN.md, allow only the documented Hermes surfaces):

- the paused representation is EXACTLY Hermes's own ``pause_job`` update set —
  ``enabled=False, state="paused", paused_at=<now>, paused_reason=<why>`` —
  and the scheduling/runtime fields are reset to Hermes's own
  ``create_job(paused=True)`` pristine values (no next run, no run history,
  counters at 0): a job new to this device inherits nothing from the device
  that authored it;
- the lock is the same ``.jobs.lock`` flock with a bounded wait
  (``LOCK_TIMEOUT_SECONDS``, Hermes's own 30 s bound); a timeout fails the
  cron part CLOSED with a clear error — nothing is ever written unlocked and
  nothing ever hangs;
- the write is atomic (temp sibling + ``os.replace`` + directory fsync) and
  preserves the planned file mode;
- jobs already present on this device with the SAME definition (compared
  with the runtime/scheduling fields excluded, exactly the fields
  cron/jobs.py owns at run time) keep this device's own enabled/paused
  state and runtime fields; local-only jobs are untouched and a concurrent
  local create is preserved by the same shrink-merge Hermes applies.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .errors import RoamError
from .model import canonical_json
from .textsafe import clean_line

try:  # POSIX: the same advisory flock Hermes's cron/jobs.py takes
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
try:  # Windows: msvcrt byte-range lock, as Hermes's cron/jobs.py does there
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

LOCK_FILENAME = ".jobs.lock"
# Hermes's own bound on waiting for the cross-process cron lock
# (cron/jobs.py::_JOBS_LOCK_TIMEOUT_SECONDS): one bounded wait, never a hang.
LOCK_TIMEOUT_SECONDS = 30.0
_RETRY_INTERVAL = 0.05
_PAUSE_REASON = (
    "installed by a hermes-roam pull from another device; awaiting operator "
    "approval"
)
# Job ids that are provably inert inside a printed command get the exact
# resume command; anything else is remote-controlled text and only ever
# reaches a surface through the untrusted-text sanitizer.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Hermes 0.22.5 cron/jobs.py treats these fields as runtime/scheduling state:
# pause_job/resume_job own enabled/state/paused_at/paused_reason, the due scan
# and mark_job_run own next/last run, status, delivery, failure and claim
# fields, and repeat.completed is a run counter. A job's DEFINITION is
# everything else, so the definition comparison excludes exactly these.
_RUNTIME_FIELDS = frozenset(
    {
        "enabled",
        "state",
        "paused_at",
        "paused_reason",
        "next_run_at",
        "last_run_at",
        "last_status",
        "last_error",
        "last_delivery_error",
        "last_delivery_unverified",
        "last_failure",
        "failure_streak",
        "monitor_state",
        "manual_run_at",
        "manual_run_prompt",
    }
)
# create_job(paused=True) pristine values for the scheduling/runtime fields a
# job new to this device must carry instead of the origin's.
_RUNTIME_RESET = {
    "next_run_at": None,
    "last_run_at": None,
    "last_status": None,
    "last_error": None,
    "last_delivery_error": None,
    "last_delivery_unverified": None,
    "last_failure": None,
    "failure_streak": 0,
    "monitor_state": None,
    "manual_run_at": None,
    "manual_run_prompt": None,
}


class CronLockError(RoamError):
    """Another process holds Hermes's cron lock; the cron install fails closed."""


class CronStoreError(RoamError):
    """A cron jobs.json could not be parsed; the install fails closed."""


@dataclass(frozen=True, slots=True)
class MergeResult:
    """One merged cron store: the installed text plus what the surfaces say."""

    text: str
    paused_ids: list[str] = field(default_factory=list)
    malformed_ids: list[str] = field(default_factory=list)
    kept_local_ids: list[str] = field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def is_cron_jobs_path(relpath: object) -> bool:
    """Whether relpath is a Hermes cron job store: the root ``cron/jobs.json``
    or one per named profile ``profiles/<name>/cron/jobs.json``."""
    if not isinstance(relpath, str):
        return False
    parts = relpath.split("/")
    if parts == ["cron", "jobs.json"]:
        return True
    return (
        len(parts) == 4
        and parts[0] == "profiles"
        and parts[1] not in ("", ".", "..")
        and parts[2] == "cron"
        and parts[3] == "jobs.json"
    )


def cron_dir_for(home: Path, relpath: str) -> Path:
    """The Hermes cron directory whose jobs.json lives at relpath."""
    if not is_cron_jobs_path(relpath):
        raise CronStoreError(f"not a cron job store path: {relpath!r}")
    parts = relpath.split("/")
    cron_dir = Path(home).joinpath(*parts[:-1])
    cron_dir.mkdir(parents=True, exist_ok=True)
    return cron_dir


def pause_fields(now_iso: str | None = None) -> dict:
    """EXACTLY Hermes 0.22.5's pause_job update set — no more, no less."""
    return {
        "enabled": False,
        "state": "paused",
        "paused_at": now_iso or _now_iso(),
        "paused_reason": _PAUSE_REASON,
    }


def job_definition(job: dict) -> str:
    """Canonical digest of a job's DEFINITION: every field except the
    runtime/scheduling fields cron/jobs.py owns at run time, with the repeat
    COUNTER (completed) stripped but the repeat LIMIT (times) kept."""
    out = {key: value for key, value in job.items() if key not in _RUNTIME_FIELDS}
    repeat = out.get("repeat")
    if isinstance(repeat, dict):
        out["repeat"] = {"times": repeat.get("times")}
    return canonical_json(out).decode("utf-8")


def _parse_store(text: str | None) -> list:
    """Parse a cron store: the canonical {'jobs': [...]} dict or Hermes's
    legacy bare list; anything else fails closed."""
    if text is None:
        return []
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CronStoreError(f"the cron job store is not valid JSON ({exc})") from exc
    if isinstance(data, dict) and isinstance(data.get("jobs"), list):
        return data["jobs"]
    if isinstance(data, list):
        return data
    raise CronStoreError("the cron job store is neither a jobs dict nor a list")


def _dump_store(jobs: list, now_iso: str | None = None) -> str:
    """Serialize in Hermes's own store shape ({'jobs': [...], 'updated_at'})."""
    return json.dumps(
        {"jobs": jobs, "updated_at": now_iso or _now_iso()},
        indent=2,
        ensure_ascii=False,
    )


def merge_store(
    local_text: str | None, pulled_text: str | None, *, now_iso: str | None = None
) -> MergeResult:
    """Merge a pulled cron store into this device's own.

    Every pulled job NEW to this device or whose definition changed lands
    PAUSED with Hermes's own pause_job representation (plus the pristine
    scheduling fields of create_job(paused=True)); a pulled job already
    present here with the SAME definition keeps this device's record
    verbatim — own enabled/paused state and runtime fields — so a push from
    a receiver can never pause the origin's copy. Local-only records are
    kept verbatim and never deleted. Non-dict records pass through
    untouched (Hermes's loader ignores them).
    """
    now = now_iso or _now_iso()
    local_jobs = _parse_store(local_text)
    pulled_jobs = _parse_store(pulled_text)
    local_by_id = {
        record["id"]: record
        for record in local_jobs
        if isinstance(record, dict) and isinstance(record.get("id"), str)
    }
    consumed: set[str] = set()
    out: list = []
    paused: list[str] = []
    malformed: list[str] = []
    kept: list[str] = []
    for record in pulled_jobs:
        if not isinstance(record, dict):
            out.append(record)  # never silently drop remote content
            continue
        job_id = record.get("id")
        local_record = (
            local_by_id.get(job_id) if isinstance(job_id, str) else None
        )
        if (
            local_record is not None
            and job_definition(local_record) == job_definition(record)
        ):
            out.append(local_record)
            consumed.add(job_id)
            kept.append(job_id)
            continue
        paused_record = dict(record)
        paused_record.update(pause_fields(now))
        paused_record.update(_RUNTIME_RESET)
        repeat = paused_record.get("repeat")
        if isinstance(repeat, dict):
            paused_record["repeat"] = {**repeat, "completed": 0}
        out.append(paused_record)
        if isinstance(job_id, str) and _SAFE_ID.fullmatch(job_id):
            paused.append(job_id)
        elif isinstance(job_id, str):
            malformed.append(job_id)
        else:
            malformed.append("")
    for record in local_jobs:
        if (
            isinstance(record, dict)
            and isinstance(record.get("id"), str)
            and record["id"] in consumed
        ):
            continue
        out.append(record)  # local-only job: untouched, never deleted
    if not paused and not malformed and out == local_jobs and local_text is not None:
        # Nothing changed: every pulled job was already present here with
        # the same definition, so the merged store IS this device's own
        # store. Return it VERBATIM — re-serializing would fabricate a new
        # updated_at and turn a no-op into phantom local dirt.
        return MergeResult(text=local_text, kept_local_ids=kept)
    return MergeResult(
        text=_dump_store(out, now),
        paused_ids=paused,
        malformed_ids=malformed,
        kept_local_ids=kept,
    )


def pause_note(relpath: str, merged: MergeResult) -> str:
    """The surface note: every job that would land paused with the EXACT
    resume command. Ids are remote-controlled: only provably inert ids are
    embedded in the command, everything else goes through the untrusted-text
    sanitizer and is labelled unprintable."""
    parts = [
        f"{relpath}: {len(merged.paused_ids) + len(merged.malformed_ids)} "
        "pulled cron job(s) land PAUSED — a pulled job never starts on this "
        "device by itself; resume each one explicitly with"
    ]
    parts.extend(f"`hermes cron resume {job_id}`" for job_id in merged.paused_ids)
    parts.extend(
        f"an unprintable job id ({clean_line(job_id, 60) or 'empty'})"
        + " that `hermes cron list` shows locally"
        for job_id in merged.malformed_ids
    )
    return "; ".join(parts)


def kept_store_note(relpath: str) -> str:
    """The surface note for a remote deletion of a cron store."""
    return (
        f"{relpath}: the remote no longer carries this cron job store; this "
        "device's local job definitions were kept — local-only cron jobs are "
        "never deleted by a pull (push from this device to publish them, or "
        "delete the file yourself)"
    )


class CronLocks:
    """Hermes's own cross-process cron locks: one ``.jobs.lock`` flock per
    cron directory, bounded by ``LOCK_TIMEOUT_SECONDS``. POSIX uses fcntl
    exactly like Hermes's ``_jobs_lock``; Windows uses msvcrt.locking
    (LK_LOCK blocks ~10 s per attempt, so the bound there overshoots by at
    most one attempt — still bounded, never a hang). Acquiring is
    idempotent, so a caller that already holds the locks (a journaled
    install) can re-assert them; a timeout raises CronLockError and nothing
    is written."""

    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self._descriptors: dict[str, int] = {}

    def holds(self, relpath: str) -> bool:
        return relpath in self._descriptors

    def _try_lock(self, descriptor: int) -> bool:
        """One NON-blocking attempt; acquire() owns the bounded retry loop."""
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            if msvcrt is not None:  # pragma: no cover - Windows
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                return True
        except OSError:
            return False
        raise CronLockError(
            "no file-locking backend is available on this platform; the cron "
            "part of this install failed CLOSED and nothing was written"
        )

    def acquire(self, relpath: str, *, timeout: float | None = None) -> None:
        if self.holds(relpath):
            return
        lock_path = cron_dir_for(self.home, relpath) / LOCK_FILENAME
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + (
            LOCK_TIMEOUT_SECONDS if timeout is None else timeout
        )
        try:
            while not self._try_lock(descriptor):
                if time.monotonic() >= deadline:
                    raise CronLockError(
                        f"another process holds Hermes's cron lock "
                        f"({lock_path}); the cron part of this install "
                        "failed CLOSED and nothing was written — close "
                        "the other hermes process or wait for it to "
                        "finish, then retry"
                    )
                time.sleep(_RETRY_INTERVAL)
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptors[relpath] = descriptor

    def release(self) -> None:
        for descriptor in self._descriptors.values():
            try:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover - Windows
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            except OSError:  # pragma: no cover - closing releases anyway
                pass
            os.close(descriptor)
        self._descriptors.clear()


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


def install_jobs_file(
    home: Path,
    relpath: str,
    payload: bytes,
    mode: int,
    locks: CronLocks,
) -> None:
    """Atomically install one merged cron store under the cron lock.

    The caller must already hold the lock (a journaled install acquires every
    cron lock before any file is touched); re-asserting it here is a no-op.
    The payload must already be the MERGED store (roam's pull/restore merge);
    it is written VERBATIM so the recorded sync point and the installed file
    always describe the same bytes. As a fail-closed invariant, a payload
    job that is not already on this device must be PAUSED — an unmerged
    store must never be written by accident. Before the write, the current
    on-disk store is re-read under the lock and any job that landed there
    while this pull was planning — a concurrent local ``hermes cron
    create`` — is preserved verbatim, exactly like Hermes's own
    shrink-merge; that case re-serializes, shows as real local dirt, and
    is the only path that may change the payload. An unparseable store
    fails closed; a byte-identical result is not written at all.
    """
    locks.acquire(relpath)
    target = Path(home) / relpath
    payload_jobs = _parse_store(payload.decode("utf-8", "replace"))
    payload_ids = {
        record["id"]
        for record in payload_jobs
        if isinstance(record, dict) and isinstance(record.get("id"), str)
    }
    try:
        disk_text = target.read_text("utf-8") if target.is_file() else None
    except OSError:
        disk_text = None
    disk_ids = set()
    disk_jobs: list = []
    if disk_text is not None:
        disk_jobs = _parse_store(disk_text)
        disk_ids = {
            record["id"]
            for record in disk_jobs
            if isinstance(record, dict) and isinstance(record.get("id"), str)
        }
    for record in payload_jobs:
        if (
            isinstance(record, dict)
            and isinstance(record.get("id"), str)
            and record["id"] not in disk_ids
            and not (
                record.get("enabled") is False and record.get("state") == "paused"
            )
        ):
            raise CronStoreError(
                f"refusing to install cron job {record['id']!r} from "
                f"{relpath}: it is new to this device and not paused — a "
                "pulled job never starts on this device by itself"
            )
    data = payload
    if disk_ids - payload_ids:
        # a job created locally while this pull was planning: keep it
        # verbatim (Hermes's own shrink-merge); the result differs from the
        # planned payload and will show as real local dirt.
        merged_jobs = list(payload_jobs)
        for record in disk_jobs:
            if (
                isinstance(record, dict)
                and isinstance(record.get("id"), str)
                and record["id"] not in payload_ids
            ):
                merged_jobs.append(record)
        data = _dump_store(merged_jobs).encode("utf-8")
    if disk_text is not None and data.decode("utf-8", "replace") == disk_text:
        return  # byte-identical: never rewritten
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, stat.S_IMODE(mode) or 0o644)
        os.replace(tmp, target)
        _fsync_dir(target.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
