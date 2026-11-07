"""Adversarial review ROUND 2, lane s1 — sync semantics and trust.

Every test documents a defect found by round-2 lane s1 and asserts the
CORRECT behaviour, so each FAILS against the current merged tree
(commit 5f09276, v0.2.0).  The tests drive the REAL roam pipeline (real
scope / inventory / archive / restore modules, real AES-GCM bundles and
real signed heads) against the in-memory FakeBackend — the production
wiring minus the network.

Findings (test -> finding):
  t1  a LOCAL DELETION of a synced shared file is silently reverted by
      the next `pull --apply`: plan_install only runs its divergence
      check when the local file exists, so a missing local file is
      reinstalled, the base becomes the remote entry, and the deletion
      can never be pushed ("nothing to push" forever).  The mirror case
      (remote delete vs local recreate, review r2 f2) IS flagged; this
      direction is not.
  t2  a device whose local rules.yaml hides a SHARED path publishes a
      manifest that drops it; every other device's next pull deletes
      the path.  The opt-in carry-forward guard only protects
      SESSIONS/CRON, not rule-hidden SHARED content.
  t3  the secret key-name patterns miss `aws_secret_access_key` (and
      dash forms like `api-key`): the AWS secret access key travels in
      the config.yaml bundle and is installed on the other device.
  t4  the opt-in carry-forward guard only inspects the LAST publisher:
      a deletion first published by a non-opted-in device is laundered
      by one push from an opted-in device that never had the content,
      and the fleet's opt-in files are wiped.
  t5  `hermes roam doctor` is blind to a rolled-back remote: with the
      remote head rewound below this device's floor every pull/push is
      refused, yet all doctor checks pass.
  t6a every `pull --apply` writes a full backup of the affected files —
      including the pre-merge config.yaml with this host's secrets —
      into a new <home>/roam/backups/<ts>/ dir that nothing ever prunes.
      # RULING: R5 — rewritten to assert the ruled retention window
      (keep the newest settings.backup_keep, default 5, prune the rest).
  t6b when those backups are pruned (the natural response to t6a), crash
      recovery DESTROYS content: the rollback unlinks the half-installed
      file and the pre-pull version, which existed only in the backup,
      is gone.
  t7  Engine.recover() never takes the per-home process lock: while
      another roam command holds the flock it rolls back that command's
      in-flight install.
  t8  `hermes roam init` does not run crash recovery; the CHANGELOG
      claims recovery runs "at the start of every command".
  t9  deleting <home>/roam/state.json resets the rollback guard floor
      (undisclosed): a rolled-back remote is then pulled, content the
      fleet deleted is resurrected locally, and the next push
      re-publishes it fleet-wide.
  t10 config.yaml has NO conflict detection: a local edit of a shared
      (non-host-scoped) config key — or a shared key added only
      locally — is silently discarded by the merge, with no conflict
      flag, and the local value can never be pushed again.

Clean probes (these PASS and document behaviour that is already
correct — they are findings-free areas, kept here as guards):
  c1  max_tokens / token_budget / context tokens are NOT secret-stripped
      (no false positive: they keep syncing).
  c2  setuid/sticky bits are dropped on install (mode & 0o777).
  c3  OPENAI_API_KEY / GITHUB_TOKEN / nested Authorization headers ARE
      stripped at any depth.
  c4  roster tampering fails head verification (the roster is inside
      the HMAC).
"""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fakes.fake_backend import FakeBackend

from roam import head as head_mod
from roam import restore, scope
from roam.engine import Engine
from roam.errors import VerifyError
from roam.model import RoamSettings

KEY = bytes(range(32))  # real entropy: weak keys are rejected
VERSION = "0.22.5"

AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLE"


# ------------------------------------------------------------------ helpers


def mk_settings(device: str, **kw) -> RoamSettings:
    return RoamSettings(backend="fake", device_name=device, archive_key=KEY, **kw)


