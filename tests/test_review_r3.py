"""Adversarial review lane r3 — failing PoC tests for git / S3-R2 / local backend bugs.

Each test asserts the behaviour the plugin contract (errors.py, README.md) already
promises. They fail on the code under review; a fix lane makes them pass.

1. push stores bundles larger than max_bundle_bytes -> every pull (on every
   device, including the pusher) is refused by get_blob forever: the fleet is
   bricked with no rollback path.
2. git ssh transport is not fail-fast: GIT_TERMINAL_PROMPT=0 does not govern
   ssh, so an unknown host key / passphrase prompt stalls each git call until
   the hard timeout (put_blob retries it 3x), against README's "fails fast
   instead of hanging" and catalog rule 12 (nothing waits on a human).
3. _redact leaks credential fragments when a URL-embedded password contains
   '@' or '/', into BackendError text (user-facing, engine Status.error).
4. git missing -> raw FileNotFoundError ("unexpected failure"), not a named error.
5. README claims S3/git backends are unaffected on Windows, but importing
   roam.backends crashes there (top-level `import fcntl` in local.py).
6. S3 endpoint without scheme -> raw botocore ValueError, not a named error.
7. local put_blob on a filesystem without hardlinks -> raw OSError, no
   fallback and no named error.
"""

from __future__ import annotations

import builtins
import errno
import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from roam.backends.git import GitBackend, _git_env, _redact
from roam.backends.local import LocalBackend
from roam.backends.s3 import S3Backend
from roam.engine import Engine
from roam.errors import BackendError, ConfigError, RoamError
from roam.model import RoamSettings

BRANCH = "roam"


# ---------------------------------------------------------------- 1. size cap


def _engine(tmp_path: Path, home_name: str, device: str, cap: int) -> tuple[Path, Engine]:
    home = tmp_path / home_name
    (home / "memories").mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_bytes(b"model:\n  provider: p\n")
    store = tmp_path / "store"
    settings = RoamSettings(
        backend="local",
        device_name=device,
        archive_key=bytes(range(32)),
        local_dir=str(store),
        max_bundle_bytes=cap,
    )
    return home, Engine(home, settings, LocalBackend(store), "9.9.9")


def test_push_refuses_bundles_over_max_bundle_bytes(tmp_path: Path) -> None:
    """A bundle over the cap must never be stored: get_blob refuses it forever,
    so a successful push bricks every pull on every device (no rollback)."""
    home, engine = _engine(tmp_path, "home-a", "dev-a", cap=4096)
    (home / "memories" / "big.bin").write_bytes(os.urandom(200 * 1024))
    engine.init(create_remote=True)

    with pytest.raises(RoamError):
        engine.push()

    # fail closed: nothing over the cap may be left on the remote
    bundles = tmp_path / "store" / "bundles"
    assert not bundles.exists() or list(bundles.iterdir()) == []


# ------------------------------------------------------- 2. ssh fail-fast


def test_git_env_disables_interactive_ssh_prompts() -> None:
    """GIT_TERMINAL_PROMPT=0 only covers git's own prompts; the ssh transport
    must be pinned to BatchMode so it fails fast instead of stalling."""
    env = _git_env()
    assert "BatchMode=yes" in env.get("GIT_SSH_COMMAND", "")


