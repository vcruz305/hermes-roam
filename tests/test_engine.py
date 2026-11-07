"""Engine tests: two devices, one FakeBackend, honest pipeline fake."""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fakes.fake_backend import FakeBackend
from fakes.fake_pipeline import PIPELINE

from roam import head as head_mod
from roam import restore
from roam.backends.base import Backend
from roam.engine import Engine
from roam.errors import BackendError, RemoteAhead, RestoreError, RoamError, VerifyError, VersionMismatch
from roam.model import Head, RoamSettings, Scope

KEY = hashlib.sha256(b"engine-test-archive-key").digest()  # real entropy: weak keys are rejected
VERSION = "0.22.5"

CONFIG_A = """model:
  provider: p-a
  temperature: 0.7
terminal:
  theme: dark-a
  font: mono-a
browser:
  executable: /root/bin/browser-a
mcp_servers:
  fetch:
    command: /root/bin/fetch-a
"""
CONFIG_B = """model:
  provider: p-b
  temperature: 0.2
terminal:
  theme: light-b
  font: sans-b
browser:
  executable: /usr/local/bin/browser-b
"""
SOUL_A = b"A's soul, definitely not B's"
SKILL_A = b"# skill from A"
TOOL_A = b"#!/bin/sh\necho tool\n"


def make_settings(device: str) -> RoamSettings:
    return RoamSettings(backend="fake", device_name=device, archive_key=KEY)


def make_engine(home: Path, device: str, backend: Backend, version: str = VERSION) -> Engine:
    return Engine(home, make_settings(device), backend, version, pipeline=PIPELINE)


def write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, mode)


def build_home_a(tmp_path: Path) -> Path:
    home = tmp_path / "home-a"
    write(home / "config.yaml", CONFIG_A.encode())
    write(home / "SOUL.md", SOUL_A)
    write(home / "skills/roam/SKILL.md", SKILL_A)
    write(home / "skills/tool/run.sh", TOOL_A, 0o755)
    write(home / "memories/mem.md", b"A memory")
    write(home / ".env", b"A_SECRET=1")
    write(home / "cache/junk.txt", b"cache")
    write(
        home / "plugins/foo/plugin.yaml",
        b"name: foo\nversion: 1.2.3\n",
    )
    write(home / "plugins/foo/.git/HEAD", b"ref: refs/heads/main\n")
    write(home / "plugins/foo/.git/refs/heads/main", b"a" * 40)
    return home


def build_home_b(tmp_path: Path) -> Path:
    home = tmp_path / "home-b"
    write(home / "config.yaml", CONFIG_B.encode())
    return home


def fleet(tmp_path: Path) -> tuple:
    backend = FakeBackend()
    home_a = build_home_a(tmp_path)
    home_b = build_home_b(tmp_path)
    engine_a = make_engine(home_a, "dev-a", backend)
    engine_b = make_engine(home_b, "dev-b", backend)
    return backend, home_a, home_b, engine_a, engine_b


def tree_hash(root: Path, skip_top: str | None = None) -> list:
    items = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        if dirpath == str(root) and skip_top in dirnames:
            dirnames.remove(skip_top)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            items.append(
                (
                    path.relative_to(root).as_posix(),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    oct(path.stat().st_mode & 0o7777),
                )
            )
    return items


def test_init_creates_generation_zero_head(tmp_path: Path) -> None:
    backend, _, _, engine_a, engine_b = fleet(tmp_path)
    assert "generation-0" in engine_a.init(create_remote=True)
    pair = backend.get_head()
    assert pair is not None
    signed = head_mod.verify_head(pair[0], KEY)
    assert signed.generation == 0
    assert signed.bundle_key == ""
    assert "dev-a" in signed.devices
    assert engine_a.load_state().last_seen_generation == 0
    summary = engine_b.init(create_remote=False)
    assert "generation 0" in summary
    # init never advances last_seen: pushing without pulling must stay blocked
    assert engine_b.load_state().last_seen_generation == 0


