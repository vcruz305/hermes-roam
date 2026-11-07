"""0.4 list_blobs: every real backend lists exactly its bundle keys."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from roam.backends.git import GitBackend
from roam.backends.local import LocalBackend
from roam.errors import BackendError, ConfigError

BRANCH = "roam"
KEYS = [
    "bundles/000001-dev-a-aaaaaaaaaaaa.roam",
    "bundles/000002-dev-a-bbbbbbbbbbbb.roam",
    "bundles/000003-dev-b-cccccccccccc.roam",
]


def _seed_blobs(backend, tmp_path: Path) -> None:
    for index, key in enumerate(KEYS):
        source = tmp_path / f"blob-{index}.bin"
        source.write_bytes(f"payload-{index}".encode())
        backend.put_blob(key, source)


def test_local_lists_only_bundle_keys(tmp_path: Path) -> None:
    backend = LocalBackend(tmp_path / "store")
    _seed_blobs(backend, tmp_path)
    head = tmp_path / "head.json"
    head.write_bytes(b"{}")
    backend.put_head(b"{}", None)
    (tmp_path / "store" / "unrelated.txt").write_bytes(b"x")
    assert backend.list_blobs("bundles/") == KEYS
    assert backend.list_blobs("nothing/") == []
    with pytest.raises(ConfigError):
        backend.list_blobs("../bundles/")
    with pytest.raises(ConfigError):
        backend.list_blobs("")


def test_local_listing_never_follows_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sneaky.roam").write_bytes(b"x")
    backend = LocalBackend(tmp_path / "store")
    _seed_blobs(backend, tmp_path)
    (tmp_path / "store" / "bundles" / "escape").symlink_to(outside)
    assert backend.list_blobs("bundles/") == KEYS


def test_git_lists_only_bundle_keys(tmp_path: Path) -> None:
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True
    )
    backend = GitBackend(str(bare), BRANCH, tmp_path / "mirror")
    _seed_blobs(backend, tmp_path)
    backend.put_head(b"{}", None)
    assert backend.list_blobs("bundles/") == KEYS
    assert backend.list_blobs("nothing/") == []
    with pytest.raises((ConfigError, BackendError)):
        backend.list_blobs("../bundles/")
    with pytest.raises(ConfigError):
        backend.list_blobs("")


def test_git_listing_on_an_empty_remote_is_empty(tmp_path: Path) -> None:
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True
    )
    backend = GitBackend(str(bare), BRANCH, tmp_path / "mirror")
    assert backend.list_blobs("bundles/") == []


def test_s3_lists_only_bundle_keys(tmp_path: Path) -> None:
    boto3 = pytest.importorskip("boto3")
    pytest.importorskip("moto")
    from moto import mock_aws

    with mock_aws():
        admin = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        admin.create_bucket(Bucket="roam-test-bucket")
        from roam.backends.s3 import S3Backend

        backend = S3Backend(
            bucket="roam-test-bucket",
            prefix="hermes-roam",
            region="us-east-1",
            access_key_id="testing",
            secret_access_key="testing",
        )
        _seed_blobs(backend, tmp_path)
        assert backend.list_blobs("bundles/") == KEYS
        assert backend.list_blobs("nothing/") == []
        with pytest.raises(ConfigError):
            backend.list_blobs("../bundles/")
        with pytest.raises(ConfigError):
            backend.list_blobs("")
