"""In-memory Backend with real CAS semantics (token compare-and-swap, immutable blobs)."""

from __future__ import annotations

import os
from pathlib import Path

from roam.errors import BackendError, BlobExists, HeadConflict


class FakeBackend:
    """Duck-typed Backend: put_head enforces compare-and-swap, blobs are immutable.

    Every method accepts the engine's ``deadline`` kwarg (R8) and records it so
    tests can assert one budget per user operation.
    """

    name = "fake"

    def __init__(self, *, fail_ping: bool = False) -> None:
        self._blobs: dict[str, bytes] = {}
        self._head: tuple[bytes, str] | None = None
        self._token_counter = 0
        self.fail_ping = fail_ping
        self.calls: list[str] = []
        self.deadlines: list[float | None] = []

    def _note(self, call: str, deadline: float | None) -> None:
        self.calls.append(call)
        self.deadlines.append(deadline)

    def get_head(self, *, deadline: float | None = None) -> tuple[bytes, str] | None:
        self._note("get_head", deadline)
        if self._head is None:
            return None
        return self._head[0], self._head[1]

    def put_head(self, data: bytes, expected: str | None, *, deadline: float | None = None) -> str:
        self._note("put_head", deadline)
        current = self._head
        if expected is None:
            if current is not None:
                raise HeadConflict("HEAD already exists")
        elif current is None or current[1] != expected:
            raise HeadConflict("HEAD changed since it was read")
        self._token_counter += 1
        token = f"fake-token-{self._token_counter}"
        self._head = (data, token)
        return token

    def put_blob(self, key: str, source: Path, *, deadline: float | None = None) -> None:
        self._note(f"put_blob:{key}", deadline)
        if key in self._blobs:
            raise BlobExists(f"bundle object already exists: {key}")
        self._blobs[key] = Path(source).read_bytes()

    def get_blob(self, key: str, dest: Path, *, max_bytes: int, deadline: float | None = None) -> None:
        self._note(f"get_blob:{key}", deadline)
        if key not in self._blobs:
            raise BackendError(f"bundle object not found: {key}")
        data = self._blobs[key]
        if len(data) > max_bytes:
            raise BackendError(f"bundle object exceeds the {max_bytes} byte limit")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.part")
        tmp.write_bytes(data)
        os.replace(tmp, dest)

    def blob_exists(self, key: str, *, deadline: float | None = None) -> bool:
        self._note(f"blob_exists:{key}", deadline)
        return key in self._blobs

    def ping(self, *, deadline: float | None = None) -> str:
        self._note("ping", deadline)
        if self.fail_ping:
            raise BackendError("fake backend is unreachable")
        return "fake backend reachable"