def test_push_pull_round_trip(tmp_path: Path) -> None:
    backend, home_a, home_b, engine_a, engine_b = fleet(tmp_path)
    engine_a.init(create_remote=True)
    push = engine_a.push()
    assert push.generation == 1
    assert push.bundle_key.startswith("bundles/000001-dev-a-")
    assert backend.blob_exists(push.bundle_key)

    engine_b.init(create_remote=False)
    result = engine_b.pull(apply=True)
    assert result.generation == 1
    assert result.applied
    assert result.config_merged
    assert result.plugins_to_install == ["foo"]
    # shared files arrive byte-identical, modes included
    assert (home_b / "SOUL.md").read_bytes() == SOUL_A
    assert (home_b / "skills/roam/SKILL.md").read_bytes() == SKILL_A
    assert (home_b / "skills/tool/run.sh").read_bytes() == TOOL_A
    assert (home_b / "skills/tool/run.sh").stat().st_mode & 0o777 == 0o755
    assert (home_b / "memories/mem.md").read_bytes() == b"A memory"
    # local/ignored/plugin code never travels
    assert not (home_b / ".env").exists()
    assert not (home_b / "cache").exists()
    assert not (home_b / "plugins/foo/plugin.yaml").exists()
    assert not (home_b / "roam/installed-plugins.json").is_file()
    # config merge (0.4.2 first-pull union): B never accepted a shared
    # projection before, so the remote's shared keys are added where B's
    # file lacks them and B's own values win where both sides set the same
    # key — nothing of A's arrives here because B sets every shared key A
    # carries; host-scoped keys stay B's either way
    merged_text = (home_b / "config.yaml").read_text()
    merged = yaml.safe_load(merged_text)
    assert merged["model"] == {"provider": "p-b", "temperature": 0.2}
    assert merged["terminal"] == {"theme": "light-b", "font": "sans-b"}
    assert merged["browser"] == {"executable": "/usr/local/bin/browser-b"}
    assert "/root/" not in merged_text
    assert any(
        "never synced here" in note and "model.provider" in note for note in result.notes
    ), result.notes
    # state and backups
    state = engine_b.load_state()
    assert state.last_pulled_generation == 1
    assert state.last_seen_generation == 1
    assert state.base_files  # pinned to the pulled head
    assert Path(result.backup_dir).is_dir()
    original_config = (Path(result.backup_dir) / "config.yaml").read_text()
    assert yaml.safe_load(original_config)["terminal"] == {"theme": "light-b", "font": "sans-b"}


def test_status_clean_after_pull_and_nothing_to_push(tmp_path: Path) -> None:
    backend, _, _, engine_a, engine_b = fleet(tmp_path)
    engine_a.init(create_remote=True)
    engine_a.push()
    engine_b.init(create_remote=False)
    ahead = engine_b.status()
    assert ahead.remote_ahead is True
    assert ahead.remote_generation == 1
    engine_b.pull(apply=True)
    settled = engine_b.status()
    assert settled.remote_ahead is False
    # 0.4.2: the first-pull union kept this device's own shared model
    # values, so the home is honestly dirty until they are published —
    # "local wins" is visible, never silent
    assert settled.local_dirty is True
    assert settled.dirty_count == 1
    # pulls never write the device into the head's device map; only pushes do.
    # The pull-only device still shows up, synthesized from local state.
    assert {d.name for d in settled.devices} == {"dev-a", "dev-b"}
    by_name = {d.name: d for d in settled.devices}
    assert by_name["dev-b"].last_pull_generation == 1
    assert by_name["dev-b"].last_push_generation == 0
    # publishing the kept values is the resolution; afterwards the device is
    # clean and a push with nothing to push is skipped again
    assert engine_b.push().generation == 2
    settled = engine_b.status()
    assert settled.local_dirty is False
    assert settled.dirty_count == 0
    assert engine_b.push().skipped_reason == "nothing to push"
    assert backend.get_head()[1] == "fake-token-3"  # only B's real gen-2 push moved it


def test_remote_ahead_refusal_then_pull_and_push(tmp_path: Path) -> None:
    backend, _, home_b, engine_a, engine_b = fleet(tmp_path)
    engine_a.init(create_remote=True)
    engine_a.push()
    engine_b.init(create_remote=False)
    with pytest.raises(RemoteAhead):
        engine_b.push()
    engine_b.pull(apply=True)
    # 0.4.2: the first-pull union kept this device's own shared model
    # values, so there IS something to push — publishing them is the
    # local-wins resolution and stays visible in status until it happens
    assert engine_b.status().local_dirty is True
    write(home_b / "skills/from-b/SKILL.md", b"B's own skill")
    pushed = engine_b.push()
    assert pushed.generation == 2
    pair = backend.get_head()
    signed = head_mod.verify_head(pair[0], KEY)
    assert set(signed.devices) == {"dev-a", "dev-b"}
    assert signed.devices["dev-b"]["last_push_generation"] == 2
    assert signed.parent_generation == 1
    # A is now behind and status says so
    assert engine_a.status().remote_ahead is True


def test_push_roster_carries_the_device_own_last_pull(tmp_path: Path) -> None:
    """Pulls never write the remote roster, so the next push must record this
    device's last pull from LOCAL state — copying the previous remote entry
    showed `hermes roam devices` pulled 0 for devices that had pulled."""
    backend, _, home_b, engine_a, engine_b = fleet(tmp_path)
    engine_a.init(create_remote=True)
    engine_a.push()
    engine_b.init(create_remote=False)
    engine_b.pull(apply=True)
    assert engine_b.load_state().last_pulled_generation == 1
    write(home_b / "skills/from-b/SKILL.md", b"B's own skill")
    engine_b.push()
    pair = backend.get_head()
    signed = head_mod.verify_head(pair[0], KEY)
    assert signed.devices["dev-b"]["last_push_generation"] == 2
    assert signed.devices["dev-b"]["last_pull_generation"] == 1
    # the roster is what `hermes roam devices` prints on the OTHER device
    roster = {d.name: d for d in engine_a.status().devices}
    assert roster["dev-b"].last_pull_generation == 1
    assert roster["dev-b"].last_push_generation == 2


