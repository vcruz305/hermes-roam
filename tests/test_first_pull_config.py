"""0.4.2 regression: the exact first-pull config repro, end to end.

Device A (vps) pushes generation 1 with a config.yaml that carries shared
model keys. A brand-new device B whose config.yaml exists only because the
user ran `hermes config set plugins.entries.hermes-roam.settings.*` (plus
its own model.api_key) runs `hermes roam init`, reviews the pull plan, and
then pulls. Under 0.4.1 every pull attempt — plan-only included — saved the
downloaded bundle as the three-way BASE for configs, so the apply saw
remote == base and kept the whole config local as a phantom "local-only
change"; --overwrite-conflicts could not override it because the
remote == base short-circuit ran first. These tests pin the 0.4.2 contract:

- the first pull merges as a union: remote-only shared keys are added,
  local values win where both sides set the same key and are reported,
  host-scoped and secret keys stay local;
- `pull --apply --overwrite-conflicts` applies to config.yaml too: the
  remote shared projection wins for shared keys while host-scoped, secret
  and local roam plugin settings stay local (a backup is kept);
- a brand-new device is told to take the remote home wholesale.
"""

import hashlib
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fakes.fake_backend import FakeBackend

from roam.engine import Engine
from roam.model import RoamSettings

KEY = hashlib.sha256(b"first-pull-repro-archive-key").digest()
VERSION = "0.22.5"

CONFIG_VPS = """\
model:
  provider: openrouter
  default: glm-5
  api_key: sk-vps-000000000001
terminal:
  theme: dark-vps
plugins:
  entries:
    hermes-roam:
      settings:
        backend: git
        device_name: vps
"""

# the repro's fresh device: only the roam plugin settings the user set with
# `hermes config set`, plus this device's own api_key
CONFIG_FRESH = """\
model:
  api_key: sk-fresh-0000000002
plugins:
  entries:
    hermes-roam:
      settings:
        backend: git
        git_remote: file:///srv/roam.git
        device_name: beta
"""

# a fresh device that additionally sets a shared model key itself
CONFIG_CONTESTED = """\
model:
  provider: own-provider
  api_key: sk-fresh-0000000003
plugins:
  entries:
    hermes-roam:
      settings:
        backend: git
        device_name: gamma
"""

SOUL_A = b"the fleet's real SOUL, written on device A"
SEEDED_SOUL = b"Hermes's freshly seeded default SOUL.md"
MEMORY_A = b"shared memory from device A"


def _write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def _engine(home: Path, device: str, backend: FakeBackend) -> Engine:
    # the real pipeline: real scope stripping/merging, real bundle crypto
    return Engine(
        home,
        RoamSettings(backend="fake", device_name=device, archive_key=KEY),
        backend,
        VERSION,
    )


def _fleet(tmp_path: Path, fresh_config: str) -> tuple[Path, Path, Engine, Engine]:
    backend = FakeBackend()
    home_a = tmp_path / "vps"
    home_b = tmp_path / "fresh"
    _write(home_a / "config.yaml", CONFIG_VPS.encode())
    _write(home_a / "SOUL.md", SOUL_A)
    _write(home_a / "memories/MEMORY.md", MEMORY_A)
    _write(home_b / "config.yaml", fresh_config.encode())
    _write(home_b / "SOUL.md", SEEDED_SOUL)
    engine_a = _engine(home_a, "vps", backend)
    engine_b = _engine(home_b, "fresh", backend)
    engine_a.init(create_remote=True)
    assert engine_a.push().generation == 1
    engine_b.init(create_remote=False)
    return home_a, home_b, engine_a, engine_b


