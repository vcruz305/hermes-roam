"""0.5.3 review fix (teknium1): pulled cron jobs land PAUSED, under Hermes's
own cron lock, and the paused state never round-trips.

Each test asserts the ruled behaviour for one finding. The cron store shape,
the paused representation, and the lock file are replicated from Hermes
0.22.5's cron/jobs.py (create_job, pause_job, _jobs_lock) — the plugin never
imports that module.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fakes.fake_backend import FakeBackend
from fakes.fake_pipeline import PIPELINE

from roam import cron_install
from roam.engine import Engine
from roam.errors import RoamError
from roam.model import RoamSettings

KEY = b"0123456789abcdef0123456789abcdef"  # 32 real-entropy bytes
VERSION = "0.22.5"
JOB_ID = "abc123def456"
PROFILE_JOB_ID = "fff222ddd333"


def make_settings(device: str) -> RoamSettings:
    return RoamSettings(
        backend="fake", device_name=device, archive_key=KEY, sync_cron=True
    )


def make_engine(home: Path, device: str, backend: FakeBackend) -> Engine:
    return Engine(home, make_settings(device), backend, VERSION, pipeline=PIPELINE)


def store(jobs: list) -> bytes:
    return json.dumps(
        {"jobs": jobs, "updated_at": "2026-11-07T00:00:00+00:00"},
        indent=2,
    ).encode()


def job_record(job_id: str, *, prompt: str = "ping", enabled: bool = True,
               state: str = "scheduled", next_run_at="2027-01-01T00:00:00+00:00",
               last_run_at=None) -> dict:
    """A realistic Hermes 0.22.5 cron job record (cron/jobs.py create_job)."""
    return {
        "id": job_id,
        "name": "probe",
        "prompt": prompt,
        "skills": None,
        "skill": None,
        "model": None,
        "provider": None,
        "base_url": None,
        "script": None,
        "no_agent": False,
        "monitor_script": None,
        "monitor_url": None,
        "monitor_state": None,
        "context_from": None,
        "schedule": {
            "kind": "once",
            "run_at": "2027-01-01T00:00:00+00:00",
            "display": "once at 2027-01-01 00:00",
        },
        "schedule_display": "once at 2027-01-01 00:00",
        "repeat": {"times": 1, "completed": 0},
        "enabled": enabled,
        "state": state,
        "paused_at": None,
        "paused_reason": None,
        "created_at": "2026-11-07T00:00:00+00:00",
        "next_run_at": next_run_at,
        "last_run_at": last_run_at,
        "last_status": None,
        "last_error": None,
        "last_delivery_error": None,
        "last_delivery_unverified": None,
        "last_failure": None,
        "failure_streak": 0,
        "deliver": "local",
        "origin": None,
    }


def fleet(tmp_path: Path) -> tuple:
    """Device A holds a live cron job; device B is a fresh home.

    Neither home carries a config.yaml or any other shared file: the cron
    store is the ONLY delta between the devices, so a status check after a
    pull sees cron and nothing else (an asymmetric config would be honest
    dirt by design, see test_engine.py)."""
    backend = FakeBackend()
    home_a = tmp_path / "home-a"
    home_b = tmp_path / "home-b"
    (home_a / "cron").mkdir(parents=True)
    (home_a / "cron" / "jobs.json").write_bytes(store([job_record(JOB_ID)]))
    home_b.mkdir()
    engine_a = make_engine(home_a, "dev-a", backend)
    engine_b = make_engine(home_b, "dev-b", backend)
    return backend, home_a, home_b, engine_a, engine_b


def published(tmp_path: Path) -> tuple:
    """A already pushed its cron store; B is initialised and ready to pull."""
    backend, home_a, home_b, engine_a, engine_b = fleet(tmp_path)
    engine_a.init(create_remote=True)
    engine_a.push()
    engine_b.init(create_remote=False)
    return backend, home_a, home_b, engine_a, engine_b


def read_jobs(home: Path, relpath: str = "cron/jobs.json") -> list:
    return json.loads((home / relpath).read_text())["jobs"]


def resume_note(result, job_id: str) -> str:
    matches = [n for n in getattr(result, "notes", []) or [] if f"hermes cron resume {job_id}" in n]
    assert matches, f"no note lists `hermes cron resume {job_id}`: {getattr(result, 'notes', [])}"
    return matches[0]


# ---------------------------------------------------------------------- RED 1


def test_pulled_enabled_job_lands_paused(tmp_path: Path) -> None:
    """Finding: pull --apply wrote the other device's jobs.json as-is, so the
    scheduler picks the pulled job up on its next tick and it runs here."""
    _, home_a, home_b, _, engine_b = published(tmp_path)

    # the plan-only pull already lists the exact resume command
    plan = engine_b.pull(apply=False)
    resume_note(plan, JOB_ID)
    assert not (home_b / "cron" / "jobs.json").exists()

    result = engine_b.pull(apply=True)
    resume_note(result, JOB_ID)

    installed = read_jobs(home_b)[0]
    # exactly Hermes's own pause_job representation (cron/jobs.py):
    assert installed["enabled"] is False
    assert installed["state"] == "paused"
    assert isinstance(installed["paused_at"], str) and installed["paused_at"]
    assert isinstance(installed["paused_reason"], str) and installed["paused_reason"]
    # a job new to this device inherits no scheduling state from the origin
    assert installed["next_run_at"] is None
    assert installed["last_run_at"] is None
    assert installed["failure_streak"] == 0
    assert installed["repeat"]["completed"] == 0
    # the definition itself traveled
    assert installed["prompt"] == "ping"
    assert installed["schedule"]["run_at"] == "2027-01-01T00:00:00+00:00"
    # the install went through Hermes's cron lock file
    assert (home_b / "cron" / ".jobs.lock").is_file()


def test_paused_state_is_not_local_dirt(tmp_path: Path) -> None:
    """The paused rewrite must not count as a local change: status must be
    clean after the pull, or every later push would carry phantom dirt."""
    _, _, home_b, _, engine_b = published(tmp_path)
    engine_b.pull(apply=True)
    status = engine_b.status()
    assert not status.local_dirty, status.dirty_count


def test_unchanged_existing_job_keeps_its_own_state(tmp_path: Path) -> None:
    """A job already present with the SAME definition keeps this device's
    own enabled/paused state and runtime fields; nothing is rewritten."""
    _, home_a, home_b, _, engine_b = published(tmp_path)
    own = job_record(JOB_ID, next_run_at="2028-03-04T05:06:07+00:00",
                     last_run_at="2026-11-01T00:00:00+00:00")
    (home_b / "cron").mkdir(parents=True)
    before = store([own])
    (home_b / "cron" / "jobs.json").write_bytes(before)

    result = engine_b.pull(apply=True)
    after = (home_b / "cron" / "jobs.json").read_bytes()
    assert after == before  # byte-identical: never rewritten, never backed up
    installed = read_jobs(home_b)[0]
    assert installed["enabled"] is True
    assert installed["state"] == "scheduled"
    assert installed["next_run_at"] == "2028-03-04T05:06:07+00:00"
    assert installed["last_run_at"] == "2026-11-01T00:00:00+00:00"
    assert not any(JOB_ID in n and "resume" in n for n in result.notes)
    status = engine_b.status()
    assert not status.local_dirty, status.dirty_count


def test_changed_definition_repauses(tmp_path: Path) -> None:
    """A pulled job whose DEFINITION changed (prompt edited on A) re-pauses.

    The full lifecycle: the job lands paused on B, B's operator resumes it
    (hermes cron resume), then A edits the definition. The default pull
    fails CLOSED for the diverged store (kept local, visible conflict);
    --overwrite-conflicts takes the remote definition and it lands PAUSED
    again — the same install path, never a running job."""
    _, home_a, home_b, engine_a, engine_b = published(tmp_path)
    engine_b.pull(apply=True)  # the job first lands paused on B (gen 1)

    # B's operator resumes the job — a per-device runtime-state change
    resumed = json.loads((home_b / "cron" / "jobs.json").read_text())
    resumed["jobs"][0]["enabled"] = True
    resumed["jobs"][0]["state"] = "scheduled"
    resumed["jobs"][0]["next_run_at"] = "2028-01-01T00:00:00+00:00"
    (home_b / "cron" / "jobs.json").write_text(json.dumps(resumed, indent=2))

    # A edits the job's definition and publishes a new generation
    (home_a / "cron" / "jobs.json").write_bytes(
        store([job_record(JOB_ID, prompt="changed definition")])
    )
    engine_a.push()

    # default pull: the locally-diverged store is a kept-local conflict —
    # fail closed, the new definition does not silently land
    plain = engine_b.pull(apply=True)
    assert any("cron/jobs.json" in n for n in plain.notes)
    still = read_jobs(home_b)[0]
    assert still["prompt"] == "ping"
    assert still["enabled"] is True  # B's own resume stands

    # --overwrite-conflicts takes the remote definition — and it lands PAUSED
    result = engine_b.pull(apply=True, overwrite_conflicts=True)
    resume_note(result, JOB_ID)
    installed = read_jobs(home_b)[0]
    assert installed["prompt"] == "changed definition"
    assert installed["enabled"] is False
    assert installed["state"] == "paused"
    assert installed["next_run_at"] is None


def test_paused_state_does_not_round_trip_to_the_origin(tmp_path: Path) -> None:
    """B pushes (for its own reasons) while the job is paused there; A pulls
    back and must keep its own ENABLED copy — each receiver keeps its own
    enabled state, so the pause never propagates back."""
    _, home_a, home_b, engine_a, engine_b = published(tmp_path)
    engine_b.pull(apply=True)  # job lands paused on B

    # B has its own legitimate shared change to publish
    (home_b / "memories").mkdir()
    (home_b / "memories" / "b.md").write_text("b memory")
    pushed = engine_b.push()
    assert pushed.generation == 2

    pulled = engine_a.pull(apply=True)
    assert not any(f"hermes cron resume {JOB_ID}" in n for n in pulled.notes)
    a_job = read_jobs(home_a)[0]
    assert a_job["enabled"] is True
    assert a_job["state"] == "scheduled"
    assert a_job["prompt"] == "ping"
    # neither side shows the cron store as local dirt
    assert not engine_a.status().local_dirty
    assert not engine_b.status().local_dirty


def test_local_only_jobs_are_untouched_and_never_deleted(tmp_path: Path) -> None:
    """Local-only jobs never travel and a remote deletion of the cron store
    never deletes this device's own job definitions."""
    backend, home_a, home_b, engine_a, engine_b = published(tmp_path)
    engine_b.pull(apply=True)  # A's job lands paused on B (gen 1)

    # B's operator then creates a local-only job of their own
    own = job_record("bbb111ccc222", prompt="b-only")
    local = json.loads((home_b / "cron" / "jobs.json").read_text())
    local["jobs"].append(own)
    (home_b / "cron" / "jobs.json").write_text(json.dumps(local, indent=2))

    # A stops syncing cron and publishes a generation without the store
    (home_a / "cron" / "jobs.json").unlink()
    engine_a.push()

    result = engine_b.pull(apply=True)
    ids = [j["id"] for j in read_jobs(home_b)]
    assert "abc123def456" in ids  # the pulled job's definition is still installed
    assert "bbb111ccc222" in ids  # the local-only job survived the pull
    b = [j for j in read_jobs(home_b) if j["id"] == "bbb111ccc222"][0]
    assert b["enabled"] is True  # and kept its own state
    assert any("bbb111ccc222" not in n and "cron" in n for n in result.notes)