def test_cas_race_leaves_blob_and_state_untouched(tmp_path: Path) -> None:
    backend, home_a, _, engine_a, _ = fleet(tmp_path)
    engine_a.init(create_remote=True)
    engine_a.push()
    write(home_a / "SOUL.md", b"A changed the soul")
    original_put_head = backend.put_head

    def racing(data: bytes, expected: str | None, *, deadline: float | None = None) -> str:
        imposter = head_mod.sign_head(
            Head(
                generation=2,
                device="device-c",
                hermes_version=VERSION,
                created_at=1.0,
                bundle_key="bundles/000002-device-c-x.roam",
            ),
            KEY,
        )
        backend._head = (head_mod.encode_head(imposter), "fake-token-c")
        return original_put_head(data, expected, deadline=deadline)

    backend.put_head = racing
    with pytest.raises(RemoteAhead) as excinfo:
        engine_a.push()
    assert "device-c" in str(excinfo.value)
    # the blob was uploaded before the head (blob-first ordering) and stays behind
    assert any(key.startswith("bundles/000002-") for key in backend._blobs)
    # local state is untouched: still pinned to generation 1
    state = engine_a.load_state()
    assert state.last_seen_generation == 1
    assert state.last_pushed_generation == 1


def test_pull_deletes_only_previously_synced_files(tmp_path: Path) -> None:
    _, home_a, home_b, engine_a, engine_b = fleet(tmp_path)
    engine_a.init(create_remote=True)
    engine_a.push()
    engine_b.init(create_remote=False)
    engine_b.pull(apply=True)
    assert (home_b / "skills/roam/SKILL.md").is_file()
    # A removes one file and adds another; B creates an unmanaged file locally
    (home_a / "skills/roam/SKILL.md").unlink()
    write(home_a / "skills/evil/SKILL.md", b"replacement skill")
    engine_a.push()
    write(home_b / "unmanaged-notes.md", b"B's private notes")
    result = engine_b.pull(apply=True)
    assert result.generation == 2
    assert not (home_b / "skills/roam/SKILL.md").exists()
    assert (home_b / "skills/evil/SKILL.md").read_bytes() == b"replacement skill"
    assert (home_b / "unmanaged-notes.md").read_bytes() == b"B's private notes"
    # the plan reported the removal under the shared scope
    assert result.plan.removed.get(Scope.SHARED) == ["skills/roam/SKILL.md"]


def test_dry_run_touches_nothing(tmp_path: Path) -> None:
    backend, home_a, home_b, engine_a, engine_b = fleet(tmp_path)
    engine_a.init(create_remote=True)
    engine_a.push()
    engine_b.init(create_remote=False)
    before_b = tree_hash(home_b, skip_top="roam")
    plan = engine_b.pull(dry_run=True)
    assert plan.applied is False
    assert plan.generation == 1
    assert "SOUL.md" in plan.plan.added.get(Scope.SHARED, [])
    assert tree_hash(home_b, skip_top="roam") == before_b  # user content untouched
    # push dry_run on A with a real change: reported, not performed
    write(home_a / "SOUL.md", b"A changed the soul")
    before_a = tree_hash(home_a, skip_top="roam")
    dry = engine_a.push(dry_run=True)
    assert dry.dry_run is True
    assert dry.generation == 2
    assert tree_hash(home_a, skip_top="roam") == before_a
    assert len(backend._blobs) == 1
    assert engine_a.load_state().last_pushed_generation == 1


def test_pull_rejects_tampering(tmp_path: Path) -> None:
    backend, _, _, engine_a, engine_b = fleet(tmp_path)
    engine_a.init(create_remote=True)
    engine_a.push()
    engine_b.init(create_remote=False)
    # tampered head
    original_data, token = backend.get_head()
    backend._head = (original_data[:-40] + b"0" * 40, token)
    with pytest.raises(VerifyError):
        engine_b.pull(apply=True)
    # restore, then tampered blob
    backend._head = (original_data, token)
    key = list(backend._blobs)[0]
    blob = bytearray(backend._blobs[key])
    blob[-1] ^= 0xFF
    backend._blobs[key] = bytes(blob)
    with pytest.raises(VerifyError):
        engine_b.pull(apply=True)


def test_version_mismatch_blocks_pull(tmp_path: Path) -> None:
    _, _, _, engine_a, engine_b = fleet(tmp_path)
    engine_a.init(create_remote=True)
    engine_a.push()
    engine_b.init(create_remote=False)
    # a device running an OLDER Hermes than the pusher must refuse the pull
    old = Engine(
        engine_b.home,
        make_settings("dev-b"),
        engine_b.backend,
        "0.19.0",
        pipeline=PIPELINE,
    )
    with pytest.raises(VersionMismatch):
        old.pull(apply=True)
    # 'unknown' never mismatches
    unknown = Engine(
        engine_b.home,
        make_settings("dev-b"),
        engine_b.backend,
        "unknown",
        pipeline=PIPELINE,
    )
    result = unknown.pull(apply=False)
    assert result.generation == 1


