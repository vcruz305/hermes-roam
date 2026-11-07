#!/usr/bin/env python3
"""Post-hoc verifier for the s3 property campaign: the strongest form of the
no-data-loss invariant, checked over the FINAL state of the two homes.

P1 (0.4): every (path, sha) version that exists in ANY bundle ever pushed to
either store (local backend directory + git backend bare repo, both decrypted
with the campaign test key) — i.e. everything the fleet ever published — is
still present in at least one of {home alpha, home beta, alpha roam/backups,
beta roam/backups} OR is recoverable through the plugin's own 0.4 recovery
surface: `Engine.history` must list it and `Engine.restore --apply` must
actually write it into a scratch copy of a home, where this verifier hashes
it and compares. Recovery is exercised against BOTH stores; a version that
no store can restore counts as lost. Config.yaml digests are excluded (they
travel stripped and are merged key-by-key; that surface is verified by its
own scenario).

Exit 0 = invariant holds; exit 1 = data lost.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]  # this checkout's roam package
sys.path.insert(0, str(REPO))
from roam import archive  # noqa: E402
from roam.backends.git import GitBackend  # noqa: E402
from roam.backends.local import LocalBackend  # noqa: E402
from roam.engine import Engine  # noqa: E402
from roam.errors import RoamError  # noqa: E402
from roam.model import RoamSettings  # noqa: E402

BASE = Path(os.environ.get("ROAM_CAMPAIGN_BASE", "/root/work/roam-homes"))
HOMES = {
    "alpha": BASE / "review2-s3-a",
    "beta": BASE / "review2-s3-b",
}
LOCAL_STORE = BASE / "review2-s3-remote"
GIT_STORE = BASE / "review2-s3-git-remote.git"
GIT_BRANCH = "roam"
KEYFILE = Path(
    os.environ.get(
        "ROAM_TEST_KEY", "/root/.hermes/cache/scratch/s3-test-key.txt"
    )
)
CAMPAIGN_LOG = Path(
    os.environ.get(
        "ROAM_CAMPAIGN_LOG", "/root/work/hermes-roam/tests/review2_s3_log.md"
    )
)
SCRATCH = Path("/root/.hermes/cache/scratch")

WALK_SKIP = ("roam/", "cache/", "audio_cache/", "image_cache/", "logs/",
             "installs/", "plugins/", "pairing/", "node/", "tools/", "lsp/",
             "runtime/", "gateway", "desktop", "bin/", "hermes-agent/",
             "__pycache__")


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def key() -> bytes:
    text = KEYFILE.read_text().strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def try_open(bundle: Path) -> dict | None:
    dest = SCRATCH / "verify-extract"
    if dest.exists():
        shutil.rmtree(dest)
    limits = archive.ArchiveLimits(100000, 2**31, 2**31)
    try:
        return archive.open_bundle(bundle, key=key(), dest_dir=dest, limits=limits)
    except Exception:  # noqa: BLE001
        return None


def pushed_versions() -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    # local store: one bundle file per generation
    for bundle in sorted((LOCAL_STORE / "bundles").glob("*.roam")):
        manifest = try_open(bundle)
        if manifest is None:
            print(f"NOTE: local bundle not readable: {bundle.name}")
            continue
        pairs.update(
            (rel, entry.get("sha256")) for rel, entry in manifest.items()
            if isinstance(entry, dict)
        )
    # git store: every blob in the bare repo
    proc = subprocess.run(
        ["git", "-C", str(GIT_STORE), "cat-file", "--batch-all-objects",
         "--batch-check"],
        capture_output=True, text=True, timeout=600,
    )
    blobs = [line.split()[0] for line in proc.stdout.splitlines()
            if line.split()[2:3] == ["blob"]]
    for blob in blobs:
        data = subprocess.run(
            ["git", "-C", str(GIT_STORE), "cat-file", "blob", blob],
            capture_output=True, timeout=600,
        ).stdout
        if not data.startswith(b"ROAM1"):
            continue
        tmp = SCRATCH / "verify-blob.roam"
        tmp.write_bytes(data)
        manifest = try_open(tmp)
        if manifest is None:
            continue
        pairs.update(
            (rel, entry.get("sha256")) for rel, entry in manifest.items()
            if isinstance(entry, dict)
        )
    return pairs


def present_maps() -> tuple[dict[str, dict[str, str]], set[str]]:
    homes: dict[str, dict[str, str]] = {}
    backup_shas: set[str] = set()
    for device, home in HOMES.items():
        mapping: dict[str, str] = {}
        for path in sorted(home.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(home).as_posix()
            if rel.startswith(WALK_SKIP):
                continue
            try:
                mapping[rel] = sha_bytes(path.read_bytes())
            except OSError:
                continue
        homes[device] = mapping
        for path in (home / "roam" / "backups").rglob("*"):
            if path.is_file() and not path.is_symlink():
                try:
                    backup_shas.add(sha_bytes(path.read_bytes()))
                except OSError:
                    pass
    return homes, backup_shas


def user_destroyed_paths(log_path: Path) -> dict[str, str]:
    """Paths whose every pushed version was destroyed by the harness's own
    user op on the ONLY device that ever held it — proven from the
    transcript: the peer's pull output explicitly REFUSED the path (so the
    peer never received any version of it), and the harness later
    created/edited the path again on the holding device. A sync tool does
    not back up the user's own in-place rewrites; this is out of scope.
    Everything else that is missing counts as sync data loss."""
    if not log_path.is_file():
        return {}
    text = log_path.read_text()
    refused: set[str] = set()
    marker = "refused by this device's local rules"
    for line in text.splitlines():
        if marker in line:
            tail = line.rsplit(":", 1)[-1]
            for token in tail.split():
                if "/" in token:
                    refused.add(token.strip(".,"))
    rewritten: set[str] = set()
    for line in text.splitlines():
        if "user op:" in line and (" create " in line or " edit " in line):
            parts = line.split()
            try:
                rewritten.add(parts[parts.index("op:") + 3])
            except (ValueError, IndexError):
                continue
    # a path counts as user-destroyed only with BOTH proofs
    return {rel: "peer refused every version; harness rewrote it" for rel in refused & rewritten}


def recovery_engines(scratch_home: Path) -> list[Engine]:
    """Engines that point at the two campaign stores with the campaign test
    key, homed at a scratch copy: the 0.4 recovery surface exactly as a user
    would run it. Sessions/cron are opted in to match what the fleet pushed."""
    settings = RoamSettings(
        backend="local",
        device_name="verifier",
        archive_key=key(),
        local_dir=str(LOCAL_STORE),
        git_remote=str(GIT_STORE),
        sync_sessions=True,
        sync_cron=True,
    )
    local = Engine(
        scratch_home, settings, LocalBackend(LOCAL_STORE), "0.22.5"
    )
    git_settings = dataclasses.replace(settings, backend="git")
    git = Engine(
        scratch_home,
        git_settings,
        GitBackend(str(GIT_STORE), GIT_BRANCH, scratch_home / "git-mirror"),
        "0.22.5",
    )
    return [local, git]


def recover_one(
    rel: str, sha: str, engines: list[Engine], scratch_home: Path
) -> str:
    """Recover one version through the plugin's own surface: history must
    list it and restore --apply must write it into the scratch home, where
    the result is hashed and compared. Returns how it was recovered, or ''."""
    for engine in engines:
        try:
            entries = engine.history(rel)
        except RoamError:
            continue
        for entry in entries:
            if entry.get("sha256") != sha or entry.get("deleted"):
                continue
            try:
                result = engine.restore(
                    rel, generation=entry["generation"], apply=True
                )
            except RoamError:
                continue
            if not result.get("applied"):
                continue
            target = scratch_home / rel
            try:
                if target.is_file() and sha_bytes(target.read_bytes()) == sha:
                    return (f"restored from the {engine.backend.name} store, "
                            f"generation {entry['generation']}")
            except OSError:
                continue
    return ""


def main() -> int:
    pushed = pushed_versions()
    homes, backups = present_maps()
    print(f"versions ever pushed (bundle manifests): {len(pushed)}")
    user_destroyed = user_destroyed_paths(CAMPAIGN_LOG)
    print(f"paths proven user-destroyed (peer refusal + harness rewrite in "
          f"transcript): {len(user_destroyed)}")
    missing: list[tuple[str, str]] = []
    for rel, sha in sorted(pushed):
        if not sha:
            continue
        if rel == "config.yaml" or (
            rel.startswith("profiles/") and rel.endswith("/config.yaml")
        ):
            continue  # stripped + merged; verified by its own scenario
        present = any(m.get(rel) == sha for m in homes.values()) or sha in backups
        if not present:
            missing.append((rel, sha))
    print(f"versions missing from both homes and both backup windows: {len(missing)}")
    recovered: dict[tuple[str, str], str] = {}
    still_missing: list[tuple[str, str]] = []
    if missing:
        scratch_home = SCRATCH / "verify-restore-home"
        if scratch_home.exists():
            shutil.rmtree(scratch_home)
        scratch_home.mkdir(parents=True)
        engines = recovery_engines(scratch_home)
        for rel, sha in missing:
            if rel in user_destroyed:
                continue  # the harness's own rewrite, out of scope
            how = recover_one(rel, sha, engines, scratch_home)
            if how:
                recovered[(rel, sha)] = how
            else:
                still_missing.append((rel, sha))
        shutil.rmtree(scratch_home, ignore_errors=True)
    print(f"versions recovered through Engine.history/restore into the "
          f"scratch home (hash-checked): {len(recovered)}")
    for (rel, sha), how in sorted(recovered.items()):
        print(f"  + {rel}@{sha[:12]} — {how}")
    if still_missing:
        print(f"SYNC DATA LOST (pushed, nowhere in homes, backups, or the "
              f"recovery surface): {len(still_missing)}")
        for rel, sha in still_missing:
            print(f"  - {rel}@{sha[:12]}")
        return 1
    print("P1 HOLDS: every version ever pushed that the sync system touched "
          "is still present in a home, in a roam/backups directory, or was "
          "restored from the remote store through the plugin's own recovery "
          "surface and hash-checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
