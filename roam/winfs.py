"""Windows file-attribute helpers.

Roam installs session-archive members read-only (0444). On Windows a
read-only file cannot be the destination of ``os.replace``, cannot be
unlinked, and stops ``shutil.rmtree`` — POSIX allows all three because there
the directory's permissions decide. These helpers clear the read-only
attribute first on Windows and are no-ops everywhere else.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path


def make_writable(path: str | os.PathLike) -> None:
    """Clear the Windows read-only attribute so the file can be replaced or removed."""
    if os.name != "nt":
        return
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    except OSError:
        pass


def replace(src: str | os.PathLike, dst: str | os.PathLike) -> None:
    """``os.replace`` that also succeeds over a read-only destination on Windows."""
    if os.name == "nt" and os.path.lexists(dst):
        make_writable(dst)
    os.replace(src, dst)


def unlink(path: Path, *, missing_ok: bool = True) -> None:
    """``Path.unlink`` that also removes read-only files on Windows."""
    make_writable(path)
    path.unlink(missing_ok=missing_ok)


def rmtree(path: str | os.PathLike, *, ignore_errors: bool = False) -> None:
    """``shutil.rmtree`` that clears read-only attributes and retries on Windows."""
    if os.name != "nt":
        shutil.rmtree(path, ignore_errors=ignore_errors)
        return

    def _retry(func, target, _exc):  # pragma: no cover - Windows
        try:
            make_writable(target)
            func(target)
        except OSError:
            if not ignore_errors:
                raise

    if sys.version_info >= (3, 12):  # pragma: no cover - Windows
        shutil.rmtree(path, onexc=_retry)
    else:  # pragma: no cover - Windows on 3.11
        shutil.rmtree(path, onerror=_retry)
