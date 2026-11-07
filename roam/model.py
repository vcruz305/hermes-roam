"""Frozen data model for Hermes Roam. Lanes implement behind these types; do not change signatures."""

from __future__ import annotations

import base64
import binascii
import dataclasses
import enum
import json
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import WeakArchiveKey

ROAM_VERSION = "0.1.0"
HEAD_KEY = "HEAD"
BUNDLE_PREFIX = "bundles/"
STATE_DIRNAME = "roam"  # <home>/roam/ — all of it LOCAL except rules.yaml
DEFAULT_MAX_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024

SECRET_ARCHIVE_KEY = "ROAM_ARCHIVE_KEY"
SECRET_S3_ACCESS_KEY_ID = "ROAM_S3_ACCESS_KEY_ID"
SECRET_S3_SECRET_ACCESS_KEY = "ROAM_S3_SECRET_ACCESS_KEY"


class Scope(enum.StrEnum):
    SHARED = "shared"
    LOCAL = "local"
    SESSIONS = "sessions"
    CRON = "cron"
    IGNORED = "ignored"


def decode_key(value: str) -> bytes:
    """Accept a 32-byte key as base64 (std or urlsafe) or 64 hex chars. Raise ValueError otherwise."""
    text = value.strip()
    raw = _decode_key_bytes(text)
    if len(set(raw)) == 1:
        raise WeakArchiveKey(
            "weak key: ROAM_ARCHIVE_KEY decodes to 32 identical bytes and has no "
            "entropy; generate a real key with "
            "'python -c \"import secrets;print(secrets.token_urlsafe(32))\"'"
        )
    return raw


def _decode_key_bytes(text: str) -> bytes:
    if len(text) == 64:
        try:
            raw = binascii.unhexlify(text)
            if len(raw) == 32:
                return raw
        except (binascii.Error, ValueError):
            pass
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(text + "=" * (-len(text) % 4))
        except (binascii.Error, ValueError):
            continue
        if len(raw) == 32:
            return raw
    raise ValueError("ROAM_ARCHIVE_KEY must decode to exactly 32 bytes (base64 or 64 hex chars)")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True, slots=True)
class RoamSettings:
    backend: str = "local"  # "s3" | "git" | "local"
    device_name: str = field(default_factory=lambda: socket.gethostname().split(".")[0] or "device")
    s3_bucket: str = ""
    s3_prefix: str = "hermes-roam"
    s3_endpoint_url: str = ""
    s3_region: str = "auto"
    git_remote: str = ""
    git_branch: str = "roam"
    local_dir: str = ""
    # 0.4.1: sync_sessions publishes a READ-ONLY archive of this device's
    # session transcripts (bundle paths under archive/sessions/<device>/ only).
    # The raw sessions/ folder is always LOCAL: request dumps never travel.
    sync_sessions: bool = False
    sessions_max_mb: int = 50  # cap on this device's published archive, newest first
    sync_cron: bool = False
    allow_agent_apply: bool = False  # gate for model-driven roam pull --apply (default off)
    auto_push: bool = False
    auto_pull_shared: bool = False
    push_debounce_seconds: int = 300
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES
    operation_timeout: float = 900.0  # one budget for a whole push/pull (R8)
    git_transfer_timeout: float = 600.0  # cap on a single git data transfer (R8)
    backup_keep: int = 5  # newest backup dirs retained (R5)
    # secrets — never persisted, never printed
    archive_key: bytes = b""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    def redacted(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["archive_key"] = "<set>" if self.archive_key else "<missing>"
        d["s3_access_key_id"] = "<set>" if self.s3_access_key_id else ""
        d["s3_secret_access_key"] = "<set>" if self.s3_secret_access_key else ""
        return d


@dataclass(frozen=True, slots=True)
class FileEntry:
    relpath: str  # POSIX relative to home
    sha256: str
    size: int
    mode: int  # permission bits only (0o644 / 0o755)
    scope: Scope

    def to_dict(self) -> dict[str, Any]:
        return {"sha256": self.sha256, "size": self.size, "mode": self.mode, "scope": self.scope.value}


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    name: str
    last_push_generation: int = 0
    last_pull_generation: int = 0
    hermes_version: str = ""
    platform: str = ""
    seen_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> DeviceInfo:
        return cls(name=name, **{k: d[k] for k in d if k in cls.__dataclass_fields__ and k != "name"})


@dataclass(slots=True)
class Head:
    generation: int
    device: str
    hermes_version: str
    created_at: float
    bundle_key: str = ""  # empty for generation 0 (init, no bundle)
    bundle_sha256: str = ""
    bundle_size: int = 0
    plain_sha256: str = ""
    files: dict[str, dict[str, Any]] = field(default_factory=dict)  # relpath -> FileEntry.to_dict()
    parent_generation: int = -1
    # 0.4: the previous head's bundle key, so the bundle chain is verifiable;
    # empty for generation 0 and for heads written before 0.4.
    parent_bundle_key: str = ""
    devices: dict[str, dict[str, Any]] = field(default_factory=dict)
    v: int = 1
    sig: str = ""  # hex HMAC; empty until signed

    def unsigned_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d.pop("sig", None)
        return d

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Head:
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass(frozen=True, slots=True)
class LocalState:
    """<home>/roam/state.json — what this device last saw. All LOCAL."""

    last_pushed_generation: int = 0
    last_pulled_generation: int = 0
    last_seen_generation: int = 0  # remote head generation observed at last push/pull/status
    last_push_at: float = 0.0
    last_pull_at: float = 0.0
    base_files: dict[str, dict[str, Any]] = field(default_factory=dict)  # manifest at last sync point
    # 0.4.2: shared-projection digests of each config this device ever
    # ACCEPTED (merged, took wholesale, or already carried). The three-way
    # config verdict on pull compares against this accepted base — never
    # against a bundle the device merely downloaded — so a pull that was
    # refused or never applied can never turn a remote change into a
    # phantom "local-only change".
    config_base_digests: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LocalState:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True, slots=True)