class FakeClock:
    """Advancing clock so each pull lands in its own backup timestamp dir."""

    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        self.t += 3600.0
        return self.t


def mk_engine(home: Path, device: str, backend: FakeBackend, **kw) -> Engine:
    # No pipeline= argument: the engine must use the REAL roam pipeline.
    clock = FakeClock() if kw.pop("fake_clock", False) else None
    if clock is None:
        return Engine(home, mk_settings(device, **kw), backend, VERSION)
    return Engine(home, mk_settings(device, **kw), backend, VERSION, clock=clock)


def write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, mode)


def make_home(tmp_path: Path, name: str, files: dict[str, bytes] | None = None,
             modes: dict[str, int] | None = None) -> Path:
    home = tmp_path / name
    for rel, data in (files or {}).items():
        write(home / rel, data, (modes or {}).get(rel, 0o644))
    return home


def init_and_push(engine: Engine) -> None:
    engine.init(create_remote=True)
    engine.push()


def crash_mid_install(engine: Engine, monkeypatch) -> None:
    """Simulate SIGKILL exactly mid `pull --apply` (as review r2 f1): the
    first planned file is really installed, the process dies on the second
    (SystemExit is a BaseException, so restore.apply's `except Exception`
    rollback does NOT run); the journal stays PHASE_INSTALLING and the
    home is half-applied. The calling test must arrange >= 2 planned
    writes so the crash lands mid-flight."""
    real_install = restore._install_from_staged
    calls: list[Path] = []

    def kill_after_first(target, staged, mode):
        calls.append(target)
        if len(calls) == 2:
            raise SystemExit(137)  # the process dies on the second write
        return real_install(target, staged, mode)

    monkeypatch.setattr(restore, "_install_from_staged", kill_after_first)
    with pytest.raises(SystemExit):
        engine.pull(apply=True)
    assert len(calls) >= 2, "the crash must land mid-install, not before it"


def stuck_journal(home: Path) -> dict:
    journals = sorted((home / "roam" / "journals").glob("*.json"))
    installing = [
        path for path in journals
        if json.loads(path.read_bytes()).get("phase") == "installing"
    ]
    assert len(installing) == 1, (
        f"expected exactly one PHASE_INSTALLING journal, found {len(installing)} "
        f"of {len(journals)} (committed journals are never pruned either)"
    )
    return json.loads(installing[0].read_bytes())


# ------------------------------------------------------------------ t1


def test_t1_local_delete_is_silently_reverted_by_pull(tmp_path) -> None:
    """A synced shared file the user deleted locally is silently reinstalled
    by the next pull --apply, flagged as no conflict, and the deletion
    becomes unpushable (the base is reset to the remote entry)."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {
        "SOUL.md": b"v1", "skills/keep/SKILL.md": b"keep",
    })
    engine_a = mk_engine(home_a, "dev-a", backend)
    init_and_push(engine_a)  # generation 1

    home_b = make_home(tmp_path, "home-b")
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.pull(apply=True)  # B is in sync with generation 1

    # The user deletes the synced file locally (intent: remove it).
    (home_b / "SOUL.md").unlink()

    # The remote is unchanged; the next pull --apply must treat the local
    # deletion like any other local divergence since the last sync point:
    # keep it deleted and flag it, never silently reinstall.
    result = engine_b.pull(apply=True)
    assert not (home_b / "SOUL.md").exists() or result.conflicts, (
        "pull --apply silently reinstalled a file the user had deleted; "
        "the three-way check only runs when the local file exists, the "
        "result flags no conflict, and the deletion is now unpushable: "
        f"{json.dumps(result.to_dict())}"
    )
    # And the loss is permanent: the base now equals the remote manifest.
    push = engine_b.push()
    assert push.skipped_reason != "nothing to push", (
        "after the reinstallation the device's base equals the remote "
        "manifest, so the user's deletion can never be published"
    )


# ------------------------------------------------------------------ t2


def test_t2_rule_hidden_shared_path_is_wiped_fleet_wide(tmp_path) -> None:
    """A device whose rules.yaml keeps a SHARED path local publishes a
    manifest without it on its next push; every other device deletes
    that path on pull. The carry-forward guard only covers sessions and
    cron, not rule-hidden shared content."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {
        "skills/private/SKILL.md": b"private skill",
        "SOUL.md": b"v1",
    })
    engine_a = mk_engine(home_a, "dev-a", backend)
    init_and_push(engine_a)  # generation 1 carries skills/private/SKILL.md

    # Device B decides skills/private/** must stay on this machine only.
    home_b = make_home(tmp_path, "home-b", {
        "roam/rules.yaml": b"local:\n  - skills/private/**\n",
    })
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.pull(apply=True)  # skills/private/SKILL.md refused, kept local

    # B makes an ordinary shared change and pushes it.
    write(home_b / "SOUL.md", b"b's edit")
    engine_b.push()  # generation 2: the manifest drops skills/private/SKILL.md

    # A pulls: skills/private/SKILL.md is in A's base and absent from the
    # new manifest — a deletion nobody performed on A.
    engine_a.pull(apply=True)
    assert (home_a / "skills/private/SKILL.md").exists(), (
        "device A deleted skills/private/SKILL.md by pulling a manifest "
        "published by a device whose local rules.yaml hides the path: the "
        "opt-in carry-forward guard protects only sessions/cron, so a "
        "single device's rules.yaml wipes shared content fleet-wide"
    )


