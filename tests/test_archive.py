"""Tests for roam.archive: bundle build/open round trip and every rejection."""

from __future__ import annotations

import hashlib
import stat
import struct
import zipfile
from pathlib import Path

import pytest

from roam import archive
from roam.errors import ArchiveError, VerifyError
from roam.model import FileEntry, Scope, canonical_json

KEY = b"k" * 32
LIMITS = archive.ArchiveLimits(
    max_members=1000, max_member_bytes=1024 * 1024, max_total_bytes=4 * 1024 * 1024
)
HEADER = {"v": 1, "device": "dev-a", "generation": 3, "created_at": 123.0, "key_id": "k1"}

FILES = {
    "SOUL.md": b"soul bytes",
    "config.yaml": b"on-disk config",
    "memories/m.md": b"memory content",
    "skills/x/run.sh": b"#!/bin/sh\necho run\n",
}
EXTRA = {
    "config.yaml": b"host-stripped config",
    "roam/installed-plugins.json": b'{"hermes-foo": {"version": "1.0", "sha": ""}}',
}


def make_home(root: Path) -> Path:
    home = root / "home"
    home.mkdir(parents=True)
    for relpath, content in FILES.items():
        target = home / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (home / "skills/x/run.sh").chmod(0o755)
    return home


def make_entries() -> list[FileEntry]:
    entries = []
    for relpath, content in FILES.items():
        mode = 0o755 if relpath == "skills/x/run.sh" else 0o644
        entries.append(
            FileEntry(
                relpath,
                hashlib.sha256(content).hexdigest(),
                len(content),
                mode,
                Scope.SHARED,
            )
        )
    return sorted(entries, key=lambda entry: entry.relpath)


def build(tmp_path: Path) -> tuple[Path, archive.BundleInfo]:
    out = tmp_path / "bundle.roam"
    info = archive.build_bundle(
        make_home(tmp_path),
        make_entries(),
        out,
        key=KEY,
        header=dict(HEADER),
        extra_files=dict(EXTRA),
    )
    return out, info


def craft_bundle(tmp_path: Path, members: dict[str, bytes], *, infos: list | None = None) -> Path:
    """Build an arbitrary plaintext zip and seal it with a matching header."""
    plain = tmp_path / "crafted.zip"
    with zipfile.ZipFile(plain, "w", zipfile.ZIP_DEFLATED) as handle:
        for name, data in members.items():
            handle.writestr(name, data)
        if infos:
            for info in infos:
                handle.writestr(info, b"x")
    size, digest = archive._hash_file(plain)
    header = {**HEADER, "plain_sha256": digest, "plain_size": size}
    out = tmp_path / "crafted.roam"
    archive._seal_envelope(plain, out, key=KEY, header_bytes=canonical_json(header))
    plain.unlink()
    return out


def patch_declared_size(raw: bytes, name: str, size: int) -> bytes:
    """Rewrite the uncompressed-size field of a member in local and central headers."""
    data = bytearray(raw)
    first = data.find(name.encode())
    struct.pack_into("<I", data, first - 8, size)  # local header
    second = data.find(name.encode(), first + 1)
    struct.pack_into("<I", data, second - 22, size)  # central directory
    return bytes(data)