def test_exact_repro_first_pull_merges_shared_keys_and_keeps_secrets(tmp_path: Path) -> None:
    home_a, home_b, engine_a, engine_b = _fleet(tmp_path, CONFIG_FRESH)
    # the repro's poisoning step: a plan-only pull (the notice tells the user
    # to review the plan first) must never become a config sync point
    engine_b.pull(apply=False)
    result = engine_b.pull(apply=True)
    assert result.applied

    # the shared model keys the fresh device lacks ARRIVE (0.4.1 kept the
    # whole file local as a phantom local-only change)
    merged = yaml.safe_load((home_b / "config.yaml").read_text())
    assert merged["model"]["provider"] == "openrouter"
    assert merged["model"]["default"] == "glm-5"
    # this device's own api_key and roam plugin settings stay local
    assert merged["model"]["api_key"] == "sk-fresh-0000000002"
    assert merged["plugins"]["entries"]["hermes-roam"]["settings"]["device_name"] == "beta"
    assert merged["plugins"]["entries"]["hermes-roam"]["settings"]["git_remote"] == (
        "file:///srv/roam.git"
    )
    # the shared memory arrived; the seeded SOUL.md is a listed conflict
    assert (home_b / "memories/MEMORY.md").read_bytes() == MEMORY_A
    assert "SOUL.md" in result.conflicts
    assert (home_b / "SOUL.md").read_bytes() == SEEDED_SOUL
    # the outcome is reported, and a brand-new device is told what to do
    assert any("never synced here" in note for note in result.notes), result.notes
    assert any("brand-new device" in note for note in result.notes), result.notes

    # --overwrite-conflicts applies to the shared files: the remote SOUL.md
    # wins (with a backup), and the config's shared projection stays in place
    over = engine_b.pull(apply=True, overwrite_conflicts=True)
    assert over.applied
    assert (home_b / "SOUL.md").read_bytes() == SOUL_A
    merged = yaml.safe_load((home_b / "config.yaml").read_text())
    assert merged["model"]["provider"] == "openrouter"
    assert merged["model"]["default"] == "glm-5"
    assert merged["model"]["api_key"] == "sk-fresh-0000000002"
    assert merged["plugins"]["entries"]["hermes-roam"]["settings"]["device_name"] == "beta"
    backup = Path(over.backup_dir)
    assert (backup / "SOUL.md").read_bytes() == SEEDED_SOUL

    # the fleet converged: neither side has anything left to push or pull
    assert engine_a.push().skipped_reason == "nothing to push"
    assert engine_b.push().skipped_reason == "nothing to push"
    assert engine_b.status().local_dirty is False


def test_exact_repro_contested_shared_key_and_overwrite_conflicts(tmp_path: Path) -> None:
    """A fresh device that sets a shared key itself keeps its value on the
    first pull (reported), and `--overwrite-conflicts` then takes the
    remote's shared projection while its secrets and roam settings stay."""
    home_a, home_b, _engine_a, engine_b = _fleet(tmp_path, CONFIG_CONTESTED)

    result = engine_b.pull(apply=True)
    assert result.applied
    merged = yaml.safe_load((home_b / "config.yaml").read_text())
    # the remote-only shared key arrived, the contested one stayed local
    assert merged["model"]["default"] == "glm-5"
    assert merged["model"]["provider"] == "own-provider"
    assert merged["model"]["api_key"] == "sk-fresh-0000000003"
    assert merged["plugins"]["entries"]["hermes-roam"]["settings"]["device_name"] == "gamma"
    # the kept value is reported by name with both resolutions
    kept_note = next((n for n in result.notes if "never synced here" in n), "")
    assert "model.provider" in kept_note, result.notes
    assert "push from this device" in kept_note
    assert "--overwrite-conflicts" in kept_note
    # the device stays honestly dirty until it publishes the kept value
    assert engine_b.status().local_dirty is True

    # --overwrite-conflicts applies to config.yaml too: the remote shared
    # projection wins for shared keys, secrets and roam settings stay local,
    # and a backup of the pre-merge config is kept
    pre_merge = (home_b / "config.yaml").read_text()
    over = engine_b.pull(apply=True, overwrite_conflicts=True)
    assert over.applied
    merged = yaml.safe_load((home_b / "config.yaml").read_text())
    assert merged["model"]["provider"] == "openrouter"
    assert merged["model"]["default"] == "glm-5"
    assert merged["model"]["api_key"] == "sk-fresh-0000000003"
    assert merged["plugins"]["entries"]["hermes-roam"]["settings"]["device_name"] == "gamma"
    backup = Path(over.backup_dir)
    assert yaml.safe_load((backup / "config.yaml").read_text())["model"] == (
        {"provider": "own-provider", "default": "glm-5",
         "api_key": "sk-fresh-0000000003"}
    )
    assert (backup / "config.yaml").read_text() == pre_merge
    # the fleet converged: nothing is left to push or pull
    assert engine_b.status().local_dirty is False
    assert engine_b.push().skipped_reason == "nothing to push"


