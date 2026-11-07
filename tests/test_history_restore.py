"""0.4 history/restore: signed head chaining, verifiable store chain, recovery."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fakes.fake_backend import FakeBackend
from fakes.fake_pipeline import PIPELINE
from fakes.fake_pipeline import build_bundle as fake_build_bundle

from roam import head as head_mod
from roam.engine import Engine
from roam.errors import RoamError, VerifyError
from roam.model import RoamSettings

KEY = hashlib.sha256(b"history-test-archive-key").digest()
OTHER_KEY = hashlib.sha256(b"another-fleet-key-entirely").digest()
VERSION = "0.22.5"

CONFIG_A = """model:
  provider: p-a
  temperature: 0.7
terminal:
  theme: dark-a
  font: mono-a
browser:
  executable: /root/bin/browser-a
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


class ListableBackend(FakeBackend):
    """FakeBackend plus the 0.4 listing surface."""

    def list_blobs(self, prefix: str, *, deadline: float | None = None) -> list[str]:
        self._note(f"list_blobs:{prefix}", deadline)
        return sorted(key for key in self._blobs if key.startswith(prefix))


def make_engine(home: Path, device: str, backend: FakeBackend, key: bytes = KEY) -> Engine:
    settings = RoamSettings(backend="fake", device_name=device, archive_key=key)
    return Engine(home, settings, backend, VERSION, pipeline=PIPELINE)


def write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, mode)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fleet(tmp_path: Path) -> tuple:
    backend = ListableBackend()
    home = tmp_path / "home-a"
    write(home / "config.yaml", CONFIG_A.encode())
    write(home / "SOUL.md", b"soul v1")
    write(home / "skills/roam/SKILL.md", b"# skill v1")
    engine = make_engine(home, "dev-a", backend)
    engine.init(create_remote=True)
    engine.push()
    return backend, home, engine


def test_push_signs_the_parent_bundle_key(tmp_path: Path) -> None:
    backend, home, engine = fleet(tmp_path)
    data, _ = backend.get_head()
    head1 = head_mod.verify_head(data, KEY)
    assert head1.parent_bundle_key == ""  # generation 0 has no bundle
    write(home / "SOUL.md", b"soul v2")
    engine.push()
    data, _ = backend.get_head()
    head2 = head_mod.verify_head(data, KEY)
    assert head2.parent_bundle_key == head1.bundle_key
    assert head2.parent_generation == head1.generation


def test_history_lists_every_version_and_the_deletion(tmp_path: Path) -> None:
    backend, home, engine = fleet(tmp_path)
    write(home / "SOUL.md", b"soul v2")
    engine.push()
    (home / "SOUL.md").unlink()
    engine.push()
    entries = engine.history("SOUL.md")
    assert [(e["generation"], e["deleted"]) for e in entries] == [(3, True), (2, False), (1, False)]
    assert entries[0]["sha256"] == sha(b"soul v2")
    assert entries[1]["sha256"] == sha(b"soul v2")
    assert entries[2]["sha256"] == sha(b"soul v1")
    assert all(e["device"] == "dev-a" for e in entries)
    assert all(e["size"] == len(b"soul v2") or e["size"] == len(b"soul v1") for e in entries)


def test_history_limit_caps_the_entry_list(tmp_path: Path) -> None:
    backend, home, engine = fleet(tmp_path)
    write(home / "SOUL.md", b"soul v2")
    engine.push()
    entries = engine.history("SOUL.md", limit=1)
    assert len(entries) == 1
    assert entries[0]["generation"] == 2


def test_history_without_a_path_lists_only_real_bundle_members(tmp_path: Path) -> None:
    backend, home, engine = fleet(tmp_path)
    write(home / "SOUL.md", b"soul v2")
    engine.push()
    entries = engine.history()
    paths = [e["path"] for e in entries]
    assert "SOUL.md" in paths
    assert not any(p == "config.yaml" or p.startswith("roam/") for p in paths)
    assert all(e["device"] == "dev-a" for e in entries)


def test_history_never_trusts_foreign_or_garbage_bundles(tmp_path: Path) -> None:
    backend, home, engine = fleet(tmp_path)
    write(home / "SOUL.md", b"soul v2")
    engine.push()
    # a bundle sealed with another fleet's key, under a self-consistent key
    forged = fake_build_bundle(
        home,
        [],
        tmp_path / "forged.roam",
        key=OTHER_KEY,
        header={"v": 1, "device": "dev-a", "generation": 3, "created_at": 1.0},
        extra_files={},
    )
    backend._blobs[
        head_mod.bundle_key_for(3, "dev-a", forged.plain_sha256)
    ] = (tmp_path / "forged.roam").read_bytes()
    # plain garbage under a well-shaped key
    backend._blobs["bundles/000009-dev-a-0123456789ab.roam"] = b"garbage"
    entries = engine.history("SOUL.md")
    assert [(e["generation"], e["deleted"]) for e in entries] == [(2, False), (1, False)]
    # the forged generation 3 never produced a deletion entry


def test_history_refuses_an_unverifiable_head(tmp_path: Path) -> None:
    backend, home, _ = fleet(tmp_path)
    stranger = make_engine(home, "dev-a", backend, key=OTHER_KEY)
    with pytest.raises(VerifyError):
        stranger.history()


def test_history_on_an_empty_remote_is_empty(tmp_path: Path) -> None:
    backend = ListableBackend()
    home = tmp_path / "home-a"
    engine = make_engine(home, "dev-a", backend)
    engine.init(create_remote=True)  # generation 0: no bundle yet
    assert engine.history() == []