# ------------------------------------------------------------------ t3


AWS_CONFIG = f"""model:
  provider: p-a
mcp_servers:
  fetch:
    command: /usr/bin/fetch
    env:
      AWS_ACCESS_KEY_ID: NOTAKEYIDEXAMPLE00
      AWS_SECRET_ACCESS_KEY: {AWS_SECRET}
"""


def test_t3_aws_secret_access_key_travels(tmp_path) -> None:
    """`aws_secret_access_key` (the standard AWS name) and dash forms like
    `api-key` match no DEFAULT_SECRET_KEY_PATTERNS entry: the secret is
    neither stripped on push nor kept local on pull."""
    host_keys = list(scope.Rules.default().host_keys)

    stripped = scope.strip_host_keys(AWS_CONFIG, host_keys)
    assert AWS_SECRET not in stripped, (
        "strip_host_keys keeps mcp_servers.fetch.env.AWS_SECRET_ACCESS_KEY: "
        "the key name matches no secret pattern (only *_api_key/*_token/"
        "*_secret suffixes are checked) and the value matches no known "
        "credential regex, so the AWS secret access key is pushed"
    )

    stripped_dash = scope.strip_host_keys(
        "headers:\n  api-key: short-but-secret\n", host_keys
    )
    assert "short-but-secret" not in stripped_dash, (
        "a dash-form key (`api-key`) is not matched by the underscore-based "
        "secret patterns and its value travels"
    )

    # End to end: A's AWS secret really leaves the machine and lands on B.
    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {"config.yaml": AWS_CONFIG.encode()})
    engine_a = mk_engine(home_a, "dev-a", backend)
    init_and_push(engine_a)
    home_b = make_home(tmp_path, "home-b")
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.pull(apply=True)
    b_config = (home_b / "config.yaml").read_text()
    assert AWS_SECRET not in b_config, (
        "device A's AWS secret access key was installed into device B's "
        "config.yaml by an ordinary pull --apply"
    )


# ------------------------------------------------------------------ t4


