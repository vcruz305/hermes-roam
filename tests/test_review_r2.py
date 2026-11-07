"""Adversarial review lane r2 — engine / restore / config-merge data loss and correctness.

Every test in this file documents a real defect found by review lane r2 and is
written to assert the CORRECT behaviour, so each one FAILS against the current
production code (commit fe2558a).  The tests drive the REAL roam pipeline
(real scope / inventory / archive / restore modules, real AES-GCM bundles and
real signed heads) against the in-memory FakeBackend, which is exactly the
production wiring minus the network.

Findings (test name -> finding):
  f1  a crash (SIGKILL / power loss) mid pull --apply is never recovered:
      Engine.recover() exists but has no caller and no CLI verb, so a
      half-applied home and a PHASE_INSTALLING journal survive every
      subsequent start.
  f2  pull --apply deletes a file the user recreated locally at a path the
      remote deleted, without any conflict flag.
  f3  pull --apply silently clobbers local edits made since the last sync
      (no local-dirty check, no warning).
  f4  secrets inside config.yaml (model.api_key, platforms.telegram.token)
      are stripped of nothing and travel in the bundle.
  f5  profiles/<name>/config.yaml is classified SHARED and synced verbatim:
      no host-key stripping, no secret stripping, no home-path stripping.
  f6  a local roam/rules.yaml cannot block incoming remote files: the
      remote manifest's scope wins over the user's local rules.
  f7  a device without the sessions opt-in pushes a manifest that drops
      synced sessions from the remote; every later pull deletes them
      everywhere (fleet-wide data loss).
  f8  file modes are flattened: a 0600 private memory is recorded as 0644
      and installed 0644 on every other device.
  f9  a stale <home>/roam/.state.json.tmp (crash mid-save, or two concurrent
      roam processes) makes every future save crash with FileExistsError;
      pull --apply installs the files first and then dies before recording
      state, leaving the device applied-but-unsynced forever.
  f10 home-path stripping misses values inside lists and values that are the
      home directory itself, so they travel and clobber local values on pull.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import load_plugin_package
from fakes.fake_backend import FakeBackend

from roam import head as head_mod
from roam import inventory, restore, scope
from roam.engine import Engine
from roam.model import RoamSettings, Scope

KEY = bytes(range(32))  # real entropy: weak keys are rejected
VERSION = "0.22.5"


# ------------------------------------------------------------------ helpers


def mk_settings(device: str, **kw) -> RoamSettings:
    return RoamSettings(backend="fake", device_name=device, archive_key=KEY, **kw)


def mk_engine(home: Path, device: str, backend: FakeBackend, **kw) -> Engine:
    # No pipeline= argument: the engine must use the REAL roam pipeline.
    return Engine(home, mk_settings(device, **kw), backend, VERSION)


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


def signed_files(backend: FakeBackend) -> dict:
    pair = backend.get_head()
    assert pair is not None
    return head_mod.verify_head(pair[0], KEY).files


def conflict_flagged(result) -> bool:
    """Whether a pull result surfaces a local-vs-remote conflict at all.
    Only the plan and the notes count — not the backup_dir path, which is
    just a filesystem location."""
    text = json.dumps(result.plan.to_dict()).lower() + " " + " ".join(result.notes).lower()
    return any(word in text for word in ("conflict", "locally", "unsaved", "dirty"))


# ----------------------------------------------------------------- f1


def test_f1_crash_mid_apply_is_never_recovered(tmp_path, monkeypatch) -> None:
    """A SIGKILL during pull --apply leaves a half-applied home; the next
    start of every public surface must consume the stuck journal. Today
    nothing does: Engine.recover() has no caller and no CLI verb."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {
        "skills/a/SKILL.md": b"remote a",
        "skills/b/SKILL.md": b"remote b",
    })
    engine_a = mk_engine(home_a, "dev-a", backend)
    init_and_push(engine_a)

    home_b = make_home(tmp_path, "home-b")
    engine_b = mk_engine(home_b, "dev-b", backend)

    # Simulate the process dying mid-install: SystemExit is a BaseException,
    # so restore.apply's `except Exception` rollback does NOT run — the
    # on-disk state is exactly what a SIGKILL / power loss leaves behind.
    real_install = restore._install_from_staged
    installs: list[Path] = []

    def kill_after_first(target, staged, mode):
        installs.append(target)
        if len(installs) == 2:
            raise SystemExit(137)  # the process dies here
        real_install(target, staged, mode)

    monkeypatch.setattr(restore, "_install_from_staged", kill_after_first)
    with pytest.raises(SystemExit):
        engine_b.pull(apply=True)

    journals = sorted((home_b / "roam" / "journals").glob("*.json"))
    assert len(journals) == 1  # the crash journal exists ...
    assert installs  # ... after a real partial install

    # Next start of the device: run every public, non-destructive surface.
    engine_next = mk_engine(home_b, "dev-b", backend)
    engine_next.status()
    engine_next.pull()  # plan only, nothing written
    engine_next.diff()

    record = json.loads(journals[0].read_bytes())
    # Desired: the stuck journal must have been consumed (rolled back or
    # otherwise resolved) by the next start, or a CLI verb must exist to
    # run recovery. Today it is still "installing" and there is no verb.
    plugin = load_plugin_package()
    import argparse

    verbs = set()
    for action in plugin.cli.make_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            verbs = set(action.choices)
    assert "recover" in verbs or record["phase"] != "installing", (
        f"a crash mid-apply leaves phase={record['phase']!r} forever; "
        f"Engine.recover() has no caller and no CLI verb (verbs={sorted(verbs)})"
    )


