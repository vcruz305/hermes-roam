"""Model tests: key decoding (weak keys rejected) and result-dataclass contracts."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from roam.errors import ConfigError, WeakArchiveKey
from roam.model import PullResult, PushResult, RoamSettings, Status, decode_key

REAL_KEY = bytes(range(32))


def test_decode_key_accepts_real_keys_in_both_encodings() -> None:
    assert decode_key(REAL_KEY.hex()) == REAL_KEY
    import base64

    assert decode_key(base64.b64encode(REAL_KEY).decode()) == REAL_KEY


def test_decode_key_rejects_wrong_lengths() -> None:
    with pytest.raises(ValueError):
        decode_key("a" * 63)


def test_decode_key_rejects_all_same_byte_keys_as_a_config_error() -> None:
    for weak in ("00" * 32, "ff" * 32, "41" * 32):
        with pytest.raises(ValueError):
            decode_key(weak)
        with pytest.raises(ConfigError):
            decode_key(weak)
        with pytest.raises(WeakArchiveKey):
            decode_key(weak)
        with pytest.raises(WeakArchiveKey, match="weak key"):
            decode_key(weak)


def test_roam_settings_gains_allow_agent_apply_default_false() -> None:
    assert RoamSettings().allow_agent_apply is False


def test_roam_settings_gains_the_round3_deadline_and_retention_fields() -> None:
    """R8/R5: one operation budget, a git transfer cap, a backup window."""
    defaults = RoamSettings()
    assert defaults.operation_timeout == 900.0
    assert defaults.git_transfer_timeout == 600.0
    assert defaults.backup_keep == 5


def test_status_gains_pending_auto_push_default_false() -> None:
    """R9: a killed auto-push must be visible; the engine defaults it to off."""
    status = Status(
        device="d",
        backend="fake",
        local=None,
        remote_generation=None,
        remote_device="",
        remote_created_at=0.0,
        remote_ahead=False,
        local_dirty=False,
        dirty_count=0,
        devices=[],
        error="",
        recovered=None,
    )
    assert status.pending_auto_push is False


def test_result_objects_carry_the_review_round_2_fields() -> None:
    pull = PullResult(
        generation=1, applied=False, plan=None, conflicts=["SOUL.md"], skipped_local=["a"]
    )
    assert pull.conflicts == ["SOUL.md"]
    assert pull.skipped_local == ["a"]
    assert pull.recovered is None
    push = PushResult(
        generation=1, bundle_key="", bundle_size=0, file_count=0, dry_run=False, files=["a"]
    )
    assert push.files == ["a"]
    assert push.recovered is None
    status = Status(
        device="d",
        backend="fake",
        local=None,
        remote_generation=None,
        remote_device="",
        remote_created_at=0.0,
        remote_ahead=False,
        local_dirty=False,
        dirty_count=0,
        devices=[],
        error="",
        recovered=None,
    )
    assert status.recovered is None