def test_t4_carry_forward_guard_laundering(tmp_path) -> None:
    """The opt-in carry-forward guard only inspects the LAST publisher. A
    deletion first published by a non-opted-in device is laundered by a
    single push from an opted-in device that never had the content, and
    every opted-in device then wipes its files."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {
        "sessions/s1.jsonl": b'{"turn": 1}',
        "skills/keep/SKILL.md": b"keep",
    })
    engine_a = mk_engine(home_a, "dev-a", backend, sync_sessions=True)
    init_and_push(engine_a)  # generation 1 carries the sessions

    # Z opts in and holds the session transcript locally.
    home_z = make_home(tmp_path, "home-z")
    engine_z = mk_engine(home_z, "dev-z", backend, sync_sessions=True)
    engine_z.pull(apply=True)

    # X never opted in; its push drops the sessions (guarded at gen 2).
    home_x = make_home(tmp_path, "home-x")
    engine_x = mk_engine(home_x, "dev-x", backend)
    engine_x.pull(apply=True)  # sessions skipped, never installed on X
    write(home_x / "skills/x.md", b"x's change")
    engine_x.push()  # generation 2 without the sessions; X's syncs == []

    # Y opts in but is fresh — it never had the sessions. It pulls
    # generation 2 (nothing to refuse: it owns none) and pushes its own
    # change; Y's roster entry says syncs == ["sessions"].
    home_y = make_home(tmp_path, "home-y", {"skills/y.md": b"y's change"})
    engine_y = mk_engine(home_y, "dev-y", backend, sync_sessions=True)
    engine_y.pull(apply=True)  # generation 2
    engine_y.push()  # generation 3; publisher Y is opted in

    # Z pulls generation 3: the sessions are in Z's base and absent from
    # the manifest; the guard sees an opted-in LAST publisher and lets
    # the deletion through.
    result = engine_z.pull(apply=True)
    assert (home_z / "sessions/s1.jsonl").exists(), (
        "the opt-in carry-forward guard was laundered: a deletion first "
        "published by a non-opted-in device (X) went through after one "
        "intermediate push by an opted-in device that never had the "
        "content (Y), and Z's session transcript was deleted: "
        f"{json.dumps(result.to_dict())}"
    )


# ------------------------------------------------------------------ t5


def test_t5_doctor_is_green_while_the_remote_is_rolled_back(tmp_path) -> None:
    """With the remote head rewound below this device's floor every pull,
    push and diff is refused — but doctor runs all checks green: it never
    compares the remote generation with the rollback floor."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {"SOUL.md": b"v1"})
    engine_a = mk_engine(home_a, "dev-a", backend)
    engine_a.init(create_remote=True)
    gen0 = backend.get_head()  # the signed generation-0 head
    engine_a.push()  # generation 1

    home_b = make_home(tmp_path, "home-b")
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.pull(apply=True)  # B saw generation 1

    # The remote is rolled back (rewound git branch / stale store copy):
    # still validly signed, but generation 0.
    backend._head = gen0

    with pytest.raises(VerifyError):  # the device really is bricked ...
        engine_b.pull()
    checks = engine_b.doctor()
    assert not all(check.ok for check in checks), (
        "the remote head was rolled back to generation 0 and every pull "
        "and push is refused with a rollback VerifyError, yet `hermes "
        "roam doctor` reports every check green — the health check never "
        "compares the remote generation with the device's sync floor: "
        + "; ".join(f"{c.name}={c.ok}" for c in checks)
    )


# ------------------------------------------------------------------ t6a