def test_engine_rollback_mid_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, home_b, engine_a, engine_b = fleet(tmp_path)
    engine_a.init(create_remote=True)
    engine_a.push()
    engine_b.init(create_remote=False)
    tree_hash_before = tree_hash(home_b, skip_top="roam")
    real_install = restore._install_from_staged

    def flaky(target: Path, staged: Path, mode: int) -> None:
        if "staging" in str(staged):
            raise OSError("simulated disk failure mid-install")
        real_install(target, staged, mode)

    monkeypatch.setattr(restore, "_install_from_staged", flaky)
    with pytest.raises(RestoreError) as excinfo:
        engine_b.pull(apply=True)
    assert "rollback restored" in str(excinfo.value)
    # every original user byte is back (roam/ plugin state excluded from the comparison)
    assert tree_hash(home_b, skip_top="roam") == tree_hash_before
    journals = list((home_b / "roam/journals").glob("*.json"))
    assert len(journals) == 1
    assert json.loads(journals[0].read_bytes())["phase"] == "rolled_back"
    assert engine_b.load_state().last_pulled_generation == 0


def test_doctor(tmp_path: Path) -> None:
    _, _, _, engine_a, _ = fleet(tmp_path)
    engine_a.init(create_remote=True)
    checks = engine_a.doctor()
    by_name = {c.name: c for c in checks}
    assert set(by_name) == {
        "crash recovery",
        "archive key",
        "backend reachability",
        "remote head signature",
        "rollback floor",
        "hermes version",
        "bundle size",
        "staging dir writable",
        "backups dir writable",
    }
    assert all(c.ok for c in checks)
    # bad key -> the archive-key check fails and push refuses to run
    broken = Engine(
        engine_a.home,
        RoamSettings(backend="fake", device_name="dev-a", archive_key=b"short"),
        engine_a.backend,
        VERSION,
        pipeline=PIPELINE,
    )
    assert broken.doctor()[0].name == "crash recovery"  # recovery runs first, stays green
    by_name_broken = {c.name: c for c in broken.doctor()}
    assert by_name_broken["archive key"].ok is False
    with pytest.raises(VerifyError):
        broken.push()
    # unreachable backend -> status degrades gracefully, doctor flags both checks
    dead = FakeBackend(fail_ping=True)
    dying = make_engine(tmp_path / "home-x", "dev-x", dead)
    write(tmp_path / "home-x/SOUL.md", b"x")

    def broken_get_head(*, deadline: float | None = None):  # noqa: ARG001
        raise BackendError("the remote exploded")

    dead.get_head = broken_get_head
    status = dying.status()
    assert status.remote_generation is None
    assert status.remote_ahead is False  # never "ahead" when the remote could not be read
    assert status.error
    dead_checks = {c.name: c for c in dying.doctor()}
    assert dead_checks["backend reachability"].ok is False
    assert dead_checks["remote head signature"].ok is False


def test_diff_groups_local_changes_against_remote(tmp_path: Path) -> None:
    _, home_a, home_b, engine_a, engine_b = fleet(tmp_path)
    engine_a.init(create_remote=True)
    engine_a.push()
    engine_b.init(create_remote=False)
    engine_b.pull(apply=True)
    (home_a / "skills/roam/SKILL.md").unlink()
    write(home_a / "skills/new/SKILL.md", b"newest skill")
    engine_a.push()
    diff = engine_b.diff()
    assert diff.remote_generation == 2
    assert "skills/new/SKILL.md" in diff.added.get(Scope.SHARED, [])
    assert "skills/roam/SKILL.md" in diff.removed.get(Scope.SHARED, [])
    # 0.4.2: the first-pull union kept this device's own shared model
    # values, so config.yaml stays visibly changed until the device pushes
    assert set(diff.changed.get(Scope.SHARED, [])) == {"config.yaml"}


def test_status_flags_local_dirty(tmp_path: Path) -> None:
    _, _, _, engine_a, _ = fleet(tmp_path)
    engine_a.init(create_remote=True)
    engine_a.push()
    clean = engine_a.status()
    assert clean.local_dirty is False
    write(engine_a.home / "SOUL.md", b"edited soul")
    dirty = engine_a.status()
    assert dirty.local_dirty is True
    assert dirty.dirty_count == 1
    diff = engine_a.diff()
    assert diff.changed.get(Scope.SHARED, []) == ["SOUL.md"]


def test_push_lists_its_files_without_uploading_on_dry_run(tmp_path: Path) -> None:
    """push --dry-run must enumerate exactly what a real push would bundle,
    without storing anything on the remote."""
    backend = FakeBackend()
    home_a = build_home_a(tmp_path)
    engine_a = make_engine(home_a, "dev-a", backend)
    engine_a.init(create_remote=True)
    engine_a.push()  # establish the fleet

    write(home_a / "memories/new-delta.md", b"delta")  # memories/ is shared by default

    # dry run: nothing is uploaded, but the file list is complete
    before = backend.get_head()
    dry = engine_a.push(dry_run=True)
    assert dry.dry_run is True
    assert "memories/new-delta.md" in dry.files
    assert "config.yaml" in dry.files  # configs travel stripped, but they travel
    assert len(dry.files) == dry.file_count
    assert backend.get_head() == before  # head untouched: nothing was uploaded

    # the real push returns the same list and really stores the bundle
    pushed = engine_a.push()
    assert pushed.files == dry.files
    assert pushed.dry_run is False