def test_restore_recovers_a_deleted_version_and_the_next_push_publishes_it(
    tmp_path: Path,
) -> None:
    backend, home, engine = fleet(tmp_path)
    write(home / "SOUL.md", b"soul v2")
    engine.push()
    (home / "SOUL.md").unlink()
    engine.push()
    result = engine.restore("SOUL.md", generation=2, apply=True)
    assert result["applied"] is True
    assert result["sha256"] == sha(b"soul v2")
    assert (home / "SOUL.md").read_bytes() == b"soul v2"
    assert result["files_written"] == 1
    assert result["backup_dir"]
    # the restored version is a LOCAL change: state was not moved, so the
    # device is dirty and the next push publishes it again
    status = engine.status()
    assert status.local_dirty is True
    push = engine.push()
    assert push.generation == 4
    data, _ = backend.get_head()
    head4 = head_mod.verify_head(data, KEY)
    assert head4.files["SOUL.md"]["sha256"] == sha(b"soul v2")
    assert head4.parent_bundle_key  # the chain pointer keeps chaining


def test_restore_dry_run_touches_nothing(tmp_path: Path) -> None:
    backend, home, engine = fleet(tmp_path)
    write(home / "SOUL.md", b"soul v2")
    engine.push()
    (home / "SOUL.md").unlink()
    engine.push()
    result = engine.restore("SOUL.md", generation=1, apply=False)
    assert result["applied"] is False
    assert result["sha256"] == sha(b"soul v1")
    assert not (home / "SOUL.md").exists()
    assert result["backup_dir"] == ""
    assert engine.push().skipped_reason  # still nothing local to push


def test_restore_refuses_local_ignored_and_unopted_paths(tmp_path: Path) -> None:
    backend, home, engine = fleet(tmp_path)
    write(home / ".env", b"A_SECRET=2")
    write(home / "cache/junk.txt", b"cache")
    write(home / "sessions/2026/session.jsonl", b"{}")
    engine.push()
    for path in (".env", "cache/junk.txt", "sessions/2026/session.jsonl"):
        with pytest.raises(RoamError, match="restore refuses"):
            engine.restore(path, generation=2, apply=True)
    assert (home / ".env").read_bytes() == b"A_SECRET=2"


def test_restore_refuses_unknown_generations_and_paths(tmp_path: Path) -> None:
    backend, home, engine = fleet(tmp_path)
    with pytest.raises(RoamError, match="never published"):
        engine.restore("SOUL.md", generation=99, apply=True)
    with pytest.raises(RoamError, match="no version"):
        engine.restore("skills/other/MISSING.md", generation=1, apply=True)
    with pytest.raises(RoamError, match="generation must be"):
        engine.restore("SOUL.md", generation=0, apply=True)
    # a generation whose bundle was pruned from the store is not recoverable
    write(home / "SOUL.md", b"soul v2")
    engine.push()
    for key in list(backend._blobs):
        if key.startswith("bundles/000002-"):
            del backend._blobs[key]
    with pytest.raises(RoamError, match="not recoverable"):
        engine.restore("SOUL.md", generation=2, apply=True)


def test_restore_config_merges_the_shared_projection_with_local_host_keys(
    tmp_path: Path,
) -> None:
    backend, home, engine = fleet(tmp_path)
    # the device later moves its shared AND host-scoped config values
    write(home / "config.yaml", CONFIG_B.encode())
    engine.push()
    result = engine.restore("config.yaml", generation=1, apply=True)
    assert result["applied"] is True
    assert result["config_merged"] is True
    merged = (home / "config.yaml").read_text()
    assert "provider: p-a" in merged  # shared value restored from generation 1
    assert "theme: light-b" in merged  # host-scoped key stayed local
    assert "executable: /usr/local/bin/browser-b" in merged  # host-scoped too


def test_restore_takes_a_backup_of_the_overwritten_file(tmp_path: Path) -> None:
    backend, home, engine = fleet(tmp_path)
    write(home / "SOUL.md", b"soul v2")
    engine.push()
    result = engine.restore("SOUL.md", generation=1, apply=True)
    assert result["applied"] is True
    backup = Path(result["backup_dir"])
    assert (backup / "SOUL.md").read_bytes() == b"soul v2"  # v2 was backed up first
    assert (home / "SOUL.md").read_bytes() == b"soul v1"


def test_the_restored_file_hash_matches_the_history_entry(tmp_path: Path) -> None:
    backend, home, engine = fleet(tmp_path)
    write(home / "SOUL.md", b"soul v2")
    engine.push()
    entries = [e for e in engine.history("SOUL.md") if not e["deleted"]]
    result = engine.restore("SOUL.md", generation=entries[0]["generation"], apply=True)
    assert result["sha256"] == entries[0]["sha256"]


def test_history_survives_a_manually_pruned_middle_generation(tmp_path: Path) -> None:
    backend, home, engine = fleet(tmp_path)
    write(home / "SOUL.md", b"soul v2")
    engine.push()
    (home / "SOUL.md").unlink()
    engine.push()
    # the store operator prunes generation 2 by hand: history still reports
    # what is verifiable, and the gap is not silently papered over
    for key in list(backend._blobs):
        if key.startswith("bundles/000002-"):
            del backend._blobs[key]
    entries = engine.history("SOUL.md")
    assert [(e["generation"], e["deleted"]) for e in entries] == [(1, False)]