def test_t6a_secret_bearing_backups_accumulate_forever(tmp_path) -> None:
    """RULING: R5 (rewritten): backup retention is bounded — roam keeps only
    the newest settings.backup_keep (default 5) backup dirs and prunes the
    older ones (with their committed journals) after every successful
    apply. The original PoC asserted <= 1 dir, which contradicts the ruling's
    retention window; this rewrite asserts the ruled bound: after enough
    pulls the oldest secret-bearing backup dirs are gone and only the
    newest window survives. RED until the engine (g1) calls
    restore.prune_backups with settings.backup_keep after each apply."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {
        "SOUL.md": b"v1",
        "config.yaml": b"model:\n  provider: p-a\n",
    })
    engine_a = mk_engine(home_a, "dev-a", backend)
    init_and_push(engine_a)

    home_b = make_home(tmp_path, "home-b", {
        "config.yaml": b"model:\n  provider: p-b\n  api_key: b-secret-token-xyz\n",
    })
    engine_b = mk_engine(home_b, "dev-b", backend, fake_clock=True)
    engine_b.pull(apply=True)  # the merge backs up B's config (with its secret)

    for i in range(7):  # seven more generations, seven more pulls
        write(home_a / "SOUL.md", f"v{i + 2}".encode())
        engine_a.push()
        engine_b.pull(apply=True)

    keep = getattr(engine_b.settings, "backup_keep", 5)  # R5 default: 5
    backups = home_b / "roam" / "backups"
    dirs = sorted(d.name for d in backups.iterdir() if d.is_dir())
    assert len(dirs) == keep, (
        f"{len(dirs)} backup dirs accumulated across 8 pulls; the ruled "
        f"retention window is keep={keep} (the newest {keep} survive, the "
        "older secret-bearing pre-merge config copies are pruned) — the "
        "engine must call restore.prune_backups(backups, journals, "
        "settings.backup_keep) after every successful apply"
    )


# ------------------------------------------------------------------ t6b


def test_t6b_pruned_backups_make_recovery_destructive(tmp_path, monkeypatch) -> None:
    """When the user prunes the backups (the natural response to t6a), crash
    recovery DESTROYS content: the rollback unlinks the half-installed
    file and the pre-pull version — which existed only in the backup — is
    gone. Recovery must not silently destroy what it cannot restore."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {"SOUL.md": b"v1"})
    engine_a = mk_engine(home_a, "dev-a", backend)
    init_and_push(engine_a)

    home_b = make_home(tmp_path, "home-b")
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.pull(apply=True)  # B has SOUL.md v1, synced at generation 1

    write(home_a / "SOUL.md", b"v2")
    write(home_a / "skills/new.md", b"new")
    engine_a.push()  # generation 2

    # Crash exactly mid-install: journal PHASE_INSTALLING, backup holds v1,
    # home holds the half-installed v2.
    crash_mid_install(engine_b, monkeypatch)
    assert (home_b / "SOUL.md").read_bytes() == b"v2"

    # The user prunes the unbounded backups.
    shutil.rmtree(home_b / "roam" / "backups")

    engine_next = mk_engine(home_b, "dev-b", backend)
    engine_next.status()  # the next command runs crash recovery
    assert (home_b / "SOUL.md").exists(), (
        "crash recovery deleted SOUL.md: the rollback found no backup and "
        "unlinked the half-installed file, destroying the pre-pull v1 "
        "(which existed only in the pruned backup) — recovery silently "
        "destroys content it cannot restore instead of failing loudly"
    )


# ------------------------------------------------------------------ t7


