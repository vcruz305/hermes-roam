"""Head serialization, signing, and verification (ported from vigil ``state_head``)."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import replace

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .errors import VerifyError
from .model import Head, canonical_json

HEAD_INFO = b"roam-head"
KEY_LEN = 32
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SANITIZE_RE = re.compile(r"[^a-z0-9-]+")
# R6: device names and manifest paths are remote-controlled content; a head
# that carries anything outside these shapes is rejected whole.
_DEVICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
# 0.4: the only shape a signed parent bundle key may have (head chaining);
# empty means generation 0 or a head written before 0.4.
_PARENT_BUNDLE_KEY_RE = re.compile(
    r"^bundles/[0-9]{1,6}-[a-z0-9][a-z0-9-]{0,39}-[0-9a-f]{12}\.roam$"
)
_MAX_PATH_TOTAL = 1024
_MAX_PATH_SEGMENT = 255


def is_valid_device_name(name: object) -> bool:
    """Whether name fits the ruled device-name shape ^[a-z0-9][a-z0-9-]{0,39}$."""
    return isinstance(name, str) and bool(_DEVICE_NAME_RE.fullmatch(name))


def _reject(detail: str, value: object) -> VerifyError:
    from .textsafe import untrusted

    return VerifyError(f"{detail} ({untrusted(value, limit=60)})")


def _validate_manifest_path(relpath: object) -> None:
    """Reject any manifest path that is not a safe POSIX relative path."""
    if not isinstance(relpath, str) or not relpath:
        raise _reject("the remote head lists a manifest path that is empty or not a string", relpath)
    if len(relpath) > _MAX_PATH_TOTAL:
        raise _reject("the remote head lists a manifest path longer than 1024 bytes", relpath[:40])
    if relpath.startswith("/"):
        raise _reject("the remote head lists an absolute manifest path", relpath[:40])
    if "\\" in relpath:
        raise _reject("the remote head lists a manifest path containing a backslash", relpath[:40])
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in relpath):
        raise _reject("the remote head lists a manifest path containing control characters", relpath[:40])
    for segment in relpath.split("/"):
        if len(segment) > _MAX_PATH_SEGMENT:
            raise _reject(
                "the remote head lists a manifest path with a segment longer than 255 bytes",
                relpath[:40],
            )
        if segment in ("", ".", ".."):
            raise _reject("the remote head lists a manifest path with an unsafe segment", relpath[:40])


def _validate_head_content(head: Head) -> None:
    """R6: reject the whole head when its device names or manifest paths are
    outside the ruled shapes; such content is attacker-controlled and must
    never reach a user or the model verbatim."""
    if not is_valid_device_name(head.device):
        raise _reject("the remote head names a device that is not a valid device name", head.device)
    devices = head.devices if isinstance(head.devices, dict) else {}
    for name in devices:
        if not is_valid_device_name(name):
            raise _reject("the remote head roster names a device that is not a valid device name", name)
    files = head.files if isinstance(head.files, dict) else {}
    for relpath in files:
        _validate_manifest_path(relpath)
    parent_bundle_key = getattr(head, "parent_bundle_key", "")
    if parent_bundle_key and not _PARENT_BUNDLE_KEY_RE.fullmatch(parent_bundle_key):
        raise _reject(
            "the remote head names a parent bundle key with an invalid shape",
            parent_bundle_key[:60],
        )


def derive_head_key(archive_key: bytes) -> bytes:
    """HKDF-SHA256 subkey used only for head signatures (info=b"roam-head", 32 bytes)."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_LEN,
        salt=None,
        info=HEAD_INFO,
    ).derive(archive_key)


def sign_head(head: Head, archive_key: bytes) -> Head:
    """Return a copy of ``head`` carrying the hex HMAC-SHA256 over its unsigned canonical JSON."""
    if len(archive_key) != 32:
        raise VerifyError("the archive key must be exactly 32 bytes to sign a head")
    key = derive_head_key(archive_key)
    sig = hmac.new(key, canonical_json(head.unsigned_dict()), hashlib.sha256).hexdigest()
    return replace(head, sig=sig)


def verify_head(data: bytes, archive_key: bytes) -> Head:
    """Parse and authenticate head bytes; raise VerifyError on any problem."""
    if len(archive_key) != 32:
        raise VerifyError("the archive key must be exactly 32 bytes to verify a head")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerifyError("the remote head is not valid JSON") from exc
    if not isinstance(value, dict):
        raise VerifyError("the remote head must be a JSON object")
    sig = value.get("sig")
    if not isinstance(sig, str) or not _HEX64_RE.fullmatch(sig):
        raise VerifyError("the remote head signature is missing or malformed")
    if value.get("v") != 1:
        raise VerifyError(f"unsupported remote head version: {value.get('v')!r}")
    unsigned = {k: v for k, v in value.items() if k != "sig"}
    key = derive_head_key(archive_key)
    expected = hmac.new(key, canonical_json(unsigned), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise VerifyError(
            "remote head signature verification failed (wrong ROAM_ARCHIVE_KEY or tampered head)"
        )
    try:
        parsed = Head.from_dict(value)
    except (TypeError, ValueError) as exc:
        raise VerifyError(f"the remote head has invalid fields: {exc}") from exc
    _validate_head_content(parsed)
    return parsed


def encode_head(head: Head) -> bytes:
    """Canonical JSON bytes of a signed head (the put_head payload)."""
    return canonical_json(head.to_dict())


def bundle_key_for(generation: int, device: str, plain_sha256: str) -> str:
    """Immutable bundle object key: bundles/<6-digit generation>-<sanitized device>-<sha12>.roam."""
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise ValueError("generation must be a non-negative integer")
    if not isinstance(plain_sha256, str) or not _HEX64_RE.fullmatch(plain_sha256):
        raise ValueError("plain_sha256 must be 64 lowercase hexadecimal characters")
    sanitized = _SANITIZE_RE.sub("-", device.lower()).strip("-")
    if not sanitized:
        sanitized = "device"
    return f"bundles/{generation:06d}-{sanitized}-{plain_sha256[:12]}.roam"