def test_git_never_invokes_a_prompting_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove git really defers to an ssh binary for ssh:// remotes: whatever
    ssh runs must carry non-interactive options (BatchMode)."""
    record = tmp_path / "ssh-args.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" > " + str(record) + "\nexit 1\n"
    )
    fake_ssh.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])

    backend = GitBackend("ssh://git@127.0.0.1/repo.git", BRANCH, tmp_path / "mirror")
    with pytest.raises(BackendError):  # the fake ssh rejects; git must fail fast
        backend.ping()

    assert record.is_file(), "git must route ssh through the ssh binary"
    assert "BatchMode" in record.read_text()


# ------------------------------------------------------- 3. credential leak


def test_redact_strips_userinfo_when_password_has_at() -> None:
    line = "fatal: unable to access 'https://deploy:p@ssw0rd@evil.example/repo/': failed"
    redacted = _redact(line)
    assert "ssw0rd" not in redacted  # password tail currently survives
    assert "deploy" not in redacted


def test_redact_strips_userinfo_when_password_has_slash() -> None:
    line = "fatal: unable to access 'https://deploy:se/cr@evil.example/repo/': failed"
    redacted = _redact(line)
    assert "se/cr" not in redacted  # the whole credential currently survives
    assert "deploy" not in redacted


def test_git_failure_message_has_no_url_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RoamError str(exc) is user-facing and 'must never contain secrets'."""
    backend = GitBackend("https://evil.example/repo.git", BRANCH, tmp_path / "mirror")
    err = (
        "fatal: unable to access 'https://deploy:p@ssw0rd@evil.example/repo/': "
        "The requested URL returned error: 403"
    )
    monkeypatch.setattr(
        GitBackend, "_run", lambda self, argv, **kw: (128, b"", err)
    )
    with pytest.raises(BackendError) as excinfo:
        backend._fetch()
    assert "ssw0rd" not in str(excinfo.value)


# ------------------------------------------------------- 4. git missing


def test_missing_git_binary_is_a_named_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    with pytest.raises((ConfigError, BackendError), match="git"):
        GitBackend("https://example.invalid/x.git", BRANCH, tmp_path / "mirror")


# ------------------------------------------------------- 5. non-POSIX import


def test_backends_import_without_posix_fcntl() -> None:
    """README: 'Windows is not supported by the local backend ... the S3 and
    git backends are unaffected' — importing roam.backends must not crash on
    platforms without fcntl, or no backend works there at all."""
    real_import = builtins.__import__

    def no_fcntl(name: str, *args: object, **kwargs: object):
        if name == "fcntl":
            raise ImportError("No module named 'fcntl' (simulated non-POSIX platform)")
        return real_import(name, *args, **kwargs)

    def purge() -> None:
        for mod in [
            m
            for m in list(sys.modules)
            if m == "roam" or m.startswith("roam.")
        ]:
            del sys.modules[mod]

    saved = {
        m: mod
        for m, mod in list(sys.modules.items())
        if m == "roam" or m.startswith("roam.")
    }
    assert saved, "expected roam modules to have been loaded before"
    purge()
    builtins.__import__ = no_fcntl
    try:
        importlib.import_module("roam.backends")
    except ImportError as exc:
        pytest.fail(f"roam.backends must import where fcntl is missing: {exc}")
    finally:
        builtins.__import__ = real_import
        purge()
        # restore the ORIGINAL module objects: leaving the re-imported ones in
        # sys.modules would break module identity for every later test (the
        # engine would catch exceptions from a different errors.BackendError).
        sys.modules.update(saved)


# ------------------------------------------------------- 6. endpoint scheme


def test_s3_endpoint_without_scheme_is_a_named_error() -> None:
    backend = S3Backend(
        bucket="b",
        prefix="p",
        endpoint_url="example.invalid",
        region="auto",
        access_key_id="a",
        secret_access_key="s",
    )
    with pytest.raises((ConfigError, BackendError)):
        backend.ping()


# ------------------------------------------------------- 7. linkless filesystem


def test_local_put_blob_on_a_linkless_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LocalBackend(tmp_path / "store")
    source = tmp_path / "bundle.roam"
    source.write_bytes(b"payload")

    def no_link(*args: object, **kwargs: object) -> None:
        raise PermissionError(errno.EPERM, "link() not supported on this filesystem")

    monkeypatch.setattr(os, "link", no_link)
    try:
        backend.put_blob("bundles/1-dev-abc.roam", source)
    except BackendError:
        pass  # a named refusal is acceptable; a raw OSError is not
    else:
        # a copy fallback is equally fine — the blob must then be retrievable
        assert backend.blob_exists("bundles/1-dev-abc.roam")