def test_t7_recover_bypasses_the_process_lock(tmp_path, monkeypatch) -> None:
    """Engine.recover() never takes the per-home process lock: while
    another roam command holds the flock it rolls back that command's
    in-flight install."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {"SOUL.md": b"v1"})
    engine_a = mk_engine(home_a, "dev-a", backend)
    init_and_push(engine_a)

    home_b = make_home(tmp_path, "home-b")
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.pull(apply=True)

    write(home_a / "SOUL.md", b"v2")
    write(home_a / "skills/new.md", b"new")
    engine_a.push()
    crash_mid_install(engine_b, monkeypatch)  # a live PHASE_INSTALLING journal

    engine_next = mk_engine(home_b, "dev-b", backend)
    with engine_next._process_lock():  # another roam command holds the lock
        outcomes = None
        try:
            outcomes = engine_next.recover()
        except Exception:  # noqa: BLE001 - the refusal is the desired path
            outcomes = None
    assert outcomes is None, (
        "Engine.recover() ran crash recovery (and rolled back an "
        "in-flight install) while the per-home process lock was held by "
        f"another command: {outcomes!r} — recover() is the only engine "
        "entry point that never takes the lock"
    )


# ------------------------------------------------------------------ t8


def test_t8_init_skips_crash_recovery(tmp_path, monkeypatch) -> None:
    """`hermes roam init` must run crash recovery like every other command
    (round 3 ruling, rewritten): a PHASE_INSTALLING journal must not survive
    init, and the half-applied home must be rolled back."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {"SOUL.md": b"v1"})
    engine_a = mk_engine(home_a, "dev-a", backend)
    init_and_push(engine_a)

    home_b = make_home(tmp_path, "home-b")
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.pull(apply=True)

    write(home_a / "SOUL.md", b"v2")
    write(home_a / "skills/new.md", b"new")
    engine_a.push()
    crash_mid_install(engine_b, monkeypatch)

    engine_next = mk_engine(home_b, "dev-b", backend)
    engine_next.init(create_remote=False)
    journals = sorted((home_b / "roam" / "journals").glob("*.json"))
    phases = [json.loads(path.read_bytes()).get("phase") for path in journals]
    assert "installing" not in phases, (
        "`hermes roam init` left a PHASE_INSTALLING journal and its "
        "half-applied home untouched: crash recovery does not run at the "
        "start of every command, only at push/pull/status/diff/doctor"
    )
    assert (home_b / "SOUL.md").read_bytes() == b"v1", (
        "the half-applied home survived `hermes roam init` unrecovered"
    )


# ------------------------------------------------------------------ t9


def test_t9_state_json_deletion_resets_the_rollback_guard(tmp_path) -> None:
    """Deleting <home>/roam/state.json resets the rollback-guard floor —
    exactly what DISCLOSURES §4 tells the user to do when migrating.
    Round 3 (R4) ruling, rewritten: the floor lives in roam/floor.json and
    outlives state.json, so the rolled-back remote must be REFUSED outright
    — deleted content cannot be resurrected by deleting state.json."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {
        "SOUL.md": b"v1", "skills/old.md": b"old content",
    })
    engine_a = mk_engine(home_a, "dev-a", backend)
    init_and_push(engine_a)
    gen1 = backend.get_head()  # the signed generation-1 head

    home_b = make_home(tmp_path, "home-b")
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.pull(apply=True)  # B has skills/old.md at generation 1

    # The remote legitimately deletes skills/old.md; B follows.
    (home_a / "skills/old.md").unlink()
    write(home_a / "SOUL.md", b"v2")
    engine_a.push()  # generation 2 without skills/old.md
    engine_b.pull(apply=True)
    assert not (home_b / "skills/old.md").exists()

    # The remote is rolled back to generation 1 and the user deletes
    # state.json (the documented backend-migration step).
    backend._head = gen1
    (home_b / "roam" / "state.json").unlink()

    with pytest.raises(VerifyError) as excinfo:
        engine_b.pull(apply=True)
    assert "rolled back or is serving stale data" in str(excinfo.value), (
        "after state.json was deleted the rolled-back generation-1 head was "
        "pulled without any rollback refusal — the floor must live in "
        "roam/floor.json and outlive state.json"
    )
    assert not (home_b / "skills/old.md").exists(), (
        "content the fleet had deleted was resurrected on this device"
    )


# ------------------------------------------------------------------ t10


def test_t10_config_conflicts_are_silently_discarded(tmp_path) -> None:
    """config.yaml has no conflict detection at all: a local edit of a
    shared (non-host-scoped) config key — and a shared key that exists
    only locally — is silently dropped by the merge, with no conflict
    flag, and the local value can never be pushed again."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {
        "SOUL.md": b"v1",
        "config.yaml": b"model:\n  provider: p-a\ncustom:\n  shared_setting: a-v1\n",
    })
    engine_a = mk_engine(home_a, "dev-a", backend)
    init_and_push(engine_a)

    home_b = make_home(tmp_path, "home-b")
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.pull(apply=True)  # B's config now carries custom.shared_setting: a-v1

    # B edits the shared setting locally AND adds a brand-new shared key.
    write(home_b / "config.yaml",
          b"model:\n  provider: p-b\ncustom:\n  shared_setting: b-edit\n  new_key: b-only\n")

    # A changes the same shared setting remotely.
    write(home_a / "config.yaml",
          b"model:\n  provider: p-a\ncustom:\n  shared_setting: a-v2\n")
    write(home_a / "SOUL.md", b"v2")
    engine_a.push()  # generation 2

    result = engine_b.pull(apply=True)
    b_config = (home_b / "config.yaml").read_text()
    assert ("b-edit" in b_config or "new_key" in b_config) or result.conflicts, (
        "the config merge silently took the remote value for the shared "
        "setting (a-v2), dropped B's local edit (b-edit) AND dropped the "
        "shared key that existed only locally (new_key); the pull flags "
        "no conflict for config.yaml — unlike every regular file, local "
        f"config work is discarded without a word: {json.dumps(result.to_dict())}"
    )
    # And the loss is permanent: the stripped config now equals the
    # remote's, so a push would carry nothing.
    push = engine_b.push()
    assert push.skipped_reason != "nothing to push", (
        "after the merge B's stripped config equals the remote's, so the "
        "local config work can never be published"
    )