def test_push_with_nothing_to_push_lists_no_files(tmp_path: Path) -> None:
    backend = FakeBackend()
    home_a = build_home_a(tmp_path)
    engine_a = make_engine(home_a, "dev-a", backend)
    engine_a.init(create_remote=True)
    engine_a.push()
    again = engine_a.push()
    assert again.skipped_reason == "nothing to push"
    assert again.files == []


def test_archive_limits_follow_settings(tmp_path: Path) -> None:
    import dataclasses

    from roam.archive import ArchiveLimits

    _, _, _, engine_a, _ = fleet(tmp_path)
    engine_a.settings = dataclasses.replace(engine_a.settings, max_bundle_bytes=12345)
    limits = engine_a._archive_limits()
    assert isinstance(limits, ArchiveLimits)
    assert limits.max_total_bytes == 12345
    assert limits.max_member_bytes == 12345
    assert limits.max_members == 100_000


def test_fresh_device_lists_no_devices(tmp_path: Path) -> None:
    backend = FakeBackend()
    home = tmp_path / "home-x"
    write(home / "SOUL.md", b"x")
    engine = make_engine(home, "dev-x", backend)
    engine.init(create_remote=False)
    status = engine.status()
    assert status.devices == []  # never synced: this device is not listed


def test_restricted_pull_skips_config_and_keeps_remote_ahead(tmp_path: Path) -> None:
    backend, home_a, home_b, engine_a, engine_b = fleet(tmp_path)
    engine_a.init(create_remote=True)
    engine_a.push()
    engine_b.init(create_remote=False)
    before = (home_b / "config.yaml").read_text()
    prefixes = ["skills/", "memories/", "SOUL.md"]
    result = engine_b.pull(apply=True, include_prefixes=prefixes)
    assert result.applied
    assert result.config_merged is False
    assert (home_b / "SOUL.md").read_bytes() == SOUL_A
    assert (home_b / "skills/roam/SKILL.md").read_bytes() == SKILL_A
    assert (home_b / "memories/mem.md").read_bytes() == b"A memory"
    assert (home_b / "config.yaml").read_text() == before
    assert any("restricted pull" in note for note in result.notes)
    # a partial pull never advances the seen generation, so the remote stays ahead
    state = engine_b.load_state()
    assert state.last_seen_generation == 0
    assert state.last_pulled_generation == 0
    status = engine_b.status()
    assert status.remote_ahead is True
    diff = engine_b.diff()
    assert "config.yaml" in diff.changed.get(Scope.SHARED, [])
    assert "SOUL.md" not in diff.changed.get(Scope.SHARED, [])
    # pushing over the unpulled remote changes is still refused
    with pytest.raises(RemoteAhead):
        engine_b.push()
    # a full pull afterwards completes the sync; the config was never
    # ACCEPTED here (the restricted pull above did not touch it), so the
    # merge is the 0.4.2 first-pull union: this device's own shared values
    # win and stay visibly dirty until they are published
    full = engine_b.pull(apply=True)
    assert full.config_merged is True
    merged = yaml.safe_load((home_b / "config.yaml").read_text())
    assert merged["model"] == {"provider": "p-b", "temperature": 0.2}
    assert merged["terminal"] == {"theme": "light-b", "font": "sans-b"}
    settled = engine_b.status()
    assert settled.remote_ahead is False
    assert settled.local_dirty is True  # the kept values are pending publish


def test_restricted_pull_prefix_matching(tmp_path: Path) -> None:
    from roam.engine import _prefix_allowed

    prefixes = ["skills/", "memories/", "SOUL.md", "profiles/*/skills/"]
    assert _prefix_allowed("skills/a/SKILL.md", prefixes)
    assert _prefix_allowed("SOUL.md", prefixes)
    assert _prefix_allowed("profiles/work/skills/a/SKILL.md", prefixes)
    assert not _prefix_allowed("SOUL.md.bak", prefixes)
    assert not _prefix_allowed("config.yaml", prefixes)
    assert not _prefix_allowed("profiles/work/config.yaml", prefixes)
    assert not _prefix_allowed("profiles/a/b/skills/c", prefixes)
    assert not _prefix_allowed("skills-private/x", prefixes)
    assert _prefix_allowed("anything", None)


