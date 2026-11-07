"""Tests for the local directory backend."""

from __future__ import annotations

import errno
import hashlib
import os
import sys
from pathlib import Path

import pytest

from roam.backends import validate_key
from roam.backends.local import LocalBackend
from roam.errors import BackendError, BlobExists, ConfigError, HeadConflict


def make_backend(tmp_path: Path) -> LocalBackend:
    return LocalBackend(tmp_path / "store")


def test_absent_head_is_none(tmp_path: Path) -> None:
    assert make_backend(tmp_path).get_head() is None


def test_head_roundtrip_and_cas(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    token = backend.put_head(b'{"v":1}', None)
    assert token == hashlib.sha256(b'{"v":1}').hexdigest()

    data, current = backend.get_head()
    assert data == b'{"v":1}'
    assert current == token

    with pytest.raises(HeadConflict):
        backend.put_head(b'{"v":2}', None)  # must-not-exist fails now
    with pytest.raises(HeadConflict):
        backend.put_head(b'{"v":2}', "0" * 64)  # stale token

    token2 = backend.put_head(b'{"v":2}', token)
    assert token2 == hashlib.sha256(b'{"v":2}').hexdigest()
    data, current = backend.get_head()
    assert data == b'{"v":2}'
    assert current == token2


def test_blob_roundtrip_is_immutable(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    source = tmp_path / "bundle.roam"
    source.write_bytes(b"bundle-bytes")

    backend.put_blob("bundles/1-dev-abc.roam", source)
    assert backend.blob_exists("bundles/1-dev-abc.roam")

    dest = tmp_path / "pulled" / "1-dev-abc.roam"
    backend.get_blob("bundles/1-dev-abc.roam", dest, max_bytes=1024)
    assert dest.read_bytes() == b"bundle-bytes"

    with pytest.raises(BlobExists):
        backend.put_blob("bundles/1-dev-abc.roam", source)


def test_missing_blob_is_an_error_and_leaves_nothing(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    assert backend.blob_exists("bundles/missing.roam") is False
    dest = tmp_path / "dest"
    with pytest.raises(BackendError):
        backend.get_blob("bundles/missing.roam", dest, max_bytes=100)
    assert not dest.exists()


def test_get_blob_enforces_size_cap(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    source = tmp_path / "bundle.roam"
    source.write_bytes(b"x" * 100)
    backend.put_blob("bundles/1-dev-abc.roam", source)

    dest = tmp_path / "pulled.roam"
    with pytest.raises(BackendError):
        backend.get_blob("bundles/1-dev-abc.roam", dest, max_bytes=99)
    assert not dest.exists()
    # no temp litter anywhere
    assert [p.name for p in dest.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_invalid_keys_are_rejected(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    source = tmp_path / "bundle.roam"
    source.write_bytes(b"data")
    bad_keys = ["", "/abs", "../up", "a/../b", "a//b", "a/./b", ".", "..", "a b", "a\nb", "a..b",
                "x" * 201]
    for key in bad_keys:
        with pytest.raises(ConfigError):
            validate_key(key)
        with pytest.raises(ConfigError):
            backend.put_blob(key, source)
        with pytest.raises(ConfigError):
            backend.get_blob(key, tmp_path / "d", max_bytes=10)
        with pytest.raises(ConfigError):
            backend.blob_exists(key)


def test_ping_names_root_and_free_space(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    summary = backend.ping()
    assert str(backend.root) in summary
    assert "MiB free" in summary


def test_head_file_is_replaced_atomically_no_litter(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    backend.put_head(b"one", None)
    token = backend.get_head()[1]
    backend.put_head(b"two", token)
    names = [p.name for p in backend.root.iterdir()]
    assert sorted(names) == [".locks", "HEAD"]


# ------------------------------------------------- portability hardening

def test_construction_without_fcntl_is_a_named_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows has no fcntl: the local backend must refuse by name there,
    while roam.backends (and the s3/git backends) still import."""
    monkeypatch.setitem(sys.modules, "fcntl", None)
    with pytest.raises(ConfigError, match="POSIX"):
        LocalBackend(tmp_path / "store")


def test_put_blob_falls_back_when_hard_links_are_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = make_backend(tmp_path)
    source = tmp_path / "bundle.roam"
    source.write_bytes(b"bundle-payload")

    def no_link(*args: object, **kwargs: object) -> None:
        raise PermissionError(errno.EPERM, "link() not supported on this filesystem")

    monkeypatch.setattr(os, "link", no_link)
    backend.put_blob("bundles/1-dev-abc.roam", source)  # O_EXCL copy fallback

    assert backend.blob_exists("bundles/1-dev-abc.roam")
    dest = tmp_path / "pulled" / "b.roam"
    backend.get_blob("bundles/1-dev-abc.roam", dest, max_bytes=1024)
    assert dest.read_bytes() == b"bundle-payload"

    # create-once semantics survive the fallback
    with pytest.raises(BlobExists):
        backend.put_blob("bundles/1-dev-abc.roam", source)
