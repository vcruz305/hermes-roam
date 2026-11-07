"""Frozen backend protocol. Implementations: local.py, s3.py, git.py."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class Backend(Protocol):
    name: str

    def get_head(self, *, deadline: float | None = None) -> tuple[bytes, str] | None:
        """Return (json bytes, version token) or None when no HEAD exists yet.

        ``deadline`` is the ABSOLUTE time.monotonic() budget of the user
        operation in flight (R8): one budget per operation, shared by every
        backend call it makes. ``None`` means no budget was passed (legacy
        callers); backends still apply their own per-call caps.
        """
        ...

    def put_head(
        self, data: bytes, expected: str | None, *, deadline: float | None = None
    ) -> str:
        """Compare-and-swap HEAD. ``expected=None`` means HEAD must not exist.
        Raise ``HeadConflict`` when the current token differs. Return the new
        token. ``deadline`` as in ``get_head``.
        """
        ...

    def put_blob(
        self, key: str, source: Path, *, deadline: float | None = None
    ) -> None:
        """Store an immutable object. Raise ``BlobExists`` if ``key`` is already
        present. ``deadline`` as in ``get_head``.
        """
        ...

    def get_blob(
        self,
        key: str,
        dest: Path,
        *,
        max_bytes: int,
        deadline: float | None = None,
    ) -> None:
        """Download ``key`` to ``dest`` (write to a temp sibling then rename).
        Refuse > max_bytes. ``deadline`` as in ``get_head``.
        """
        ...

    def blob_exists(self, key: str, *, deadline: float | None = None) -> bool:
        """Cheap existence check. ``deadline`` as in ``get_head``."""
        ...

    def ping(self, *, deadline: float | None = None) -> str:
        """Cheap reachability + credential check, capped at a few seconds.
        Return a one-line human summary; raise BackendError. ``deadline`` as
        in ``get_head``.
        """
        ...