# ----------------------------------------------------------------- f2


def test_f2_pull_deletes_user_recreated_file(tmp_path) -> None:
    """The remote deletes skills/x.md; the user recreates skills/x.md locally
    with new content after the last sync. pull --apply must not silently
    destroy that new local work (or must flag it as a conflict)."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {
        "skills/x.md": b"v1", "skills/keep.md": b"keep",
    })
    engine_a = mk_engine(home_a, "dev-a", backend)
    init_and_push(engine_a)  # generation 1

    home_b = make_home(tmp_path, "home-b")
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.pull(apply=True)  # B is in sync with generation 1

    # A deletes the file remotely; B recreates it locally with new work.
    (home_a / "skills/x.md").unlink()
    engine_a.push()  # generation 2 without skills/x.md
    write(home_b / "skills/x.md", b"user's brand new local work")

    result = engine_b.pull(apply=True)
    assert (home_b / "skills/x.md").exists() or conflict_flagged(result), (
        "pull --apply deleted the user's locally recreated file at a "
        f"remotely-deleted path: {json.dumps(result.to_dict())}"
    )


# ----------------------------------------------------------------- f3


def test_f3_pull_clobbers_local_edits_without_warning(tmp_path) -> None:
    """Both sides edit SOUL.md after the last sync. pull --apply must not
    silently overwrite the local edit (or must flag the conflict)."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {"SOUL.md": b"v1"})
    engine_a = mk_engine(home_a, "dev-a", backend)
    init_and_push(engine_a)  # generation 1

    home_b = make_home(tmp_path, "home-b")
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.pull(apply=True)  # B has SOUL.md = v1

    write(home_b / "SOUL.md", b"vB local edit")  # local work since last sync
    write(home_a / "SOUL.md", b"v2")  # remote change
    engine_a.push()  # generation 2

    result = engine_b.pull(apply=True)
    content = (home_b / "SOUL.md").read_bytes()
    assert content == b"vB local edit" or conflict_flagged(result), (
        f"pull --apply silently overwrote the local edit with remote bytes "
        f"({content!r}) and the result flags no conflict: "
        f"{json.dumps(result.to_dict())}"
    )


# ----------------------------------------------------------------- f4


SECRET_CONFIG = """model:
  provider: p-a
  api_key: sk-live-abcdef123
platforms:
  telegram:
    token: "123456:ABCDEF"
"""


def test_f4_config_secrets_travel_in_the_bundle(tmp_path) -> None:
    """Secrets inside config.yaml are neither stripped on push nor kept local
    on pull: they must not survive strip_host_keys and must not land on the
    other device."""
    host_keys = list(scope.Rules.default().host_keys)
    stripped = scope.strip_host_keys(SECRET_CONFIG, host_keys)
    assert "sk-live-abcdef123" not in stripped and "123456:ABCDEF" not in stripped, (
        "strip_host_keys keeps model.api_key and platforms.telegram.token; "
        "the pushed config.yaml carries the secrets"
    )

    # End to end: the secret really leaves the machine and lands on device B.
    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {"config.yaml": SECRET_CONFIG.encode()})
    engine_a = mk_engine(home_a, "dev-a", backend)
    init_and_push(engine_a)
    home_b = make_home(tmp_path, "home-b")
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.pull(apply=True)
    b_config = (home_b / "config.yaml").read_text()
    assert "sk-live-abcdef123" not in b_config, (
        "the api_key from device A's config.yaml was installed into device B"
    )


# ----------------------------------------------------------------- f5


