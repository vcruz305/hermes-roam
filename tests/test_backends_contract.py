"""One contract suite over every backend: head CAS, blob immutability, limits."""

from __future__ import annotations

import dataclasses
import subprocess
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from roam.backends import GitBackend, LocalBackend, S3Backend, open_backend
from roam.errors import BackendError, BlobExists, ConfigError, HeadConflict
from roam.model import RoamSettings

HEAD_DATA = b'{"v":1,"generation":1}'
BLOB_KEY = "bundles/1-dev-abc.roam"
BLOB_BYTES = b"bundle-payload"


@pytest.fixture(params=["local", "s3", "git"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    kind = request.param
    if kind == "local":
        yield LocalBackend(tmp_path / "store")
    elif kind == "s3":
        with mock_aws():
            admin = boto3.client(
                "s3",
                region_name="us-east-1",
                aws_access_key_id="testing",
                aws_secret_access_key="testing",
            )
            admin.create_bucket(Bucket="roam-contract-bucket")
            yield S3Backend(
                bucket="roam-contract-bucket",
                prefix="hermes-roam",
                endpoint_url="",
                region="us-east-1",
                access_key_id="testing",
                secret_access_key="testing",
            )
    else:
        bare = tmp_path / "remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True
        )
        yield GitBackend(str(bare), "roam", tmp_path / "mirror")


def test_absent_head_is_none(backend: Any) -> None:
    assert backend.get_head() is None


def test_head_token_roundtrip_and_conflict(backend: Any) -> None:
    token = backend.put_head(HEAD_DATA, None)
    assert token

    data, current = backend.get_head()
    assert data == HEAD_DATA
    assert current == token

    with pytest.raises(HeadConflict):
        backend.put_head(HEAD_DATA + b"x", None)
    with pytest.raises(HeadConflict):
        backend.put_head(HEAD_DATA + b"x", "stale-token")

    token2 = backend.put_head(HEAD_DATA + b"x", token)
    data, current = backend.get_head()
    assert data == HEAD_DATA + b"x"
    assert current == token2


def test_blob_is_immutable_and_roundtrips(backend: Any, tmp_path: Path) -> None:
    source = tmp_path / "bundle.roam"
    source.write_bytes(BLOB_BYTES)

    backend.put_blob(BLOB_KEY, source)
    assert backend.blob_exists(BLOB_KEY) is True

    dest = tmp_path / "pulled" / "b.roam"
    backend.get_blob(BLOB_KEY, dest, max_bytes=1024)
    assert dest.read_bytes() == BLOB_BYTES

    with pytest.raises(BlobExists):
        backend.put_blob(BLOB_KEY, source)


def test_get_blob_enforces_the_size_cap(backend: Any, tmp_path: Path) -> None:
    source = tmp_path / "bundle.roam"
    source.write_bytes(b"x" * 100)
    backend.put_blob(BLOB_KEY, source)

    dest = tmp_path / "pulled.roam"
    with pytest.raises(BackendError):
        backend.get_blob(BLOB_KEY, dest, max_bytes=99)
    assert not dest.exists()
    assert [p for p in dest.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_invalid_keys_are_rejected(backend: Any, tmp_path: Path) -> None:
    source = tmp_path / "bundle.roam"
    source.write_bytes(b"data")
    for key in ("", "/abs", "../up", "a//b", "a/./b", "..", "a b", "a..b", "x" * 201):
        with pytest.raises(ConfigError):
            backend.put_blob(key, source)
        with pytest.raises(ConfigError):
            backend.get_blob(key, tmp_path / "d", max_bytes=10)
        with pytest.raises(ConfigError):
            backend.blob_exists(key)


def test_ping_is_a_single_line(backend: Any) -> None:
    summary = backend.ping()
    assert summary
    assert summary.count("\n") == 0


def test_missing_blob_is_an_error(backend: Any, tmp_path: Path) -> None:
    assert backend.blob_exists("bundles/missing.roam") is False
    with pytest.raises(BackendError):
        backend.get_blob("bundles/missing.roam", tmp_path / "d", max_bytes=10)


# ----------------------------------------------------------------- open_backend


def settings(**overrides: str) -> RoamSettings:
    return dataclasses.replace(RoamSettings(), **overrides)


def test_open_backend_local(tmp_path: Path) -> None:
    store = tmp_path / "store"
    backend = open_backend(settings(backend="local", local_dir=str(store)), home=tmp_path)
    assert isinstance(backend, LocalBackend)
    assert backend.root == store
    with pytest.raises(ConfigError, match="local_dir"):
        open_backend(settings(backend="local"), home=tmp_path)


def test_open_backend_s3_names_every_missing_piece(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="s3_bucket"):
        open_backend(settings(backend="s3"), home=tmp_path)
    with pytest.raises(ConfigError, match="ROAM_S3_ACCESS_KEY_ID"):
        open_backend(
            settings(backend="s3", s3_bucket="b", s3_secret_access_key="s"), home=tmp_path
        )
    with pytest.raises(ConfigError, match="ROAM_S3_SECRET_ACCESS_KEY"):
        open_backend(
            settings(backend="s3", s3_bucket="b", s3_access_key_id="a"), home=tmp_path
        )
    backend = open_backend(
        settings(
            backend="s3",
            s3_bucket="b",
            s3_prefix="hermes-roam",
            s3_access_key_id="a",
            s3_secret_access_key="s",
        ),
        home=tmp_path,
    )
    assert isinstance(backend, S3Backend)
    assert backend.bucket == "b"
    assert backend.prefix == "hermes-roam"


def test_open_backend_git_uses_the_home_mirror(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="git_remote"):
        open_backend(settings(backend="git"), home=tmp_path)
    backend = open_backend(
        settings(backend="git", git_remote="git@example.invalid:u/r.git"),
        home=tmp_path,
    )
    assert isinstance(backend, GitBackend)
    assert backend.branch == "roam"
    assert backend.mirror_dir == tmp_path / "roam" / "git-mirror"
    assert backend.transfer_timeout == 600.0  # the ruled default (R8)


def test_open_backend_git_reads_the_configured_transfer_timeout(tmp_path: Path) -> None:
    """R8 pre-agreed interface: settings.git_transfer_timeout flows into the
    backend (a stand-in settings object works before g1's field lands)."""
    stand_in = types.SimpleNamespace(
        backend="git",
        git_remote=str(tmp_path / "origin.git"),
        git_branch="roam",
        git_transfer_timeout=123.0,
    )
    backend = open_backend(stand_in, home=tmp_path)
    assert isinstance(backend, GitBackend)
    assert backend.transfer_timeout == 123.0


def test_open_backend_rejects_unknown_backends(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="carrier-pigeon"):
        open_backend(settings(backend="carrier-pigeon"), home=tmp_path)
