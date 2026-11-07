"""Lane r1 adversarial-review PoCs (crypto / archive / signed head / trust model).

Each test encodes the REQUIRED behavior and FAILS against the current code:

1. A storage that serves an OLDER but still validly signed HEAD must never be
   able to make `pull --apply` silently revert the home. Today `Engine.pull`
   has no generation-monotonicity check (push has one at engine.py:376), so a
   rewound git branch, an eventually-consistent S3/R2 read, or a compromised
   remote silently rolls the home back AND poisons local state
   (`last_seen_generation` regresses, so the next honest push is refused).

2. The archive key is the single trust anchor of the whole system; a key with
   zero entropy (all-zero / single repeated byte) must be rejected by
   `decode_key` instead of silently accepted.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fakes.fake_backend import FakeBackend

from roam.engine import Engine
from roam.model import RoamSettings, decode_key

KEY = bytes(range(32))
VERSION = "0.22.5"
SOUL_V1 = b"soul of generation 1"
SOUL_V2 = b"soul of generation 2 - the newest home"


def make_home(path: Path) -> Path:
    home = path
    (home / "skills/roam").mkdir(parents=True, exist_ok=True)
    (home / "SOUL.md").write_bytes(SOUL_V1)
    (home / "skills/roam/SKILL.md").write_bytes(b"# shared skill")
    return home


def make_engine(home: Path, backend: FakeBackend) -> Engine:
    settings = RoamSettings(backend="fake", device_name="dev-a", archive_key=KEY)
    # real pipeline (real walk/scope/archive/restore) + real signed-head crypto
    return Engine(home, settings, backend, VERSION)


def test_pull_must_not_apply_an_older_validly_signed_head(tmp_path):
    backend = FakeBackend()
    home = make_home(tmp_path / "home")
    engine = make_engine(home, backend)

    engine.init(create_remote=True)
    engine.push()  # generation 1 (SOUL.md = v1)
    older_head = backend.get_head()  # validly signed generation-1 head + token
    assert engine.load_state().last_seen_generation == 1

    (home / "SOUL.md").write_bytes(SOUL_V2)
    engine.push()  # generation 2 (SOUL.md = v2)
    assert engine.load_state().last_seen_generation == 2

    # Malicious / buggy / stale storage serves the older head again: the head
    # still verifies (same key), and its bundle is still in the store.
    backend._head = older_head

    try:
        engine.pull(apply=True)  # must refuse the rollback instead of applying it
    except Exception:
        pass  # refusing the rolled-back generation is the correct behavior

    assert (home / "SOUL.md").read_bytes() == SOUL_V2, (
        "pull applied generation 1 and silently reverted the home"
    )
    state = engine.load_state()
    assert state.last_seen_generation == 2, "pull regressed last_seen_generation"
    assert state.last_pulled_generation == 0, "pull recorded the rolled-back generation"


def test_decode_key_must_reject_zero_entropy_keys():
    for weak in ("00" * 32, "ff" * 32, "41" * 32):
        with pytest.raises(ValueError):
            decode_key(weak)
