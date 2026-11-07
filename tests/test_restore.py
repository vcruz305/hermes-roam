"""Tests for roam.restore: plan filtering, journaled apply, rollback, crash recovery."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from roam import restore, scope
from roam.errors import RestoreError
from roam.model import Scope

HOST_KEYS = ["terminal", "browser.executable"]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree_hash(root: Path) -> list:
    items = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            items.append(
                (
                    path.relative_to(root).as_posix(),
                    sha(path.read_bytes()),
                    oct(path.stat().st_mode & 0o777),
                )
            )
    return items


def entry(data: bytes, mode: int, scope: Scope) -> dict:
    return {"sha256": sha(data), "size": len(data), "mode": mode, "scope": scope.value}


def make_home(tmp_path: Path, files: dict[str, bytes]) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        target = home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return home


def make_staging(tmp_path: Path, files: dict[str, bytes]) -> Path:
    staging = tmp_path / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return staging


def test_plan_refuses_local_ignored_and_scopeless(tmp_path: Path) -> None:
    good = b"shared bytes"
    manifest = {
        "skills/a/SKILL.md": entry(good, 0o644, Scope.SHARED),
        ".env": entry(b"k=v", 0o644, Scope.LOCAL),
        "cache/junk": entry(b"junk", 0o644, Scope.IGNORED),
        "sessions/s.jsonl": entry(b"{}", 0o644, Scope.SESSIONS),
        "no-scope.txt": {"sha256": sha(good), "size": len(good), "mode": 0o644},
    }
    home = make_home(tmp_path, {})
    staging = make_staging(
        tmp_path,
        {
            "skills/a/SKILL.md": good,
            ".env": b"k=v",
            "cache/junk": b"junk",
            "sessions/s.jsonl": b"{}",
            "no-scope.txt": good,
        },
    )
    plan = restore.plan_install(
        home, staging, manifest, classes={Scope.SHARED}, rules=object(), base_manifest={}
    )
    assert [f.relpath for f in plan.to_write] == ["skills/a/SKILL.md"]
    assert set(plan.skipped) == {".env", "cache/junk", "sessions/s.jsonl", "no-scope.txt"}


def test_plan_flags_config_merge_separately(tmp_path: Path) -> None:
    config = b"model: x\n"
    manifest = {"config.yaml": entry(config, 0o644, Scope.SHARED)}
    home = make_home(tmp_path, {})
    staging = make_staging(tmp_path, {"config.yaml": config})
    plan = restore.plan_install(
        home, staging, manifest, classes={Scope.SHARED}, rules=object(), base_manifest={},
        is_config=lambda relpath: relpath == "config.yaml" or relpath == "profiles/p/config.yaml",
    )
    assert plan.configs == (("config.yaml", staging / "config.yaml"),)
    assert plan.to_write == ()
    assert plan.to_delete == ()


def test_plan_deletes_only_previously_synced_files(tmp_path: Path) -> None:
    keep = b"keep me"
    manifest = {"skills/keep.md": entry(keep, 0o644, Scope.SHARED)}
    base = {
        "skills/gone.md": entry(b"old", 0o644, Scope.SHARED),
        "skills/never-had.md": entry(b"old", 0o644, Scope.SHARED),
        ".env": entry(b"old", 0o644, Scope.LOCAL),
    }
    home = make_home(tmp_path, {})
    staging = make_staging(tmp_path, {"skills/keep.md": keep})
    plan = restore.plan_install(
        home, staging, manifest, classes={Scope.SHARED}, rules=object(), base_manifest=base
    )
    assert plan.to_delete == ("skills/gone.md", "skills/never-had.md")


def test_plan_rejects_unsafe_relpaths(tmp_path: Path) -> None:
    manifest = {"../escape.txt": entry(b"x", 0o644, Scope.SHARED)}
    home = make_home(tmp_path, {})
    staging = make_staging(tmp_path, {"../escape.txt": b"not really outside"})
    with pytest.raises(RestoreError):
        restore.plan_install(
            home, staging, manifest, classes={Scope.SHARED}, rules=object(), base_manifest={}
        )


def test_plan_rejects_staged_mismatch(tmp_path: Path) -> None:
    manifest = {"skills/a/SKILL.md": entry(b"declared", 0o644, Scope.SHARED)}
    home = make_home(tmp_path, {})
    staging = make_staging(tmp_path, {"skills/a/SKILL.md": b"actually different"})
    with pytest.raises(RestoreError):
        restore.plan_install(
            home, staging, manifest, classes={Scope.SHARED}, rules=object(), base_manifest={}
        )


def test_apply_installs_and_journals(tmp_path: Path) -> None:
    original = b"old SOUL"
    replacement = b"new SOUL"
    new_file = b"brand new skill"
    home = make_home(tmp_path, {"SOUL.md": original})
    staging = make_staging(
        tmp_path, {"SOUL.md": replacement, "skills/new/SKILL.md": new_file}
    )
    manifest = {
        "SOUL.md": entry(replacement, 0o644, Scope.SHARED),
        "skills/new/SKILL.md": entry(new_file, 0o755, Scope.SHARED),
    }
    # SOUL.md was synced at the last sync point with exactly these bytes, so
    # overwriting it is a normal update, not a conflict
    base = {"SOUL.md": entry(original, 0o644, Scope.SHARED)}
    plan = restore.plan_install(
        home, staging, manifest, classes={Scope.SHARED}, rules=object(), base_manifest=base
    )
    assert plan.conflicts == ()
    result = restore.apply(
        home, plan, journal_dir=tmp_path / "journals", backup_dir=tmp_path / "backups",
        merge_config_fn=lambda *a: "", host_keys=HOST_KEYS,
    )
    assert result.files_written == 2
    assert (home / "SOUL.md").read_bytes() == replacement
    assert (home / "skills/new/SKILL.md").read_bytes() == new_file
    assert (home / "skills/new/SKILL.md").stat().st_mode & 0o777 == 0o755
    backup_dir = Path(result.backup_dir)
    assert (backup_dir / "SOUL.md").read_bytes() == original
    assert not (backup_dir / "skills/new/SKILL.md").exists()
    assert json.loads(Path(result.journal_path).read_bytes())["phase"] == "committed"


def test_apply_rolls_back_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = b"old SOUL"
    replacement = b"new SOUL"
    unmanaged = b"user file roam never managed"
    home = make_home(
        tmp_path, {"SOUL.md": original, "unmanaged.txt": unmanaged}
    )
    staged_files = {
        "SOUL.md": replacement,
        "skills/a/SKILL.md": b"new skill a",
        "skills/b/SKILL.md": b"new skill b",
    }
    staging = make_staging(tmp_path, staged_files)
    manifest = {
        rel: entry(data, 0o644, Scope.SHARED) for rel, data in staged_files.items()
    }
    base = {"SOUL.md": entry(original, 0o644, Scope.SHARED)}  # previously synced
    plan = restore.plan_install(
        home, staging, manifest, classes={Scope.SHARED}, rules=object(), base_manifest=base
    )
    assert plan.conflicts == ()
    real_install = restore._install_from_staged
    installs = []
    installed_before_failure = []

    def flaky(target: Path, staged: Path, mode: int) -> None:
        if "staging" in str(staged):
            installs.append(target)
            if len(installs) == 3:
                raise OSError("simulated disk failure")
        real_install(target, staged, mode)
        if "staging" in str(staged) and len(installs) == 2:
            installed_before_failure.append(target.read_bytes())

    monkeypatch.setattr(restore, "_install_from_staged", flaky)
    before = tree_hash(home)
    with pytest.raises(RestoreError) as excinfo:
        restore.apply(
            home, plan, journal_dir=tmp_path / "journals", backup_dir=tmp_path / "backups",
            merge_config_fn=lambda *a: "", host_keys=HOST_KEYS,
        )
    assert "rollback restored 1 file(s) and removed 1 new file(s)" in str(excinfo.value)
    # skills/a/SKILL.md really was installed before the failure, then undone by the rollback
    assert installed_before_failure == [b"new skill a"]
    assert not (home / "skills/a/SKILL.md").exists()
    assert tree_hash(home) == before
    journals = list((tmp_path / "journals").glob("*.json"))
    assert len(journals) == 1
    assert json.loads(journals[0].read_bytes())["phase"] == "rolled_back"
    assert (tmp_path / "backups/SOUL.md").read_bytes() == original


def test_plan_flags_locally_edited_file_as_conflict(tmp_path: Path) -> None:
    """Both sides edited the file since the last sync: the local edit must be
    kept and the conflict surfaced, not silently clobbered (review r2 f3)."""
    replacement = b"remote v2"
    manifest = {"SOUL.md": entry(replacement, 0o644, Scope.SHARED)}
    base = {"SOUL.md": entry(b"synced v1", 0o644, Scope.SHARED)}
    home = make_home(tmp_path, {"SOUL.md": b"local edit"})
    staging = make_staging(tmp_path, {"SOUL.md": replacement})
    plan = restore.plan_install(
        home, staging, manifest, classes={Scope.SHARED}, rules=object(), base_manifest=base
    )
    assert plan.conflicts == ("SOUL.md",)
    assert plan.to_write == ()
    assert (home / "SOUL.md").read_bytes() == b"local edit"
    # overwrite_conflicts is the explicit way to take the remote version
    plan_over = restore.plan_install(
        home, staging, manifest, classes={Scope.SHARED}, rules=object(),
        base_manifest=base, overwrite_conflicts=True,
    )
    assert plan_over.conflicts == ()
    assert [f.relpath for f in plan_over.to_write] == ["SOUL.md"]


def test_plan_flags_recreated_file_at_deleted_path_as_conflict(tmp_path: Path) -> None:
    """The remote deleted the path; the user recreated it with new content: the
    local work must survive and the conflict be surfaced (review r2 f2)."""
    base = {"skills/x.md": entry(b"synced v1", 0o644, Scope.SHARED)}
    home = make_home(tmp_path, {"skills/x.md": b"user's brand new local work"})
    manifest = {"skills/keep.md": entry(b"keep", 0o644, Scope.SHARED)}
    staging = make_staging(tmp_path, {"skills/keep.md": b"keep"})
    plan = restore.plan_install(
        home, staging, manifest, classes={Scope.SHARED}, rules=object(), base_manifest=base
    )
    assert plan.conflicts == ("skills/x.md",)
    assert plan.to_delete == ()
    assert (home / "skills/x.md").read_bytes() == b"user's brand new local work"
    plan_over = restore.plan_install(
        home, staging, manifest, classes={Scope.SHARED}, rules=object(),
        base_manifest=base, overwrite_conflicts=True,
    )
    assert plan_over.conflicts == ()
    assert plan_over.to_delete == ("skills/x.md",)


def test_plan_flags_never_synced_collision_as_conflict(tmp_path: Path) -> None:
    """A local file that was never synced colliding with a remote file of
    different content is a conflict; identical content is not."""
    remote = b"remote bytes"
    manifest = {"skills/new/SKILL.md": entry(remote, 0o644, Scope.SHARED)}
    home = make_home(tmp_path, {"skills/new/SKILL.md": b"local unsaved work"})
    staging = make_staging(tmp_path, {"skills/new/SKILL.md": remote})
    plan = restore.plan_install(
        home, staging, manifest, classes={Scope.SHARED}, rules=object(), base_manifest={}
    )
    assert plan.conflicts == ("skills/new/SKILL.md",)
    assert plan.to_write == ()
    # the same bytes on both sides are not a conflict — and per R3 they are
    # not a write either: identical staged and local content is `unchanged`
    home_same = make_home(tmp_path / "same", {"skills/new/SKILL.md": remote})
    staging_same = make_staging(tmp_path / "same-st", {"skills/new/SKILL.md": remote})
    plan_same = restore.plan_install(
        home_same, staging_same, manifest, classes={Scope.SHARED}, rules=object(),
        base_manifest={},
    )
    # RULING: R3 — identical staged vs local content is never rewritten,
    # never backed up, never counted; it is listed as unchanged instead.
    assert plan_same.conflicts == ()
    assert plan_same.to_write == ()
    assert plan_same.unchanged == ("skills/new/SKILL.md",)


def test_plan_deletes_untouched_file_the_remote_deleted(tmp_path: Path) -> None:
    """The negative space of the conflict check: a file the user never touched
    is still deleted when the remote deleted it."""
    base = {"skills/gone.md": entry(b"synced v1", 0o644, Scope.SHARED)}
    home = make_home(tmp_path, {"skills/gone.md": b"synced v1"})
    manifest = {}
    staging = make_staging(tmp_path, {})
    plan = restore.plan_install(
        home, staging, manifest, classes={Scope.SHARED}, rules=object(), base_manifest=base
    )
    assert plan.conflicts == ()
    assert plan.to_delete == ("skills/gone.md",)


def test_recover_rolls_back_interrupted_install(tmp_path: Path) -> None:
    home = make_home(
        tmp_path,
        {
            "skills/a/SKILL.md": b"tampered by interrupted install",
            "skills/new/SKILL.md": b"half-installed new file",
        },
    )
    backup_dir = tmp_path / "backups"
    (backup_dir / "skills/a/SKILL.md").parent.mkdir(parents=True)
    original = b"original bytes"
    (backup_dir / "skills/a/SKILL.md").write_bytes(original)
    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    record = {
        "v": 1,
        "transaction_id": "0123456789abcdef",
        "phase": "installing",
        "home": str(home),
        "backup_dir": str(backup_dir),
        "to_write": ["skills/a/SKILL.md", "skills/new/SKILL.md"],
        "to_delete": [],
        # R5: only skills/a/SKILL.md existed before the install
        "preexisting": ["skills/a/SKILL.md"],
        "created_at": 1.0,
        "updated_at": 2.0,
        "error": None,
    }
    journal_path = journal_dir / "1-0123456789abcdef.json"
    journal_path.write_bytes(json.dumps(record).encode())
    outcomes = restore.recover(home, journal_dir)
    assert len(outcomes) == 1
    assert (home / "skills/a/SKILL.md").read_bytes() == original
    assert not (home / "skills/new/SKILL.md").exists()
    assert json.loads(journal_path.read_bytes())["phase"] == "rolled_back"


def test_recover_closes_prepared_journal(tmp_path: Path) -> None:
    home = make_home(tmp_path, {})
    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    record = {
        "v": 1, "transaction_id": "0123456789abcdef", "phase": "prepared",
        "home": str(home), "backup_dir": str(tmp_path / "backups"),
        "to_write": [], "to_delete": [], "created_at": 1.0, "updated_at": 1.0, "error": None,
    }
    journal_path = journal_dir / "1-0123456789abcdef.json"
    journal_path.write_bytes(json.dumps(record).encode())
    outcomes = restore.recover(home, journal_dir)
    assert len(outcomes) == 1
    assert "nothing had been installed" in outcomes[0]
    assert json.loads(journal_path.read_bytes())["phase"] == "rolled_back"


# ---------------------------------------- three-way planning (R3)


def mk_plan(home: Path, staging: Path, manifest: dict, base: dict, **kw) -> restore.InstallPlan:
    return restore.plan_install(
        home, staging, manifest, classes={Scope.SHARED}, rules=object(), base_manifest=base, **kw
    )


def test_local_deletion_of_synced_file_stays_deleted(tmp_path: Path) -> None:
    """RULING: R3 — a base-managed file that is missing locally is a local
    deletion, not a silent reinstall: with the remote unchanged it stays
    deleted (and the next push publishes the deletion)."""
    synced = b"synced v1"
    manifest = {"SOUL.md": entry(synced, 0o644, Scope.SHARED)}
    base = {"SOUL.md": entry(synced, 0o644, Scope.SHARED)}
    home = make_home(tmp_path, {})  # the user deleted SOUL.md locally
    staging = make_staging(tmp_path, {"SOUL.md": synced})
    plan = mk_plan(home, staging, manifest, base)
    assert plan.to_write == ()
    assert plan.to_delete == ()
    assert plan.conflicts == ()
    assert plan.unchanged == ()
    assert any(
        "deleted locally" in note and "push" in note for note in plan.notes
    ), plan.notes
    # overwrite_conflicts does not resurrect it either: the remote never
    # changed the file, so the local deletion stands
    plan_over = mk_plan(home, staging, manifest, base, overwrite_conflicts=True)
    assert plan_over.to_write == ()
    assert plan_over.conflicts == ()


def test_local_deletion_vs_remote_change_is_a_conflict(tmp_path: Path) -> None:
    """RULING: R3 — the user deleted the file AND the remote changed it since
    the last sync: a conflict, kept local (deleted) unless overwrite."""
    synced = b"synced v1"
    changed = b"remote v2"
    manifest = {"SOUL.md": entry(changed, 0o644, Scope.SHARED)}
    base = {"SOUL.md": entry(synced, 0o644, Scope.SHARED)}
    home = make_home(tmp_path, {})
    staging = make_staging(tmp_path, {"SOUL.md": changed})
    plan = mk_plan(home, staging, manifest, base)
    assert plan.conflicts == ("SOUL.md",)
    assert plan.to_write == ()
    # overwrite_conflicts is the explicit remote-wins resolution
    plan_over = mk_plan(home, staging, manifest, base, overwrite_conflicts=True)
    assert plan_over.conflicts == ()
    assert [f.relpath for f in plan_over.to_write] == ["SOUL.md"]


def test_unchanged_content_is_never_rewritten(tmp_path: Path) -> None:
    """RULING: R3 — identical staged and local content is unchanged: never
    rewritten, never backed up, never counted, and a plan with nothing to do
    is a true no-op at apply time (no journal, no backup directory)."""
    content = b"same on both sides"
    manifest = {"SOUL.md": entry(content, 0o644, Scope.SHARED)}
    home = make_home(tmp_path, {"SOUL.md": content})
    staging = make_staging(tmp_path, {"SOUL.md": content})
    plan = mk_plan(home, staging, manifest, {})
    assert plan.unchanged == ("SOUL.md",)
    assert plan.to_write == ()
    assert plan.to_delete == ()
    assert plan.configs == ()
    result = restore.apply(
        home, plan, journal_dir=tmp_path / "journals", backup_dir=tmp_path / "backups",
        merge_config_fn=lambda *a: "", host_keys=HOST_KEYS,
    )
    assert result.files_written == 0
    assert result.backup_dir == ""
    assert result.journal_path == ""
    assert not (tmp_path / "journals").exists()
    assert not (tmp_path / "backups").exists()
    assert (home / "SOUL.md").read_bytes() == content


CONFIG_HOST_KEYS = ["terminal"]
CONFIG_BASE = "model:\n  provider: p-a\ncustom:\n  shared_setting: a-v1\n"
CONFIG_LOCAL_HOST = (
    "model:\n  provider: p-a\ncustom:\n  shared_setting: a-v1\nterminal:\n  theme: dark\n"
)


def projection(text: str) -> str:
    return scope.shared_projection_digest(text, CONFIG_HOST_KEYS)


def config_entry() -> dict:
    return {"sha256": "x", "size": 1, "mode": 0o644, "scope": Scope.SHARED.value}


def test_config_three_way_planning(tmp_path: Path) -> None:
    """RULING: R3 — configs are compared by shared projection digest local vs
    base vs remote: remote-only changes merge, local-only changes stay
    local, both-sides changes conflict, identical shared content is
    unchanged, and a config never synced here merges as today."""
    base_digests = {"config.yaml": projection(CONFIG_BASE)}

    def plan_for(local_text: str, staged_text: str, base: dict, **kw) -> restore.InstallPlan:
        home = make_home(tmp_path / f"home-{abs(hash((local_text, staged_text))) % 10**8}", {})
        if local_text:
            (home / "config.yaml").write_text(local_text)
        staging = make_staging(tmp_path / f"st-{abs(hash((local_text, staged_text))) % 10**8}", {})
        (staging / "config.yaml").write_text(staged_text)
        return restore.plan_install(
            home, staging, {"config.yaml": config_entry()},
            classes={Scope.SHARED}, rules=object(), base_manifest=base,
            config_base_digests=base_digests, shared_projection_fn=projection, **kw,
        )

    # identical shared projection (the local host keys are stripped): unchanged
    plan = plan_for(CONFIG_LOCAL_HOST, CONFIG_BASE, {"config.yaml": config_entry()})
    assert plan.unchanged == ("config.yaml",)
    assert plan.configs == ()
    assert plan.config_conflicts == ()

    # remote-only change: merge as today
    remote_v2 = "model:\n  provider: p-a\ncustom:\n  shared_setting: a-v2\n"
    plan = plan_for(CONFIG_LOCAL_HOST, remote_v2, {"config.yaml": config_entry()})
    assert [rel for rel, _ in plan.configs] == ["config.yaml"]
    assert plan.config_conflicts == ()
    assert plan.unchanged == ()

    # local-only change: kept local, never merged, surfaced via notes
    local_edit = (
        "model:\n  provider: p-a\ncustom:\n  shared_setting: b-edit\nterminal:\n  theme: dark\n"
    )
    plan = plan_for(local_edit, CONFIG_BASE, {"config.yaml": config_entry()})
    assert plan.configs == ()
    assert plan.config_conflicts == ()
    assert plan.unchanged == ()
    assert any("kept local" in note and "push" in note for note in plan.notes), plan.notes

    # both sides changed: a config conflict kept local unless overwrite
    plan = plan_for(local_edit, remote_v2, {"config.yaml": config_entry()})
    assert plan.config_conflicts == ("config.yaml",)
    assert plan.configs == ()
    plan_over = plan_for(local_edit, remote_v2, {"config.yaml": config_entry()}, overwrite_conflicts=True)
    assert plan_over.config_conflicts == ()
    assert [rel for rel, _ in plan_over.configs] == ["config.yaml"]

    # never synced on this device (first pull): merge as today
    plan = plan_for(CONFIG_LOCAL_HOST, remote_v2, {})
    assert [rel for rel, _ in plan.configs] == ["config.yaml"]
    assert plan.config_conflicts == ()


def test_config_planning_without_projection_fn_is_legacy_merge(tmp_path: Path) -> None:
    """A caller that provides no shared_projection_fn (old engine) gets the
    legacy behaviour: configs merge as today, no three-way verdicts."""
    home = make_home(tmp_path, {"config.yaml": CONFIG_LOCAL_HOST.encode()})
    staging = make_staging(tmp_path, {"config.yaml": CONFIG_BASE.encode()})
    plan = restore.plan_install(
        home, staging, {"config.yaml": config_entry()},
        classes={Scope.SHARED}, rules=object(), base_manifest={"config.yaml": config_entry()},
        is_config=lambda rel: rel == "config.yaml",
    )
    assert [rel for rel, _ in plan.configs] == ["config.yaml"]
    assert plan.config_conflicts == ()
    assert plan.unchanged == ()


def test_config_refused_by_local_rules_is_skipped(tmp_path: Path) -> None:
    """A config path this device's rules keep local is never merged."""
    home = make_home(tmp_path, {})
    staging = make_staging(tmp_path, {"config.yaml": CONFIG_BASE.encode()})
    local_entry = dict(config_entry(), scope=Scope.LOCAL.value)
    plan = restore.plan_install(
        home, staging, {"config.yaml": local_entry},
        classes={Scope.SHARED}, rules=object(), base_manifest={},
    )
    assert plan.configs == ()
    assert plan.skipped == ("config.yaml",)


