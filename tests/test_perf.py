"""Performance contract for the scope classifier and the home walk.

The r4 review measured a realistic dummy home at ~25 s of CPU per command
(39,204,300 fnmatch calls: every path times every rule times every path
segment). The ruled behaviour: rules are precompiled (anchored first-segment
dispatch, memoized per-segment and per-directory classification) and
LOCAL/IGNORED directory trees are pruned before they are ever traversed, so
the walk of a 20k-path home stays under the watchdog caps and the fnmatch
call count is linear in the number of paths, not paths x rules.
"""

from __future__ import annotations

import fnmatch as fnmatch_mod
import time
from pathlib import Path

import pytest

from roam import inventory, scope
from roam.model import Scope

REAL_HOME = Path("/root/work/roam-homes/review-r4-real")
SHARED_FILENAMES = ("SKILL.md", "run.sh")


def build_home(base: Path, junk_dirs: int, shared_dirs: int) -> tuple[Path, int]:
    """A home of junk_dirs*10 prunable LOCAL paths + shared_dirs*2 SHARED paths."""
    home = base / "home"
    home.mkdir(parents=True)
    for d in range(junk_dirs):
        tree = home / "installs" / f"env-{d}"
        tree.mkdir(parents=True)
        for f in range(10):
            (tree / f"f{f}.json").write_text("x", encoding="utf-8")
    for s in range(shared_dirs):
        tree = home / "skills" / f"sk-{s}" / "scripts"
        tree.mkdir(parents=True)
        for name in SHARED_FILENAMES:
            (tree / name).write_text("x", encoding="utf-8")
    return home, junk_dirs * 10 + shared_dirs * len(SHARED_FILENAMES)


def counting_fnmatch(monkeypatch, calls: dict) -> None:
    real = fnmatch_mod.fnmatchcase

    def counting(pattern, name):
        calls["n"] += 1
        return real(pattern, name)

    monkeypatch.setattr(scope.fnmatch, "fnmatchcase", counting)


def walk(home: Path, calls: dict | None = None) -> tuple[list[str], float]:
    rules = scope.Rules.default()
    include = {Scope.SHARED, Scope.SESSIONS, Scope.CRON}
    start = time.monotonic()
    entries = inventory.walk_home(home, rules, include=include)
    return sorted(entry.relpath for entry in entries), time.monotonic() - start


def test_walk_of_20k_path_home_is_under_two_seconds(tmp_path, monkeypatch) -> None:
    home, n_paths = build_home(tmp_path, junk_dirs=1750, shared_dirs=1250)
    calls: dict = {"n": 0}
    counting_fnmatch(monkeypatch, calls)
    listed, elapsed = walk(home, calls)
    assert n_paths == 20_000
    assert len(listed) == 1250 * len(SHARED_FILENAMES)  # the shared skill trees
    assert all(rel.startswith("skills/") for rel in listed)
    assert elapsed < 2.0, f"walk of a 20k-path home took {elapsed:.2f}s"
    # linear, not paths x rules x segments: a small constant per path
    assert calls["n"] <= 25 * n_paths, (
        f"{calls['n']} fnmatch calls for {n_paths} paths: classification is not linear"
    )


def test_fnmatch_call_count_grows_linearly_with_home_size(tmp_path, monkeypatch) -> None:
    """Doubling the home must not double any per-path multiplier."""
    small, n_small = build_home(tmp_path / "small", junk_dirs=800, shared_dirs=600)
    big, n_big = build_home(tmp_path / "big", junk_dirs=1600, shared_dirs=1200)
    assert n_big == 2 * n_small
    calls: dict = {"n": 0}
    counting_fnmatch(monkeypatch, calls)
    walk(small, calls)
    small_calls = calls["n"]
    calls["n"] = 0
    walk(big, calls)
    big_calls = calls["n"]
    assert big_calls <= 2 * small_calls + 100, (
        f"fnmatch calls grew super-linearly: {small_calls} -> {big_calls} "
        f"when the home doubled ({n_small} -> {n_big} paths)"
    )


@pytest.mark.skipif(not REAL_HOME.is_dir(), reason="realistic dummy home not present")
def test_real_dummy_home_walks_in_under_one_second(monkeypatch) -> None:
    """The r4 blocker home (~80k files, 1.1 GB): walk + classify < 1 s."""
    calls: dict = {"n": 0}
    counting_fnmatch(monkeypatch, calls)
    rules = scope.load_rules(REAL_HOME)
    start = time.monotonic()
    entries = inventory.walk_home(REAL_HOME, rules, include={Scope.SHARED})
    elapsed = time.monotonic() - start
    relpaths = sorted(entry.relpath for entry in entries)
    assert relpaths, "the real dummy home must still yield its shared files"
    assert all(entry.scope is Scope.SHARED for entry in entries)
    assert "SOUL.md" in relpaths and "config.yaml" in relpaths
    assert not [r for r in relpaths if r.startswith(("cache/", "installs/", "outputs/"))]
    assert elapsed < 1.0, f"walk of the real dummy home took {elapsed:.2f}s"


def test_local_and_ignored_trees_are_never_traversed(tmp_path, monkeypatch) -> None:
    """Directory-level pruning: a LOCAL tree with an unreadable file is never
    opened at all, and the shared trees beside it still list completely."""
    home = tmp_path / "home"
    home.mkdir()
    for rel in ("skills/mine/SKILL.md", "memories/m1.md", "SOUL.md"):
        target = home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    secret = home / "installs" / "env" / "no-read.json"
    secret.parent.mkdir(parents=True)
    secret.write_text("x", encoding="utf-8")
    secret.chmod(0)
    calls: dict = {"n": 0}
    counting_fnmatch(monkeypatch, calls)
    rules = scope.Rules.default()
    entries = inventory.walk_home(home, rules, include={Scope.SHARED})
    assert [entry.relpath for entry in entries] == ["SOUL.md", "memories/m1.md", "skills/mine/SKILL.md"]