PROFILE_CONFIG = """model:
  provider: p-a
  api_key: sk-live-profile-secret
terminal:
  theme: dark-a
browser:
  executable: /root/bin/browser-a
"""


def test_f5_profile_config_yaml_syncs_verbatim(tmp_path) -> None:
    """profiles/<name>/config.yaml is classified SHARED and never goes through
    strip_host_keys: host paths and secrets inside it must not arrive on the
    other device verbatim.

    RULING (fix round 2): profiles/<name>/config.yaml STAYS SHARED — it
    travels — but it is a config path like the top-level config.yaml, so the
    engine must strip it on push and merge it on pull exactly the same way.
    The scope-level part is asserted here; the engine wiring is lane f2's.
    """
    rules = scope.Rules.default()
    assert scope.classify_path("profiles/p/config.yaml", rules) is Scope.SHARED, (
        "the ruling keeps profiles/<name>/config.yaml SHARED (it travels)"
    )
    assert scope.is_config_path("profiles/p/config.yaml"), (
        "profiles/<name>/config.yaml is not marked as a config path: the "
        "engine cannot strip/merge it like the top-level config.yaml"
    )
    assert scope.classify_path("profiles/p/.env", rules) is not Scope.SHARED

    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {
        "profiles/p/config.yaml": PROFILE_CONFIG.encode(),
        "profiles/p/.env": b"ROAM_ARCHIVE_KEY=local",
    })
    engine_a = mk_engine(home_a, "dev-a", backend)
    init_and_push(engine_a)

    files = signed_files(backend)
    assert "profiles/p/.env" not in files  # profile .env stays local ...
    assert "profiles/p/config.yaml" in files  # ... but the profile config is bundled

    home_b = make_home(tmp_path, "home-b")
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.pull(apply=True)
    pulled = home_b / "profiles/p/config.yaml"
    assert pulled.exists(), (
        "the ruling keeps profiles/<name>/config.yaml SHARED, so a pull must "
        "deliver it (stripped), not skip it"
    )
    assert (
        "/root/bin/browser-a" not in pulled.read_text()
        and "sk-live-profile-secret" not in pulled.read_text()
    ), (
        "the profile's config.yaml arrived verbatim: host paths and secrets "
        "included, with no host-key stripping at all"
    )


# ----------------------------------------------------------------- f6


def test_f6_local_rules_cannot_block_remote_files(tmp_path) -> None:
    """A local rules.yaml marks secret-notes/** local; a pull must not install
    a remote file under it. Today the remote manifest's scope wins."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {"secret-notes/x.md": b"remote secret"})
    engine_a = mk_engine(home_a, "dev-a", backend)
    init_and_push(engine_a)

    home_b = make_home(tmp_path, "home-b", {
        "roam/rules.yaml": b"local:\n  - secret-notes/**\n",
    })
    engine_b = mk_engine(home_b, "dev-b", backend)
    result = engine_b.pull(apply=True)
    assert not (home_b / "secret-notes/x.md").exists(), (
        "the local rules.yaml says secret-notes/** is local, but pull --apply "
        f"installed the remote file anyway: {json.dumps(result.to_dict())}"
    )


# ----------------------------------------------------------------- f7


def test_f7_non_optin_device_wipes_sessions_fleet_wide(tmp_path) -> None:
    """# RULING: (R1, round 3) the roster 'syncs' guard is gone: a device that
    cannot see a path can never delete it. B's push must carry the sessions
    entry forward from roam/last.roam byte-identical, so the signed
    manifest keeps listing it and A's pull never deletes A's transcripts."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {
        "sessions/s1.jsonl": b'{"turn": 1}',
        "skills/keep/SKILL.md": b"keep",
    })
    engine_a = mk_engine(home_a, "dev-a", backend, sync_sessions=True)
    init_and_push(engine_a)  # generation 1 carries sessions/s1.jsonl

    home_b = make_home(tmp_path, "home-b")
    engine_b = mk_engine(home_b, "dev-b", backend)  # sync_sessions=False
    engine_b.pull(apply=True)  # sessions skipped, not installed on B

    write(home_b / "skills/new/SKILL.md", b"b's own work")
    engine_b.push()  # generation 2: the sessions entry is carried forward
    files = signed_files(backend)
    assert "sessions/s1.jsonl" in files, (
        "device B's push dropped the sessions entry it cannot see instead "
        "of carrying it forward from roam/last.roam: every opted-in "
        "device's next pull would delete its local session files"
    )
    base_entry = files["sessions/s1.jsonl"]
    assert base_entry.get("sha256") and base_entry.get("size"), (
        "the carried-forward entry lost its content identity"
    )

    engine_a.pull(apply=True)  # A pulls the generation B published
    assert (home_a / "sessions/s1.jsonl").exists(), (
        "device A's session transcript was deleted from A's home by pulling "
        "the manifest a non-opted-in device pushed"
    )