def test_profile_cron_file_lands_paused(tmp_path: Path) -> None:
    """profiles/<name>/cron/jobs.json gets the same treatment, with the lock
    taken in that profile's own cron directory."""
    backend = FakeBackend()
    home_a = tmp_path / "home-a"
    home_b = tmp_path / "home-b"
    (home_a / "profiles" / "beta" / "cron").mkdir(parents=True)
    (home_a / "profiles" / "beta" / "cron" / "jobs.json").write_bytes(
        store([job_record(PROFILE_JOB_ID)])
    )
    home_b.mkdir()
    (home_b / "config.yaml").write_text("model:\n  provider: p-b\n")
    engine_a = make_engine(home_a, "dev-a", backend)
    engine_b = make_engine(home_b, "dev-b", backend)
    engine_a.init(create_remote=True)
    engine_a.push()
    engine_b.init(create_remote=False)

    plan = engine_b.pull(apply=False)
    resume_note(plan, PROFILE_JOB_ID)
    result = engine_b.pull(apply=True)
    resume_note(result, PROFILE_JOB_ID)
    installed = read_jobs(home_b, "profiles/beta/cron/jobs.json")[0]
    assert installed["enabled"] is False
    assert installed["state"] == "paused"
    assert installed["next_run_at"] is None
    assert (home_b / "profiles" / "beta" / "cron" / ".jobs.lock").is_file()


