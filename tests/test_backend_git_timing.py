"""Real-clock measurements of the git backend's R8 deadline machinery.

Everything here runs on the wall clock and asserts the ruled bounds:
- a blackholed ssh:// or https:// remote fails within ~20 s for ping and
  get_head (the ls-remote probe is capped at 15 s; ssh ConnectTimeout at
  10 s), and
- a legitimate transfer that keeps making progress for 30 s SUCCEEDS,
  because the transfer cap is settings.git_transfer_timeout (600 s default),
  not the old 20 s transport cap — while a transfer that outlives a
  configured smaller cap is killed at that cap.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from roam.backends.git import DEFAULT_TRANSFER_TIMEOUT, PROBE_CAP, GitBackend
from roam.errors import BackendError

BRANCH = "roam"
BLACKHOLE_SSH = "ssh://git@10.255.255.1/x.git"
BLACKHOLE_HTTPS = "https://10.255.255.1/x.git"
REAL_GIT = shutil.which("git")  # resolved before any PATH swap


def _install_fake_git(tmp_path: Path, handlers: dict[str, str]) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    lines = ["#!/bin/sh", 'case "$1" in']
    for sub, body in handlers.items():
        lines.append(f"  {sub}) {body} ;;")
    lines.append(f'  *) exec "{REAL_GIT}" "$@" ;;')
    lines.append("esac")
    fake = bin_dir / "git"
    fake.write_text("\n".join(lines) + "\n", encoding="utf-8")
    fake.chmod(0o755)
    os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]


def _restore_real_git() -> None:
    os.environ["PATH"] = os.pathsep.join(
        os.environ["PATH"].split(os.pathsep)[1:]
    )


def _timed(fn) -> tuple[float, str]:
    started = time.monotonic()
    try:
        fn()
        return time.monotonic() - started, ""
    except BackendError as exc:
        return time.monotonic() - started, str(exc)


def _make_bare(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    return bare


def _make_bare_with_roam(tmp_path: Path) -> Path:
    bare = _make_bare(tmp_path)
    work = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    (work / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(work), "-c", "user.email=s@e", "-c", "user.name=s",
         "commit", "-q", "-m", "seed"], check=True,
    )
    subprocess.run(["git", "-C", str(work), "branch", "-M", BRANCH], check=True)
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", str(bare), BRANCH], check=True,
    )
    return bare


# 30 x (1 s of streaming + a progress line): a transfer that keeps making
# progress for 30 s, like a real large bundle over a slow link.
_SLOW_PUSH = (
    "i=0; while [ $i -lt 30 ]; do printf 'progress %d/30\\n' $i >&2; "
    "sleep 1; i=$((i+1)); done; exit 0"
)


def test_blackholed_ssh_remote_fails_within_20s(tmp_path: Path) -> None:
    """Measured, real clock: ping and get_head on a blackholed ssh://
    remote must fail within ~20 s (probe cap 15 s; ssh ConnectTimeout 10 s)."""
    backend = GitBackend(BLACKHOLE_SSH, BRANCH, tmp_path / "m-ssh")
    ping_s, ping_err = _timed(backend.ping)
    head_s, head_err = _timed(backend.get_head)
    print(f"MEASURE ssh blackhole: ping {ping_s:.1f}s, get_head {head_s:.1f}s")
    assert ping_err and head_err  # both fail with a named BackendError
    assert ping_s < 20.0, f"ping took {ping_s:.1f}s"
    assert head_s < 20.0, f"get_head took {head_s:.1f}s"


def test_blackholed_https_remote_fails_within_20s(tmp_path: Path) -> None:
    """Measured, real clock: curl has no connect timeout, so the PROBE_CAP
    subprocess kill is what bounds a blackholed https connect — both
    operations fail within ~20 s."""
    backend = GitBackend(BLACKHOLE_HTTPS, BRANCH, tmp_path / "m-https")
    ping_s, ping_err = _timed(backend.ping)
    head_s, head_err = _timed(backend.get_head)
    print(f"MEASURE https blackhole: ping {ping_s:.1f}s, get_head {head_s:.1f}s")
    assert ping_err and head_err
    assert ping_s < 20.0, f"ping took {ping_s:.1f}s"
    assert head_s < 20.0, f"get_head took {head_s:.1f}s"


def test_probe_cap_is_15s_and_transfer_default_is_600s() -> None:
    assert PROBE_CAP == 15.0
    assert DEFAULT_TRANSFER_TIMEOUT == 600.0


def test_slow_legit_transfer_of_30s_succeeds(tmp_path: Path) -> None:
    """Measured, real clock: a transfer that streams for 30 s must SUCCEED —
    the cap is the transfer timeout (600 s default), not the old 20 s
    transport cap that made such bundles permanently unpublishable."""
    bare = _make_bare(tmp_path)
    _install_fake_git(tmp_path, {"push": _SLOW_PUSH})
    try:
        backend = GitBackend(str(bare), BRANCH, tmp_path / "m-slow")
        source = tmp_path / "bundle.roam"
        source.write_bytes(b"payload")
        started = time.monotonic()
        backend.put_blob("bundles/1-dev-abc.roam", source)  # no error: it went through
        elapsed = time.monotonic() - started
        print(f"MEASURE slow legit transfer: {elapsed:.1f}s, succeeded")
        assert elapsed >= 29.0, "the 30 s transfer must be allowed to run"
        assert elapsed < 60.0
    finally:
        _restore_real_git()


def test_transfer_cap_kills_a_transfer_that_outlives_it(tmp_path: Path) -> None:
    """Measured, real clock: with a smaller configured cap the same 30 s
    transfer is killed at the cap with a named error."""
    bare = _make_bare(tmp_path)
    _install_fake_git(tmp_path, {"push": _SLOW_PUSH})
    try:
        backend = GitBackend(
            str(bare), BRANCH, tmp_path / "m-capped", transfer_timeout=5.0
        )
        source = tmp_path / "bundle.roam"
        source.write_bytes(b"payload")
        started = time.monotonic()
        with pytest.raises(BackendError, match="timed out"):
            backend.put_blob("bundles/1-dev-abc.roam", source)
        elapsed = time.monotonic() - started
        print(f"MEASURE capped transfer: killed at {elapsed:.1f}s (cap 5 s)")
        assert elapsed < 12.0, f"the cap must kill at ~5 s, took {elapsed:.1f}s"
    finally:
        _restore_real_git()


def test_timeout_kill_no_longer_wedges_the_mirror_end_to_end(tmp_path: Path) -> None:
    """Real git killed mid-ref-update leaves refs/remotes/origin/<branch>.lock;
    the next backend init sweeps it and a real fetch goes through."""
    bare = _make_bare_with_roam(tmp_path)
    wedge = (
        "mkdir -p .git/refs/remotes/origin && "
        ": > .git/refs/remotes/origin/roam.lock && sleep 30"
    )
    _install_fake_git(tmp_path, {"fetch": wedge})
    try:
        backend = GitBackend(
            str(bare), BRANCH, tmp_path / "m-wedge", transfer_timeout=2.0
        )
        started = time.monotonic()
        with pytest.raises(BackendError):
            backend.get_head()  # killed at the 2 s cap mid-ref-update
        elapsed = time.monotonic() - started
        print(f"MEASURE wedge kill: {elapsed:.1f}s")
        assert elapsed < 10.0
        lock = tmp_path / "m-wedge" / ".git" / "refs" / "remotes" / "origin" / f"{BRANCH}.lock"
        assert lock.is_file(), "the kill left the mirror ref lock behind"

        # a fresh backend sweeps the stale lock at init...
        GitBackend(str(bare), BRANCH, tmp_path / "m-wedge", transfer_timeout=2.0)
        assert not lock.is_file()
    finally:
        _restore_real_git()

    # ...and the real fetch then goes through: no permanent wedge
    proc = subprocess.run(
        ["git", "fetch", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}"],
        cwd=str(tmp_path / "m-wedge"), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"real fetch still fails: {proc.stderr.strip()}"