class Status:
    device: str
    backend: str
    local: LocalState
    remote_generation: int | None  # None = unreachable / absent
    remote_device: str
    remote_created_at: float
    remote_ahead: bool
    local_dirty: bool
    dirty_count: int
    devices: list[DeviceInfo]
    error: str = ""
    recovered: str | None = None  # what crash recovery rolled back at the start of this call
    pending_auto_push: bool = False  # a killed/failed auto-push awaits the next session (R9)
    # 0.4.1: the on/off state of the optional sync items, so every surface can
    # show what this device opted into (local data, never remote-controlled).
    opt_ins: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        return d


@dataclass(frozen=True, slots=True)
class Diff:
    added: dict[Scope, list[str]]
    changed: dict[Scope, list[str]]
    removed: dict[Scope, list[str]]
    remote_generation: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "remote_generation": self.remote_generation,
            "added": {k.value: v for k, v in self.added.items()},
            "changed": {k.value: v for k, v in self.changed.items()},
            "removed": {k.value: v for k, v in self.removed.items()},
        }

    def is_empty(self) -> bool:
        groups = (self.added, self.changed, self.removed)
        return not any(any(g.values()) for g in groups)


@dataclass(frozen=True, slots=True)
class PushResult:
    generation: int
    bundle_key: str
    bundle_size: int
    file_count: int
    dry_run: bool
    skipped_reason: str = ""  # "nothing to push" etc.
    files: list[str] = field(default_factory=list)  # bundle relpaths (dry_run and real push)
    notes: list[str] = field(default_factory=list)  # 0.4.1: e.g. session-archive publish/degrade notices
    recovered: str | None = None  # what crash recovery rolled back at the start of this call

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True, slots=True)
class PullResult:
    generation: int
    applied: bool
    plan: Diff
    backup_dir: str = ""
    config_merged: bool = False
    plugins_to_install: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)  # local-vs-remote divergences kept local
    skipped_local: list[str] = field(default_factory=list)  # remote paths local policy refuses
    recovered: str | None = None  # what crash recovery rolled back at the start of this call

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["plan"] = self.plan.to_dict()
        return d


def home_state_dir(home: Path) -> Path:
    return home / STATE_DIRNAME