def test_first_pull_union_and_overwrite_apply_to_profile_configs(tmp_path: Path) -> None:
    """0.5.0: the 0.4.2 config contract covers EVERY synced config — the
    root config.yaml and each profiles/NAME/config.yaml alike."""
    backend = FakeBackend()
    home_a = tmp_path / "vps"
    home_b = tmp_path / "fresh"
    config_a = """\
model:
  provider: openrouter
  default: glm-5
plugins:
  entries:
    hermes-roam:
      settings:
        backend: git
        device_name: vps
"""
    config_b = """\
model:
  api_key: «redacted:sk-work…»
plugins:
  entries:
    hermes-roam:
      settings:
        backend: git
        device_name: beta
"""
    _write(home_a / "config.yaml", CONFIG_VPS.encode())
    _write(home_a / "SOUL.md", SOUL_A)
    _write(home_a / "profiles/work/config.yaml", config_a.encode())
    _write(home_a / "profiles/work/SOUL.md", b"the work profile's soul")
    _write(home_b / "config.yaml", CONFIG_FRESH.encode())
    _write(home_b / "SOUL.md", SEEDED_SOUL)
    _write(home_b / "profiles/work/config.yaml", config_b.encode())
    _write(home_b / "profiles/work/SOUL.md", SEEDED_SOUL)
    engine_a = _engine(home_a, "vps", backend)
    engine_b = _engine(home_b, "fresh", backend)
    engine_a.init(create_remote=True)
    assert engine_a.push().generation == 1
    engine_b.init(create_remote=False)

    # a plan-only review is not a sync point for the profile config either
    engine_b.pull(apply=False)
    result = engine_b.pull(apply=True)
    assert result.applied
    merged = yaml.safe_load((home_b / "profiles/work/config.yaml").read_text())
    # the fleet's shared model keys arrived in the PROFILE config
    assert merged["model"]["provider"] == "openrouter"
    assert merged["model"]["default"] == "glm-5"
    # the profile's own api_key and roam settings stayed local
    assert merged["model"]["api_key"] == "«redacted:sk-work…»"
    assert merged["plugins"]["entries"]["hermes-roam"]["settings"]["device_name"] == "beta"
    # the profile's soul is a listed conflict (seeded local copy)
    assert "profiles/work/SOUL.md" in result.conflicts
    assert (home_b / "profiles/work/SOUL.md").read_bytes() == SEEDED_SOUL
    # the pre-merge profile config (the original local-only copy) was backed
    # up like every replaced file when the union rewrote it
    assert (Path(result.backup_dir) / "profiles/work/config.yaml").read_text() == config_b

    # --overwrite-conflicts takes the remote shared projection for the
    # profile config too, while its secrets and roam settings stay local
    over = engine_b.pull(apply=True, overwrite_conflicts=True)
    assert over.applied
    merged = yaml.safe_load((home_b / "profiles/work/config.yaml").read_text())
    assert merged["model"]["provider"] == "openrouter"
    assert merged["model"]["default"] == "glm-5"
    assert merged["model"]["api_key"] == "«redacted:sk-work…»"
    assert merged["plugins"]["entries"]["hermes-roam"]["settings"]["device_name"] == "beta"
    assert (Path(over.backup_dir) / "profiles/work/SOUL.md").read_bytes() == SEEDED_SOUL
    # the profile's real soul arrived with the overwrite
    assert (home_b / "profiles/work/SOUL.md").read_bytes() == b"the work profile's soul"
    # converged
    assert engine_b.status().local_dirty is False
    assert engine_b.push().skipped_reason == "nothing to push"


def test_plan_only_pull_poisons_nothing(tmp_path: Path) -> None:
    """The 0.4.1 root cause, pinned: any number of plan-only pulls leaves the
    config with NO accepted base, so the following apply still performs the
    first-pull union instead of keeping the whole file local."""
    home_a, home_b, _engine_a, engine_b = _fleet(tmp_path, CONFIG_FRESH)
    for _ in range(3):
        plan = engine_b.pull(apply=False)
        assert not plan.applied
        assert plan.config_merged is False
    state = engine_b.load_state()
    assert state.config_base_digests == {}
    engine_b.pull(apply=True)
    merged = yaml.safe_load((home_b / "config.yaml").read_text())
    assert merged["model"]["provider"] == "openrouter"
    assert merged["model"]["default"] == "glm-5"