def test_malformed_job_id_is_sanitized_in_the_note(tmp_path: Path) -> None:
    """A remote-controlled job id never reaches a surface raw: only ids that
    are safe to embed in a command get the exact resume command."""
    backend = FakeBackend()
    home_a = tmp_path / "home-a"
    home_b = tmp_path / "home-b"
    evil = job_record("good12id34567")
    evil["name"] = "x\rid"
    weird = {"id": "bad rid\twith controls; rm", "prompt": "p"}
    (home_a / "cron").mkdir(parents=True)
    (home_a / "cron" / "jobs.json").write_bytes(store([evil, weird]))
    home_b.mkdir()
    (home_b / "config.yaml").write_text("model:\n  provider: p-b\n")
    engine_a = make_engine(home_a, "dev-a", backend)
    engine_b = make_engine(home_b, "dev-b", backend)
    engine_a.init(create_remote=True)
    engine_a.push()
    engine_b.init(create_remote=False)

    result = engine_b.pull(apply=True)
    joined = "\n".join(result.notes)
    assert "hermes cron resume good12id34567" in joined
    # the control characters never survive into the note
    assert "\r" not in joined and "\t" not in joined
    assert "hermes cron resume bad rid" not in joined


def test_foreign_cron_lock_fails_closed_within_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real fcntl lock held by another process on B's .jobs.lock makes the
    pull fail closed with a clear error inside the bounded wait — nothing is
    written, unlocked or not."""
    monkeypatch.setattr(cron_install, "LOCK_TIMEOUT_SECONDS", 1.5)
    _, home_a, home_b, _, engine_b = published(tmp_path)

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, sys, time\n"
                f"p = {str(home_b / 'cron' / '.jobs.lock')!r}\n"
                "import os; os.makedirs(os.path.dirname(p), exist_ok=True)\n"
                "f = open(p, 'a+')\n"
                "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
                "sys.stdout.write('locked\\n'); sys.stdout.flush()\n"
                "time.sleep(60)\n"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        started = time.monotonic()
        with pytest.raises(RoamError) as excinfo:
            engine_b.pull(apply=True)
        elapsed = time.monotonic() - started
        assert "cron" in str(excinfo.value).lower()
        assert "lock" in str(excinfo.value).lower()
        assert elapsed < 15.0  # bounded: never a hang
        # fail CLOSED: no cron store and no other home file was written
        assert not (home_b / "cron" / "jobs.json").exists()
        assert not (home_b / "SOUL.md").exists()
    finally:
        holder.kill()
        holder.wait()


def test_foreign_cron_lock_recovery_restores_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash recovery never restores a cron store outside the lock either."""
    monkeypatch.setattr(cron_install, "LOCK_TIMEOUT_SECONDS", 0.3)
    _, home_a, home_b, _, engine_b = published(tmp_path)
    own = store([job_record("bbb111ccc222", prompt="b-only")])
    (home_b / "cron").mkdir(parents=True)
    (home_b / "cron" / "jobs.json").write_bytes(own)

    from roam import restore

    journal_dir = home_b / "roam" / "journals"
    backup_dir = home_b / "roam" / "backups" / "20260101-000000"
    staged = home_b / "roam" / "staging" / "pulled"
    (staged / "cron").mkdir(parents=True)
    (staged / "cron" / "jobs.json").write_bytes(store([job_record(JOB_ID)]))
    plan = restore.InstallPlan(
        to_write=(
            restore.PlannedFile(
                "cron/jobs.json", staged / "cron" / "jobs.json", "x" * 64, 1, 0o644,
                __import__("roam.model", fromlist=["Scope"]).Scope.CRON,
            ),
        )
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, sys, time\n"
                f"p = {str(home_b / 'cron' / '.jobs.lock')!r}\n"
                "import os; os.makedirs(os.path.dirname(p), exist_ok=True)\n"
                "f = open(p, 'a+')\n"
                "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
                "sys.stdout.write('locked\\n'); sys.stdout.flush()\n"
                "time.sleep(60)\n"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        with pytest.raises(RoamError):
            restore.apply(
                home_b, plan,
                journal_dir=journal_dir,
                backup_dir=backup_dir,
                merge_config_fn=PIPELINE.merge_config,
                host_keys=[],
            )
        assert (home_b / "cron" / "jobs.json").read_bytes() == own
    finally:
        holder.kill()
        holder.wait()


def test_merge_store_unit_contract() -> None:
    """The merge's unit contract: definitions compared with the runtime/
    scheduling fields excluded (enabled/state/paused markers, next/last run,
    counters, claims) exactly as cron/jobs.py treats them."""
    local = job_record(JOB_ID, next_run_at="2028-01-01T00:00:00+00:00")
    same_def = job_record(JOB_ID, next_run_at="2029-01-01T00:00:00+00:00")
    a = cron_install.merge_store(store([local]).decode(), store([same_def]).decode())
    assert a.kept_local_ids == [JOB_ID]
    assert a.paused_ids == []
    assert json.loads(a.text)["jobs"][0]["next_run_at"] == "2028-01-01T00:00:00+00:00"

    changed = job_record(JOB_ID, prompt="changed")
    b = cron_install.merge_store(store([local]).decode(), store([changed]).decode())
    assert b.paused_ids == [JOB_ID]
    installed = json.loads(b.text)["jobs"][0]
    assert installed["prompt"] == "changed"
    assert installed["enabled"] is False
    assert installed["state"] == "paused"

    # runtime-only divergence never counts as a definition change
    local_resumed = dict(job_record(JOB_ID))
    local_resumed.update({"enabled": False, "state": "paused",
                          "paused_at": "2026-11-07T01:00:00+00:00",
                          "paused_reason": "user paused it here"})
    remote_copy = job_record(JOB_ID)  # still enabled on the remote
    c = cron_install.merge_store(store([local_resumed]).decode(), store([remote_copy]).decode())
    assert c.kept_local_ids == [JOB_ID]
    kept = json.loads(c.text)["jobs"][0]
    assert kept["state"] == "paused" and kept["enabled"] is False

    # unparseable pulled store fails closed
    with pytest.raises(RoamError):
        cron_install.merge_store(None, "{not json")

    # unparseable LOCAL store fails closed too (never merged over)
    with pytest.raises(RoamError):
        cron_install.merge_store("{not json", store([job_record(JOB_ID)]).decode())
