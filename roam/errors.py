"""Frozen exception hierarchy for Hermes Roam. Every message names the fix."""

from __future__ import annotations


class RoamError(Exception):
    """Base class. ``str(exc)`` is user-facing and must never contain secrets."""


class ConfigError(RoamError):
    """Settings are missing or invalid (names the setting / env var)."""


class BackendError(RoamError):
    """The remote could not be reached or refused the operation."""


class HeadConflict(BackendError):
    """Compare-and-swap on HEAD failed: someone else pushed. Pull first."""


class BlobExists(BackendError):
    """An immutable bundle object already exists under that key."""


class RemoteAhead(RoamError):
    """The remote generation moved past what this device last saw. Pull (or --force) first."""


class VerifyError(RoamError):
    """Signature or digest mismatch: the remote data is not what this fleet's key signed."""


class CarryForwardError(RoamError):
    """A push must copy unseen base entries from roam/last.roam, which is
    missing or unreadable; the device must pull first (R1)."""


class WeakArchiveKey(ConfigError, ValueError):
    """The archive key decodes to 32 bytes but has zero entropy (all bytes equal).

    Subclasses both ConfigError (the ruled refusal type) and ValueError (the
    documented decode_key contract), so every caller — including the settings
    resolver that wraps ValueError into ConfigError — handles it as a config
    error while the key never becomes a silent trust anchor.
    """


class ArchiveError(RoamError):
    """Bundle build/open refused (unsafe member, size limit, bad envelope)."""


class RestoreError(RoamError):
    """Journaled install failed; rollback status is in the message."""


class VersionMismatch(RoamError):
    """The remote home was pushed by a newer Hermes than this one."""