def test_crash_recovery_runs_at_every_entry_point(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A SIGKILL-equivalent mid pull --apply leaves a PHASE_INSTALLING journal;
    the next entry point must roll the home back and say so via `recovered`
    (review r2 blocker f1)."""
    backend, _, home_b, engine_a, engine_b = fleet(tmp_path)
    engine_a.init(create_remote=True)
    engine_a.push()
    engine_b.init(create_remote=False)
    before = tree_hash(home_b, skip_top="roam")
    real_install = restore._install_from_staged
    installs: list[Path] = []

    def kill_after_first(target: Path, staged: Path, mode: int) -> None:
        installs.append(target)
        if len(installs) == 2:
            raise SystemExit(137)  # BaseException: restore's rollback does not run
        real_install(target, staged, mode)

    monkeypatch.setattr(restore, "_install_from_staged", kill_after_first)
    with pytest.raises(SystemExit):
        engine_b.pull(apply=True)
    assert installs
    journals = sorted((home_b / "roam" / "journals").glob("*.json"))
    assert len(journals) == 1
    assert json.loads(journals[0].read_bytes())["phase"] == "installing"

    # the next start of every public surface consumes the journal and reports it
    status = engine_b.status()
    assert status.recovered and "rolled back" in status.recovered
    record = json.loads(journals[0].read_bytes())
    assert record["phase"] == "rolled_back"
    assert tree_hash(home_b, skip_top="roam") == before
    # once consumed, nothing reports recovery again and doctor stays green
    assert engine_b.status().recovered is None
    checks = {c.name: c for c in engine_b.doctor()}
    assert checks["crash recovery"].ok is True


def test_save_state_survives_and_sweeps_stale_tmps(tmp_path: Path) -> None:
    """A crash mid-save (or an older fixed-name tmp) must never wedge state saving."""
    backend, _, _, engine_a, _ = fleet(tmp_path)
    engine_a.init(create_remote=True)
    roam_dir = engine_a.home / "roam"
    stale_fixed = roam_dir / ".state.json.tmp"
    stale_fixed.write_bytes(b"stale tmp from a crashed save")
    stale_unique = roam_dir / ".state.json.deadbeef.tmp"
    stale_unique.write_bytes(b"stale tmp from another crash")
    state = engine_a.load_state()
    engine_a.save_state(state)  # must not raise and must sweep the stale tmps
    assert (roam_dir / "state.json").is_file()
    assert not stale_fixed.exists()
    assert not stale_unique.exists()
    assert not list(roam_dir.glob(".state.json*.tmp"))


def test_second_roam_process_fails_fast_on_the_lock(tmp_path: Path) -> None:
    """While one command holds the per-home lock, a second must get an
    immediate named refusal — never interleave installs."""
    backend, home_a, _, engine_a, engine_b = fleet(tmp_path)
    engine_a.init(create_remote=True)
    engine_a.push()
    engine_b.init(create_remote=False)
    with engine_b._process_lock():
        with pytest.raises(RoamError, match="another hermes roam command is running"):
            engine_b.pull(apply=True)
        with pytest.raises(RoamError, match="another hermes roam command is running"):
            engine_b.push()
    # released: the same command works again
    result = engine_b.pull(apply=True)
    assert result.applied


def test_local_rules_win_over_remote_scopes(tmp_path: Path) -> None:
    """This device's rules.yaml decides what a pull may write or delete; the
    scope claimed by the remote manifest never wins (review r2 f6 mechanism)."""
    backend = FakeBackend()
    shared_rules = b"shared:\n  - work-shared/**\n"
    home_a = tmp_path / "home-a"
    write(home_a / "roam/rules.yaml", shared_rules)
    write(home_a / "SOUL.md", b"a soul")
    write(home_a / "work-shared/x.md", b"shared via A's rules")
    settings_a = RoamSettings(
        backend="fake", device_name="dev-a", archive_key=KEY
    )
    engine_a = Engine(home_a, settings_a, backend, VERSION)  # real pipeline
    engine_a.init(create_remote=True)
    engine_a.push()  # generation 1 carries work-shared/x.md

    home_b = tmp_path / "home-b"
    write(home_b / "roam/rules.yaml", shared_rules)
    settings_b = RoamSettings(backend="fake", device_name="dev-b", archive_key=KEY)
    engine_b = Engine(home_b, settings_b, backend, VERSION)
    engine_b.init(create_remote=False)
    pulled = engine_b.pull(apply=True)
    assert (home_b / "work-shared/x.md").read_bytes() == b"shared via A's rules"
    assert "work-shared/x.md" not in pulled.skipped_local

    # B's user changes their mind: the local rules now keep work-shared/** local
    write(home_b / "roam/rules.yaml", b"local:\n  - work-shared/**\n")
    (home_a / "work-shared/x.md").unlink()  # A deletes it remotely
    write(home_a / "work-shared/new.md", b"added remotely")
    engine_a.push()  # generation 2

    result = engine_b.pull(apply=True)
    # neither the deletion nor the new file is applied; both are surfaced
    assert (home_b / "work-shared/x.md").read_bytes() == b"shared via A's rules"
    assert not (home_b / "work-shared/new.md").exists()
    # both refusals are surfaced (roam/installed-plugins.json is LOCAL by the
    # roam/ contract and always listed; it is handled via plugins_to_install)
    assert {"work-shared/x.md", "work-shared/new.md"} <= set(result.skipped_local)
    assert result.plan.removed.get(Scope.SHARED, []) == []
    assert any("refused by this device's local rules" in note for note in result.notes)


PROFILE_CONFIG_A = """model:
  provider: p-a
terminal:
  theme: dark-a
  font: mono-a
browser:
  executable: /root/bin/browser-a
"""

PROFILE_CONFIG_B = """model:
  provider: p-b
browser:
  executable: /usr/local/bin/browser-b
"""


def test_every_config_is_stripped_on_push_and_merged_on_pull(tmp_path: Path) -> None:
    """profiles/<name>/config.yaml must go through the same strip/merge pipeline
    as the top-level config.yaml (review r2 f5 engine side)."""
    backend = FakeBackend()
    home_a = build_home_a(tmp_path)
    write(home_a / "profiles/p/config.yaml", PROFILE_CONFIG_A.encode())
    engine_a = make_engine(home_a, "dev-a", backend)
    engine_a.init(create_remote=True)
    engine_a.push()

    home_b = build_home_b(tmp_path)
    write(home_b / "profiles/q/config.yaml", PROFILE_CONFIG_B.encode())
    engine_b = make_engine(home_b, "dev-b", backend)
    engine_b.init(create_remote=False)
    result = engine_b.pull(apply=True)
    assert result.applied

    # profile p: B had no local config — the merged file is the stripped remote
    pulled_p = home_b / "profiles/p/config.yaml"
    assert pulled_p.is_file()
    text_p = pulled_p.read_text()
    assert "provider: p-a" in text_p  # the shared part of the config did travel
    assert "/root/bin/browser-a" not in text_p  # host key never travelled
    assert "dark-a" not in text_p  # the terminal subtree never travelled
    assert (pulled_p.stat().st_mode & 0o777) == 0o600  # a fresh config is private

    # profile q: never shared by A — B's own config is untouched by the pull
    pulled_q = home_b / "profiles/q/config.yaml"
    text_q = pulled_q.read_text()
    assert text_q == PROFILE_CONFIG_B


def test_pull_refuses_older_validly_signed_head(tmp_path: Path) -> None:
    """A rewound / eventually-consistent remote serving an OLDER signed head
    must never roll the home or the local state back (review r1 high)."""
    backend, home_a, _, engine_a, _ = fleet(tmp_path)
    engine_a.init(create_remote=True)
    engine_a.push()  # generation 1
    older = backend.get_head()
    write(home_a / "SOUL.md", b"A changed the soul")
    engine_a.push()  # generation 2
    assert engine_a.load_state().last_seen_generation == 2
    before = tree_hash(home_a, skip_top="roam")

    backend._head = older  # the storage serves the generation-1 head again
    with pytest.raises(VerifyError, match="rollback"):
        engine_a.pull(apply=True)
    assert tree_hash(home_a, skip_top="roam") == before
    state = engine_a.load_state()
    assert state.last_seen_generation == 2
    assert state.last_pulled_generation == 0
    # status reports the rewound remote as stale, never as ahead
    status = engine_a.status()
    assert status.remote_ahead is False
    assert "rolled back" in status.error
    with pytest.raises(VerifyError, match="rollback"):
        engine_a.diff()
    with pytest.raises(VerifyError, match="rollback"):
        engine_a.push()
    # allow_rollback is the explicit, single way to accept the rewound head
    result = engine_a.pull(apply=True, allow_rollback=True)
    assert result.generation == 1


def test_pull_refuses_head_below_last_pulled_generation(tmp_path: Path) -> None:
    """The floor is max(last seen, last pulled): a pull-only device that already
    applied a newer generation must not be rolled back by a rewound store."""
    backend, _, home_b, engine_a, engine_b = fleet(tmp_path)
    engine_a.init(create_remote=True)
    engine_a.push()  # generation 1
    engine_b.init(create_remote=False)
    engine_b.pull(apply=True)  # B applied generation 1
    older = backend.get_head()  # the generation-1 head
    write(engine_a.home / "SOUL.md", b"A changed the soul")
    engine_a.push()  # generation 2
    engine_b.pull(apply=True)  # B applied generation 2
    assert engine_b.load_state().last_pulled_generation == 2
    before = tree_hash(home_b, skip_top="roam")

    backend._head = older  # the store serves the generation-1 head again
    with pytest.raises(VerifyError, match="rollback"):
        engine_b.pull(apply=True)
    assert tree_hash(home_b, skip_top="roam") == before
    state = engine_b.load_state()
    assert state.last_pulled_generation == 2
    assert state.last_seen_generation == 2


def test_status_clean_when_base_carries_rules_hidden_paths(tmp_path: Path) -> None:
    """Round-4 h2 git-convergence regression: a base entry for a path this
    device's own rules keep LOCAL is carried-forward fleet state, not local
    dirt. The device can neither publish nor delete it and push answers
    "nothing to push" for the same state, so status must report clean —
    while the signed head still pins the path so only a device that can see
    it can ever delete it."""
    backend = FakeBackend()
    shared_rules = b"shared:\n  - work-shared/**\n"
    # like the campaign's beta rules: the shared extension does not overlap
    # the narrowed path, so the local rule wins for it (first-match-wins)
    narrowed_rules = (
        b"local:\n  - work-shared/secret.md\nshared:\n  - work-shared/open/**\n"
    )

    home_a = tmp_path / "home-a"
    write(home_a / "roam/rules.yaml", shared_rules)
    write(home_a / "work-shared/open/x.md", b"shared x")
    write(home_a / "work-shared/secret.md", b"fleet secret")
    engine_a = Engine(
        home_a,
        RoamSettings(backend="fake", device_name="dev-a", archive_key=KEY),
        backend,
        VERSION,
    )
    engine_a.init(create_remote=True)
    assert engine_a.push().generation == 1

    home_b = tmp_path / "home-b"
    write(home_b / "roam/rules.yaml", narrowed_rules)
    engine_b = Engine(
        home_b,
        RoamSettings(backend="fake", device_name="dev-b", archive_key=KEY),
        backend,
        VERSION,
    )
    engine_b.init(create_remote=False)
    pulled = engine_b.pull(apply=True)
    # B got the visible file and refused the narrowed one; the refused path
    # is pinned in B's last-sync snapshot as carry-forward fleet state
    assert (home_b / "work-shared/open/x.md").read_bytes() == b"shared x"
    assert not (home_b / "work-shared/secret.md").exists()
    assert "work-shared/secret.md" in pulled.skipped_local
    assert "work-shared/secret.md" in engine_b.load_state().base_files

    # B keeps its own LOCAL version of the narrowed path (the campaign's
    # beta-side skills/skill-b edit): status must stay clean
    write(home_b / "work-shared/secret.md", b"b's own local secret")
    settled = engine_b.status()
    assert settled.remote_ahead is False
    assert settled.local_dirty is False
    assert settled.dirty_count == 0
    again = engine_b.push()
    assert again.skipped_reason == "nothing to push"

    # the signed head still pins the fleet version; A's copy is untouched
    pair = backend.get_head()
    signed = head_mod.verify_head(pair[0], KEY)
    assert "work-shared/secret.md" in signed.files
    assert (home_a / "work-shared/secret.md").read_bytes() == b"fleet secret"

    # real dirt on a path B can see is still flagged
    write(home_b / "work-shared/open/x.md", b"b edited x")
    dirty = engine_b.status()
    assert dirty.local_dirty is True
    assert dirty.dirty_count == 1


def test_status_clean_after_opting_out_of_a_synced_class(tmp_path: Path, monkeypatch) -> None:
    """Round-4 h2 regression, publisher side, 0.4.1 form: after a device
    opts out of the class it previously published (the campaign's alpha
    turning sync_sessions back off), the push unpublishes this device's
    OWN archive namespace only, the rest of the fleet state stays clean,
    and the settled state afterwards answers "nothing to push". The raw
    sessions/ folder is always LOCAL and never part of any push."""
    from roam import session_archive as archive_mod

    backend = FakeBackend()
    home_a = tmp_path / "home-a"
    write(home_a / "sessions/request_dump_1.json", b"{}")  # always LOCAL now
    engine_a = Engine(
        home_a,
        RoamSettings(
            backend="fake", device_name="dev-a", archive_key=KEY, sync_sessions=True
        ),
        backend,
        VERSION,
    )
    # tests never spawn the real Hermes CLI: the export is injected through
    # the module attribute the lazy pipeline loader resolves at call time
    monkeypatch.setattr(archive_mod, "find_hermes", lambda: "hermes")
    monkeypatch.setattr(
        archive_mod,
        "collect_export",
        lambda home, scratch, exe, timeout: (
            [
                {
                    "name": "sess-abc123.md",
                    "data": b"# transcript\nsession_id: sess-abc123\n",
                    "header": {"session_id": "sess-abc123", "updated_at": "2026-11-07"},
                }
            ],
            "",
        ),
    )

    def signed() -> dict:
        pair = backend.get_head()
        assert pair is not None
        return head_mod.verify_head(pair[0], KEY).files

    engine_a.init(create_remote=True)
    assert engine_a.push().generation == 1
    assert "archive/sessions/dev-a/default/sess-abc123.md" in signed()

    # the raw request dump never traveled
    assert "sessions/request_dump_1.json" not in signed()

    # the same home opts out of publishing the archive on its next run
    engine_off = Engine(
        home_a,
        RoamSettings(backend="fake", device_name="dev-a", archive_key=KEY),
        backend,
        VERSION,
    )
    settled = engine_off.status()
    assert settled.remote_ahead is False
    assert settled.local_dirty is False
    assert settled.dirty_count == 0
    again = engine_off.push()
    # the opt-out PUBLISHES the unpublishing: this device's own archive
    # namespace leaves the manifest, and the push goes through
    assert again.generation == 2
    assert "archive/sessions/dev-a/default/sess-abc123.md" not in signed()
    # with the fleet state now settled, the next push has nothing to do
    settled_again = engine_off.status()
    assert settled_again.local_dirty is False
    assert settled_again.dirty_count == 0
    assert engine_off.push().skipped_reason == "nothing to push"