# ---------------------------------------- backup retention + recovery (R5)


def test_prune_backups_keeps_the_newest_window(tmp_path: Path) -> None:
    """RULING: R5 — only the newest keep-window of backup dirs survives;
    committed/rolled_back journals older than the window go with them, and
    live journals (installing/unrecoverable/unreadable) are never pruned."""
    backups = tmp_path / "backups"
    journals = tmp_path / "journals"
    journals.mkdir()
    dir_names = []
    for epoch in range(1_700_000_000, 1_700_000_008):  # eight generations
        name = time.strftime("%Y%m%d-%H%M%S", time.gmtime(epoch))
        dir_names.append((epoch, name))
        (backups / name).mkdir(parents=True)
        (backups / name / "config.yaml").write_text("pre-merge config")
        phase = "installing" if epoch == 1_700_000_003 else "committed"
        (journals / f"{epoch}-tx.json").write_text(
            json.dumps({"v": 1, "phase": phase, "error": None})
        )
    # an unrecoverable journal and an unreadable one: live evidence forever
    (journals / "1699999999-tx.json").write_text(
        json.dumps({"v": 1, "phase": "unrecoverable", "error": "no backup"})
    )
    (journals / "1699999998-tx.json").write_text("not json at all")
    # a foreign directory: never touched
    (backups / "user-data").mkdir()

    pruned = restore.prune_backups(backups, journals, keep=5)
    remaining_dirs = sorted(d.name for d in backups.iterdir() if d.is_dir())
    assert remaining_dirs == sorted(
        [name for _, name in dir_names[3:]] + ["user-data"]
    ), remaining_dirs
    remaining_journals = sorted(j.name for j in journals.glob("*.json"))
    # the three pruned generations' committed journals are gone; the
    # installing/unrecoverable/unreadable journals and the kept window's
    # journals stay
    assert remaining_journals == sorted(
        [f"{epoch}-tx.json" for epoch, _ in dir_names[3:]]
        + ["1699999999-tx.json", "1699999998-tx.json"]
    ), remaining_journals
    assert pruned == 3 + 3


