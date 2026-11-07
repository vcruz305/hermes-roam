"""Storage backends for Hermes Roam: local directory, S3/R2, and git."""

from __future__ import annotations

import re
from pathlib import Path

from ..errors import ConfigError
from ..model import SECRET_S3_ACCESS_KEY_ID, SECRET_S3_SECRET_ACCESS_KEY, RoamSettings

__all__ = [
    "Backend",
    "GitBackend",
    "LocalBackend",
    "S3Backend",
    "open_backend",
    "validate_key",
]

_KEY_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_MAX_KEY_CHARACTERS = 200


def validate_key(key: str) -> str:
    """Return ``key`` unchanged when it is a safe relative POSIX object key.

    Rules: non-empty, at most 200 characters, only ``[A-Za-z0-9._/-]``, no
    leading ``/``, no empty, ``.`` or ``..`` segments, and no ``..`` substring
    at all (conservative). Raise ``ConfigError`` otherwise.
    """

    if not isinstance(key, str):
        raise ConfigError(f"invalid object key {key!r}: keys must be strings")
    if not key or len(key) > _MAX_KEY_CHARACTERS:
        raise ConfigError(
            f"invalid object key {key!r}: keys are 1..{_MAX_KEY_CHARACTERS} characters"
        )
    if key.startswith("/"):
        raise ConfigError(f"invalid object key {key!r}: keys are relative, with no leading slash")
    if not _KEY_RE.fullmatch(key):
        raise ConfigError(f"invalid object key {key!r}: keys may only contain [A-Za-z0-9._/-]")
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ConfigError(f"invalid object key {key!r}: no empty or dot path segments")
    if any(".." in part for part in parts):
        raise ConfigError(f"invalid object key {key!r}: no '..' anywhere in a key")
    return key


def open_backend(settings: RoamSettings, *, home: Path) -> Backend:
    """Build the configured backend. Secrets come from settings, never from disk."""

    if settings.backend == "local":
        if not settings.local_dir:
            raise ConfigError("the local backend needs local_dir: set it in the roam config")
        return LocalBackend(Path(settings.local_dir))
    if settings.backend == "s3":
        if not settings.s3_bucket:
            raise ConfigError("the s3 backend needs s3_bucket: set it in the roam config")
        if not settings.s3_access_key_id:
            raise ConfigError(f"the s3 backend needs the {SECRET_S3_ACCESS_KEY_ID} secret")
        if not settings.s3_secret_access_key:
            raise ConfigError(f"the s3 backend needs the {SECRET_S3_SECRET_ACCESS_KEY} secret")
        return S3Backend(
            settings.s3_bucket,
            settings.s3_prefix,
            endpoint_url=settings.s3_endpoint_url,
            region=settings.s3_region,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
        )
    if settings.backend == "git":
        if not settings.git_remote:
            raise ConfigError(
                "the git backend needs git_remote: set the repository URL in the roam config"
            )
        mirror_dir = home / "roam" / "git-mirror"
        # R8 pre-agreed interface: RoamSettings.git_transfer_timeout (g1).
        # Read tolerantly so the backend still constructs before that field
        # lands; the default matches the ruled default.
        transfer_timeout = float(getattr(settings, "git_transfer_timeout", 0.0) or 600.0)
        return GitBackend(
            settings.git_remote,
            settings.git_branch or "roam",
            mirror_dir,
            transfer_timeout=transfer_timeout,
        )
    raise ConfigError(f"unknown roam backend {settings.backend!r}: use 'local', 's3', or 'git'")


# Backends import validate_key from this package, so they load after it is defined.
from .base import Backend  # noqa: E402
from .git import GitBackend  # noqa: E402
from .local import LocalBackend  # noqa: E402
from .s3 import S3Backend  # noqa: E402