# ------------------------------------------------------------------ clean probes


def test_c1_token_like_shared_settings_are_not_stripped() -> None:
    """False-positive probe: legitimate shared settings whose names contain
    'token'/'key' must keep syncing (they must NOT be stripped)."""
    host_keys = list(scope.Rules.default().host_keys)
    config = "model:\n  max_tokens: 8192\n  token_budget: 1000\n  cache_key_strategy: lru\n"
    stripped = scope.strip_host_keys(config, host_keys)
    for name in ("max_tokens", "token_budget", "cache_key_strategy"):
        assert name in stripped, (
            f"false positive: shared setting {name} was stripped and "
            "would silently never sync"
        )


def test_c2_setuid_and_sticky_bits_are_dropped_on_install(tmp_path) -> None:
    """Mode probe: the manifest carries real permission bits but setuid and
    sticky must not survive the install."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {"SOUL.md": b"v1"})
    engine_a = mk_engine(home_a, "dev-a", backend)
    init_and_push(engine_a)

    home_b = make_home(tmp_path, "home-b")
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.pull(apply=True)
    mode = (home_b / "SOUL.md").stat().st_mode
    assert mode & 0o777 == 0o644 and not mode & 0o7000, (
        f"installed file carries special bits: {oct(mode)}"
    )


def test_c3_known_credential_keys_are_stripped_at_any_depth() -> None:
    """Guard probe: the credential patterns that DO exist must keep
    working, nested and in mcp env blocks."""
    host_keys = list(scope.Rules.default().host_keys)
    config = (
        "mcp_servers:\n"
        "  fetch:\n"
        "    command: /usr/bin/fetch\n"
        "    env:\n"
        "      OPENAI_API_KEY: sk-abcdef1234567890\n"
        "      GITHUB_TOKEN: ghp_abcdef1234567890\n"
        "    headers:\n"
        "      Authorization: Bearer abcdef\n"
    )
    stripped = scope.strip_host_keys(config, host_keys)
    for secret in ("sk-abcdef1234567890", "ghp_abcdef1234567890", "Bearer abcdef"):
        assert secret not in stripped, f"known credential {secret!r} travelled"


def test_c4_roster_tampering_fails_head_verification(tmp_path) -> None:
    """Trust probe: the devices roster is inside the signed head, so an
    attacker without the archive key cannot forge a 'syncs' entry."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {"SOUL.md": b"v1"})
    engine_a = mk_engine(home_a, "dev-a", backend)
    init_and_push(engine_a)
    pair = backend.get_head()
    assert pair is not None
    head = json.loads(pair[0])
    head["devices"]["dev-a"]["syncs"] = ["sessions", "cron"]
    with pytest.raises(VerifyError):
        head_mod.verify_head(json.dumps(head).encode(), KEY)