def test_prune_backups_keeps_everything_below_the_window(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    journals = tmp_path / "journals"
    backups.mkdir()
    journals.mkdir()
    for epoch in (1_700_000_100, 1_700_000_200):
        name = time.strftime("%Y%m%d-%H%M%S", time.gmtime(epoch))
        (backups / name).mkdir()
        (journals / f"{epoch}-tx.json").write_text(
            json.dumps({"v": 1, "phase": "committed", "error": None})
        )
    assert restore.prune_backups(backups, journals, keep=5) == 0
    assert len(list(backups.iterdir())) == 2
    assert len(list(journals.glob("*.json"))) == 2


def test_recover_never_unlinks_without_a_backup(tmp_path: Path) -> None:
    """RULING: R5 — with the backup window pruned, recovery leaves the
    pre-existing half-installed file in place, marks the journal
    unrecoverable, and surfaces it; the file roam itself created is still
    removed (that destroys nothing)."""
    home = make_home(
        tmp_path,
        {"SOUL.md": b"half-installed v2", "skills/new.md": b"half-installed new"},
    )
    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    record = {
        "v": 1,
        "transaction_id": "0123456789abcdef",
        "phase": "installing",
        "home": str(home),
        "backup_dir": str(tmp_path / "backups"),  # pruned by the user
        "to_write": ["SOUL.md", "skills/new.md"],
        "to_delete": [],
        "preexisting": ["SOUL.md"],
        "created_at": 1.0,
        "updated_at": 2.0,
        "error": None,
    }
    journal_path = journal_dir / "1-0123456789abcdef.json"
    journal_path.write_bytes(json.dumps(record).encode())
    outcomes = restore.recover(home, journal_dir)
    assert len(outcomes) == 1
    assert "unrecoverable" in outcomes[0]
    # the pre-pull file was only in the pruned backup: it is LEFT IN PLACE
    assert (home / "SOUL.md").read_bytes() == b"half-installed v2"
    # the file the aborted install created is removed
    assert not (home / "skills/new.md").exists()
    assert json.loads(journal_path.read_bytes())["phase"] == "unrecoverable"
    # a second recovery run re-surfaces the unrecoverable journal and still
    # destroys nothing
    outcomes2 = restore.recover(home, journal_dir)
    assert len(outcomes2) == 1
    assert "still unrecoverable" in outcomes2[0]
    assert (home / "SOUL.md").read_bytes() == b"half-installed v2"


def test_recover_old_journal_without_preexisting_never_unlinks(tmp_path: Path) -> None:
    """A journal written before the preexisting field existed is treated as
    fully pre-existing: nothing is unlinked without a backup."""
    home = make_home(tmp_path, {"SOUL.md": b"half-installed"})
    backup_dir = tmp_path / "backups"
    (backup_dir / "skills/a").mkdir(parents=True)
    (backup_dir / "skills/a" / "SKILL.md").write_bytes(b"original")
    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    record = {
        "v": 1,
        "transaction_id": "0123456789abcdef",
        "phase": "installing",
        "home": str(home),
        "backup_dir": str(backup_dir),
        "to_write": ["skills/a/SKILL.md", "SOUL.md"],
        "to_delete": [],
        "created_at": 1.0,
        "updated_at": 2.0,
        "error": None,
    }
    journal_path = journal_dir / "1-0123456789abcdef.json"
    journal_path.write_bytes(json.dumps(record).encode())
    outcomes = restore.recover(home, journal_dir)
    assert len(outcomes) == 1
    assert "unrecoverable" in outcomes[0]
    assert (home / "skills/a/SKILL.md").read_bytes() == b"original"  # had a backup
    assert (home / "SOUL.md").read_bytes() == b"half-installed"  # left in place
    assert json.loads(journal_path.read_bytes())["phase"] == "unrecoverable"


def test_apply_failure_without_backup_marks_unrecoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing dangling-symlink config has no backup: a failed install
    leaves it in place, marks the journal unrecoverable, and says so loudly."""
    home = make_home(tmp_path, {})
    (home / "config.yaml").symlink_to(tmp_path / "gone")  # pre-existing, no backup possible
    staging = make_staging(
        tmp_path, {"config.yaml": CONFIG_BASE.encode(), "skills/b/SKILL.md": b"other"}
    )
    plan = restore.plan_install(
        home, staging,
        {"config.yaml": config_entry(), "skills/b/SKILL.md": entry(b"other", 0o644, Scope.SHARED)},
        classes={Scope.SHARED}, rules=object(), base_manifest={},
    )
    assert [rel for rel, _ in plan.configs] == ["config.yaml"]
    assert [f.relpath for f in plan.to_write] == ["skills/b/SKILL.md"]
    real_install = restore._install_from_staged
    installs = []

    def flaky(target: Path, staged: Path, mode: int) -> None:
        installs.append(target)
        real_install(target, staged, mode)
        if len(installs) == 1:
            raise OSError("simulated disk failure")

    monkeypatch.setattr(restore, "_install_from_staged", flaky)
    with pytest.raises(RestoreError) as excinfo:
        restore.apply(
            home, plan, journal_dir=tmp_path / "journals", backup_dir=tmp_path / "backups",
            merge_config_fn=lambda *a: "merged: x\n", host_keys=HOST_KEYS,
        )
    assert "could not be restored" in str(excinfo.value)
    assert "unrecoverable" in str(excinfo.value)
    # nothing was destroyed: the pre-existing symlink is still there exactly
    # as it was and the file the aborted install created is removed
    assert (home / "config.yaml").is_symlink()
    assert not (home / "skills/b/SKILL.md").exists()
    journals = list((tmp_path / "journals").glob("*.json"))
    assert len(journals) == 1
    assert json.loads(journals[0].read_bytes())["phase"] == "unrecoverable"