def test_build_and_open_round_trip_restores_bytes_and_modes(tmp_path: Path) -> None:
    out, info = build(tmp_path)
    assert info.size == out.stat().st_size
    assert info.sha256 == hashlib.sha256(out.read_bytes()).hexdigest()
    # the whole bundle is ciphertext: no plaintext leaks into the file
    assert b"soul bytes" not in out.read_bytes()
    prefix = len(archive.MAGIC) + 4 + len(canonical_json(info.header))
    assert info.plain_size == info.size - prefix - archive.NONCE_SIZE - archive.TAG_SIZE

    dest = tmp_path / "staging"
    manifest = archive.open_bundle(out, key=KEY, dest_dir=dest, limits=LIMITS)
    assert manifest == {entry.relpath: entry.to_dict() for entry in make_entries()}
    assert (dest / "SOUL.md").read_bytes() == FILES["SOUL.md"]
    assert (dest / "memories/m.md").read_bytes() == FILES["memories/m.md"]
    assert stat.S_IMODE((dest / "skills/x/run.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((dest / "skills").stat().st_mode) == 0o700
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    # extra files override the on-disk member and add new ones
    assert (dest / "config.yaml").read_bytes() == EXTRA["config.yaml"]
    assert (dest / "config.yaml").read_bytes() != FILES["config.yaml"]
    assert (dest / "roam/installed-plugins.json").read_bytes() == EXTRA["roam/installed-plugins.json"]
    assert not list(tmp_path.glob(".roam-open-*.zip"))
    assert not list(tmp_path.glob(".bundle.roam.*.tmp"))


def test_open_bundle_restores_private_modes_and_never_widens(tmp_path: Path) -> None:
    """r2 f8: a 0600 private file must not arrive 0644, and no extraction
    may widen group/other bits beyond what the bundle recorded (cap 0755)."""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    contents = {}
    modes = {
        "memories/private.md": 0o600,
        "memories/group.md": 0o640,
        "skills/x/run.sh": 0o755,
        "skills/x/world.sh": 0o777,
    }
    entries = []
    for relpath, mode in modes.items():
        content = f"content of {relpath}".encode()
        contents[relpath] = content
        target = home / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(mode)
        entries.append(
            FileEntry(
                relpath,
                hashlib.sha256(content).hexdigest(),
                len(content),
                mode,
                Scope.SHARED,
            )
        )
    entries.sort(key=lambda entry: entry.relpath)
    out = tmp_path / "bundle.roam"
    archive.build_bundle(
        home,
        entries,
        out,
        key=KEY,
        header=dict(HEADER),
        extra_files={},
    )
    dest = tmp_path / "staging"
    archive.open_bundle(out, key=KEY, dest_dir=dest, limits=LIMITS)
    got = {relpath: stat.S_IMODE((dest / relpath).stat().st_mode) for relpath in modes}
    assert got == {
        "memories/private.md": 0o600,  # private stays private (was 0644 before)
        "memories/group.md": 0o640,  # group bits never widened
        "skills/x/run.sh": 0o755,
        "skills/x/world.sh": 0o755,  # world bits capped at 0755
    }


def test_read_header_needs_no_key_and_matches_bundle_info(tmp_path: Path) -> None:
    out, info = build(tmp_path)
    assert archive.read_header(out) == info.header
    assert info.header["device"] == "dev-a"
    assert info.header["generation"] == 3
    assert info.plain_sha256 == info.header["plain_sha256"]


def test_wrong_key_is_verify_error_and_writes_nothing(tmp_path: Path) -> None:
    out, _ = build(tmp_path)
    dest = tmp_path / "staging"
    with pytest.raises(VerifyError):
        archive.open_bundle(out, key=b"n" * 32, dest_dir=dest, limits=LIMITS)
    assert not dest.exists()
    assert not list(tmp_path.glob(".roam-open-*.zip"))


@pytest.mark.parametrize("offset", [200, -3, -1])
def test_tampered_ciphertext_or_tag_is_verify_error(tmp_path: Path, offset: int) -> None:
    out, _ = build(tmp_path)
    raw = bytearray(out.read_bytes())
    raw[offset] ^= 1
    out.write_bytes(raw)
    dest = tmp_path / "staging"
    with pytest.raises((VerifyError, ArchiveError)):
        archive.open_bundle(out, key=KEY, dest_dir=dest, limits=LIMITS)
    assert not dest.exists()


def test_tampered_header_is_rejected(tmp_path: Path) -> None:
    out, info = build(tmp_path)
    prefix = len(archive.MAGIC) + 4
    raw = bytearray(out.read_bytes())
    # flip a byte inside the canonical-JSON header region
    raw[prefix + 40] ^= 1
    out.write_bytes(raw)
    dest = tmp_path / "staging"
    with pytest.raises((VerifyError, ArchiveError)):
        archive.open_bundle(out, key=KEY, dest_dir=dest, limits=LIMITS)
    assert not dest.exists()
    assert archive.read_header(out) != info.header


def test_truncated_bundle_is_archive_error(tmp_path: Path) -> None:
    out, _ = build(tmp_path)
    out.write_bytes(out.read_bytes()[:100])
    with pytest.raises(ArchiveError):
        archive.open_bundle(out, key=KEY, dest_dir=tmp_path / "s", limits=LIMITS)


def test_parent_escape_member_rejected_before_writing(tmp_path: Path) -> None:
    out = craft_bundle(tmp_path, {"../escape.txt": b"x"})
    dest = tmp_path / "staging"
    with pytest.raises(ArchiveError):
        archive.open_bundle(out, key=KEY, dest_dir=dest, limits=LIMITS)
    assert not dest.exists()
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.parametrize(
    "name",
    ["/absolute.txt", "C:/windows.txt", "a\\b.txt", "a/./b.txt", "nested/../../x", "a\x01b"],
)
def test_unsafe_member_names_rejected(tmp_path: Path, name: str) -> None:
    out = craft_bundle(tmp_path, {name: b"x"})
    with pytest.raises(ArchiveError):
        archive.open_bundle(out, key=KEY, dest_dir=tmp_path / "s", limits=LIMITS)


def test_zip_bomb_size_lie_rejected_before_writing(tmp_path: Path) -> None:
    plain = tmp_path / "bomb.zip"
    with zipfile.ZipFile(plain, "w", zipfile.ZIP_DEFLATED) as handle:
        handle.writestr("bomb.bin", b"\0" * 200_000)
    # lie about the declared uncompressed size: central directory says 1 byte
    plain.write_bytes(patch_declared_size(plain.read_bytes(), "bomb.bin", 1))
    size, digest = archive._hash_file(plain)
    header = {**HEADER, "plain_sha256": digest, "plain_size": size}
    out = tmp_path / "bomb.roam"
    archive._seal_envelope(plain, out, key=KEY, header_bytes=canonical_json(header))
    plain.unlink()
    dest = tmp_path / "staging"
    with pytest.raises(ArchiveError):
        archive.open_bundle(out, key=KEY, dest_dir=dest, limits=LIMITS)
    assert not dest.exists()


def test_duplicate_member_rejected(tmp_path: Path) -> None:
    plain = tmp_path / "dup.zip"
    with zipfile.ZipFile(plain, "w") as handle:
        handle.writestr("same.txt", b"a")
        handle.writestr("same.txt", b"b")
    size, digest = archive._hash_file(plain)
    header = {**HEADER, "plain_sha256": digest, "plain_size": size}
    out = tmp_path / "dup.roam"
    archive._seal_envelope(plain, out, key=KEY, header_bytes=canonical_json(header))
    plain.unlink()
    with pytest.raises(ArchiveError):
        archive.open_bundle(out, key=KEY, dest_dir=tmp_path / "s", limits=LIMITS)


def test_symlink_member_rejected(tmp_path: Path) -> None:
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    out = craft_bundle(tmp_path, {"safe.txt": b"x"}, infos=[info])
    with pytest.raises(ArchiveError):
        archive.open_bundle(out, key=KEY, dest_dir=tmp_path / "s", limits=LIMITS)


def test_oversize_declared_member_rejected(tmp_path: Path) -> None:
    plain = tmp_path / "big.zip"
    with zipfile.ZipFile(plain, "w") as handle:
        handle.writestr("big.bin", b"x")
    raw = patch_declared_size(plain.read_bytes(), "big.bin", 8 * 1024 * 1024)
    plain.write_bytes(raw)
    size, digest = archive._hash_file(plain)
    header = {**HEADER, "plain_sha256": digest, "plain_size": size}
    out = tmp_path / "big.roam"
    archive._seal_envelope(plain, out, key=KEY, header_bytes=canonical_json(header))
    plain.unlink()
    small = archive.ArchiveLimits(
        max_members=1000, max_member_bytes=1024, max_total_bytes=4 * 1024 * 1024
    )
    with pytest.raises(ArchiveError):
        archive.open_bundle(out, key=KEY, dest_dir=tmp_path / "s", limits=small)


def test_bundle_over_total_limit_rejected_before_decrypt(tmp_path: Path) -> None:
    out = craft_bundle(tmp_path, {"a.txt": b"x" * 5000})
    tiny = archive.ArchiveLimits(max_members=10, max_member_bytes=1024, max_total_bytes=1024)
    dest = tmp_path / "staging"
    with pytest.raises(ArchiveError):
        archive.open_bundle(out, key=KEY, dest_dir=dest, limits=tiny)
    assert not dest.exists()


def test_nonempty_destination_rejected(tmp_path: Path) -> None:
    out, _ = build(tmp_path)
    dest = tmp_path / "staging"
    dest.mkdir()
    (dest / "keep.txt").write_bytes(b"keep")
    with pytest.raises(ArchiveError):
        archive.open_bundle(out, key=KEY, dest_dir=dest, limits=LIMITS)
    assert (dest / "keep.txt").read_bytes() == b"keep"


def test_bundle_without_manifest_rejected(tmp_path: Path) -> None:
    out = craft_bundle(tmp_path, {"a.txt": b"x"})
    dest = tmp_path / "staging"
    with pytest.raises(ArchiveError, match="manifest"):
        archive.open_bundle(out, key=KEY, dest_dir=dest, limits=LIMITS)


def test_bad_key_length_rejected(tmp_path: Path) -> None:
    out, _ = build(tmp_path)
    with pytest.raises(ArchiveError):
        archive.open_bundle(out, key=b"short", dest_dir=tmp_path / "s", limits=LIMITS)
    with pytest.raises(ArchiveError):
        archive.build_bundle(
            make_home(tmp_path / "again"),
            make_entries(),
            tmp_path / "b2.roam",
            key=b"short",
            header={},
            extra_files={},
        )


def test_archive_limits_must_be_positive() -> None:
    with pytest.raises(ArchiveError):
        archive.ArchiveLimits(max_members=0)