# ----------------------------------------------------------------- f8


def test_f8_private_file_modes_are_flattened_to_0644(tmp_path) -> None:
    """A 0600 private memory must not become 0644 on the other device."""
    rules = scope.Rules.default()
    probe = tmp_path / "mode-probe"
    write(probe / "memories/private.md", b"private", 0o600)
    entries = inventory.walk_home(probe, rules, include={Scope.SHARED})
    modes = {e.relpath: e.mode for e in entries}
    assert modes.get("memories/private.md") == 0o600, (
        f"walk_home records a 0600 file as {oct(modes.get('memories/private.md', 0))}; "
        "the manifest can only ever carry 0644/0755"
    )

    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {"memories/private.md": b"private"},
                       modes={"memories/private.md": 0o600})
    engine_a = mk_engine(home_a, "dev-a", backend)
    init_and_push(engine_a)
    home_b = make_home(tmp_path, "home-b")
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.pull(apply=True)
    pulled = home_b / "memories/private.md"
    assert pulled.exists() and (pulled.stat().st_mode & 0o777) == 0o600, (
        f"the 0600 memory arrived as {oct(pulled.stat().st_mode & 0o777)} — "
        "private memories become world-readable on every pull"
    )


# ----------------------------------------------------------------- f9


def test_f9_stale_state_tmp_breaks_all_future_saves(tmp_path) -> None:
    """A leftover <home>/roam/.state.json.tmp (crash mid-save or a concurrent
    roam process — the tmp name is fixed) must not wedge the device: state
    saving must recover, and a pull --apply must not install files and then
    die before recording the state."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "home-a", {"SOUL.md": b"remote soul"})
    engine_a = mk_engine(home_a, "dev-a", backend)
    init_and_push(engine_a)

    home_b = make_home(tmp_path, "home-b")
    write(home_b / "roam/.state.json.tmp", b"stale tmp from a crashed save")
    engine_b = mk_engine(home_b, "dev-b", backend)

    raised = None
    try:
        engine_b.pull(apply=True)
    except FileExistsError as exc:  # engine.save_state: open(tmp, "xb")
        raised = exc
    assert raised is None, (
        "pull --apply raised FileExistsError from save_state: the files were "
        "already installed but the state was never recorded, and no code "
        "ever cleans the stale tmp — the device is wedged until someone "
        "deletes it by hand"
    )
    assert (home_b / "roam/state.json").is_file(), (
        "the pull result was never recorded in state.json"
    )


# ----------------------------------------------------------------- f10


LIST_CONFIG_REMOTE = """model:
  provider: p
mcp_servers:
  fetch:
    command: /root/bin/fetch
    args:
      - /root/leak-marker/server.py
"""
LIST_CONFIG_LOCAL = """model:
  provider: p
mcp_servers:
  fetch:
    command: /home/b/bin/fetch
    args:
      - /home/b/leak-marker/server.py
"""
EXACT_HOME_CONFIG = r"""custom:
  workdir: "/root"
  alt_workdir: "C:\\Users\\bob"
"""


def test_f10_home_paths_in_lists_and_exact_home_dirs_travel(tmp_path) -> None:
    """Home-directory values inside lists, and values that are the home
    directory itself, must be treated like every other home path: stripped
    on push, local value kept on pull."""
    host_keys = list(scope.Rules.default().host_keys)

    stripped = scope.strip_host_keys(LIST_CONFIG_REMOTE, host_keys)
    assert "/root/leak-marker/server.py" not in stripped, (
        "a home path inside a list (mcp_servers.fetch.args) survives "
        "strip_host_keys and is pushed"
    )

    stripped_exact = scope.strip_host_keys(EXACT_HOME_CONFIG, host_keys)
    assert "/root" not in stripped_exact and "C:\\Users\\bob" not in stripped_exact, (
        "values equal to the home directory itself (/root, C:\\Users\\bob) "
        "survive strip_host_keys because _HOME_PATH_RE demands a subpath"
    )

    merged = scope.merge_config(LIST_CONFIG_REMOTE, LIST_CONFIG_LOCAL, host_keys)
    assert "/home/b/leak-marker/server.py" in merged, (
        "merge_config overwrites the local (correct-for-this-host) list value "
        "with the remote one — the local value is silently lost"
    )
