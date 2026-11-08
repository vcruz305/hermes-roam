"""0.5.4: roam's first real Windows run (Hermes 0.23.0, Windows 11) found
that every pull failed with PermissionError (Windows cannot open a directory
to fsync it) and the per-home lock was a silent no-op (no fcntl). These pin
both fixes without needing a Windows host."""

import builtins
import os
import sys
import types

from roam import engine, restore
from roam.backends import local


def test_fsync_dir_is_skipped_on_windows(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os, "open", lambda *a, **k: opened.append(a) or 0)
    for fn in (engine._fsync_dir, local._fsync_dir, restore._fsync_dir):
        fn(tmp_path)
    assert opened == []


def _without_fcntl(monkeypatch, fake_msvcrt):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("no fcntl on Windows")
        if name == "msvcrt":
            return fake_msvcrt
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "fcntl", raising=False)


def test_home_lock_uses_msvcrt_region_lock_past_the_record(monkeypatch, tmp_path):
    calls = []
    held = {"busy": False}

    def locking(fd, mode, nbytes):
        calls.append((os.lseek(fd, 0, os.SEEK_CUR), mode, nbytes))
        if mode == fake.LK_NBLCK and held["busy"]:
            raise OSError("locked")

    fake = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking)
    _without_fcntl(monkeypatch, fake)
    fd = os.open(tmp_path / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        assert engine._try_lock_nb(fd) is True
        engine._unlock(fd)
        held["busy"] = True
        assert engine._try_lock_nb(fd) is False
    finally:
        os.close(fd)
    assert calls[0] == (engine._WIN_LOCK_OFFSET, fake.LK_NBLCK, 1)
    assert calls[1] == (engine._WIN_LOCK_OFFSET, fake.LK_UNLCK, 1)


def test_read_only_archive_members_are_replaceable_and_removable_on_windows(
    monkeypatch, tmp_path
):
    """Session-archive members land 0444; Windows refuses os.replace onto,
    unlink of, and rmtree over read-only files. winfs clears the attribute
    first (a real Windows pull failed with WinError 5 before 0.5.4)."""
    from roam import winfs

    cleared = []
    real_chmod = os.chmod
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(
        os, "chmod", lambda p, m: cleared.append(os.fspath(p)) or real_chmod(p, m)
    )
    dst = tmp_path / "member.md"
    dst.write_text("old")
    real_chmod(dst, 0o444)
    src = tmp_path / "staged.md"
    src.write_text("new")
    winfs.replace(src, dst)
    assert dst.read_text() == "new" and os.fspath(dst) in cleared
    real_chmod(dst, 0o444)
    winfs.unlink(dst)
    assert not dst.exists()
