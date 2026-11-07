"""Tests for roam.inventory: classification table, walk behavior, manifests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from roam import inventory, scope
from roam.model import Scope
from roam.scope import Rules

RULES = Rules.default()

# (relpath, expected scope) — the allowlist contract table, including profiles.
TABLE: list[tuple[str, Scope]] = [
    (".env", Scope.LOCAL),
    ("auth.json", Scope.LOCAL),
    ("auth.json.bak", Scope.LOCAL),
    ("auth.lock", Scope.LOCAL),
    ("x.lock", Scope.LOCAL),
    ("state.db", Scope.LOCAL),
    ("state.db-wal", Scope.LOCAL),
    ("deliveries.db", Scope.LOCAL),
    ("deliveries.db-shm", Scope.LOCAL),
    ("executions.db", Scope.LOCAL),
    ("gateway/config", Scope.LOCAL),
    ("gateway-service/x", Scope.LOCAL),
    ("bin/tool", Scope.LOCAL),
    ("hermes-agent/x", Scope.LOCAL),
    ("plugin-data/x", Scope.LOCAL),
    ("desktop/x", Scope.LOCAL),
    ("profiles/work/home/x", Scope.LOCAL),
    ("profiles/work/home/.mozilla/y", Scope.LOCAL),
    ("context_length_cache.yaml", Scope.LOCAL),
    ("context_length_cache.yaml.bak", Scope.LOCAL),
    ("channel_directory.json", Scope.LOCAL),
    ("x.bak", Scope.LOCAL),
    ("x.bak2", Scope.LOCAL),
    ("x.before-push", Scope.LOCAL),
    ("x.pid", Scope.LOCAL),
    ("node_modules/x", Scope.LOCAL),
    ("installs/x/y.bin", Scope.LOCAL),
    ("roam/state.json", Scope.LOCAL),
    ("roam/git-mirror/HEAD", Scope.LOCAL),
    ("roam/backups/a", Scope.LOCAL),
    ("roam/", Scope.LOCAL),
    ("cache/junk", Scope.IGNORED),
    ("audio_cache/x", Scope.IGNORED),
    ("image_cache/x", Scope.IGNORED),
    ("logs/x", Scope.IGNORED),
    ("backups/x", Scope.IGNORED),
    ("bootstrap-cache/x", Scope.IGNORED),
    ("checkpoints/x", Scope.IGNORED),
    ("sandboxes/x", Scope.IGNORED),
    ("state-snapshots/x", Scope.IGNORED),
    ("tmp/x", Scope.IGNORED),
    ("__pycache__/x", Scope.IGNORED),
    (".git/config", Scope.IGNORED),
    ("plugins/foo/.git/config", Scope.IGNORED),
    ("venv/x", Scope.IGNORED),
    (".venv/x", Scope.IGNORED),
    ("reports/x", Scope.IGNORED),
    ("profile-archives/x", Scope.IGNORED),
    ("cache/", Scope.IGNORED),
    ("sessions/a.jsonl", Scope.SESSIONS),
    ("profiles/work/sessions/a.jsonl", Scope.SESSIONS),
    ("sessions/", Scope.SESSIONS),
    ("cron/jobs.json", Scope.CRON),
    ("profiles/work/cron/jobs.json", Scope.CRON),
    ("config.yaml", Scope.SHARED),
    ("SOUL.md", Scope.SHARED),
    ("hermes-pack.yaml", Scope.SHARED),
    ("memories/x.md", Scope.SHARED),
    ("skills/x/SKILL.md", Scope.SHARED),
    ("skills/x/y.lock", Scope.LOCAL),
    ("skills/x/y.bak", Scope.LOCAL),
    ("skills/x/y.tmp", Scope.LOCAL),
    ("skills/x/__pycache__/y", Scope.IGNORED),
    ("skills/x/node_modules/y", Scope.LOCAL),
    ("skills/.hub/lock.json", Scope.IGNORED),
    ("skills/.locks/curator-run", Scope.IGNORED),
    ("skills/.usage.json", Scope.IGNORED),
    ("profiles/work/skills/.hub/lock.json", Scope.IGNORED),
    ("skins/x", Scope.SHARED),
    ("hooks/x", Scope.LOCAL),
    ("tui-widgets/x", Scope.LOCAL),
    ("desktop-plugins/x", Scope.LOCAL),
    ("pets/x", Scope.LOCAL),
    ("plugins/foo/plugin.yaml", Scope.LOCAL),
    ("plugins/hermes-roam/skills/roam-setup/SKILL.md", Scope.LOCAL),
    ("plugins/foo/memories/x", Scope.LOCAL),
    ("plugins/foo/.git/config", Scope.IGNORED),
    ("profiles/work/plugins/foo/plugin.yaml", Scope.LOCAL),
    # RULING: R2 — rules.yaml is per-device policy, never synced.
    ("roam/rules.yaml", Scope.LOCAL),
    ("unknown.txt", Scope.LOCAL),
    ("x.tmp", Scope.LOCAL),
    ("hf_token", Scope.LOCAL),
    ("vault/x", Scope.LOCAL),
    ("kanban.db", Scope.LOCAL),
    ("gateway_state.json", Scope.LOCAL),
    ("shared/x", Scope.LOCAL),
    ("profiles/work/config.yaml", Scope.SHARED),
    ("profiles/work/SOUL.md", Scope.SHARED),
    ("profiles/work/hermes-pack.yaml", Scope.SHARED),
    ("profiles/work/skills/x/SKILL.md", Scope.SHARED),
    ("profiles/work/skins/x", Scope.SHARED),
    ("profiles/work/.env", Scope.LOCAL),
    ("profiles/work/cache/x", Scope.IGNORED),
    ("profiles/work/hooks/x", Scope.LOCAL),
    ("profiles/work/unknown.txt", Scope.LOCAL),
    ("profiles/work/roam/state.json", Scope.LOCAL),
    # RULING: R2 — per-profile rules.yaml is per-device policy, never synced.
    ("profiles/work/roam/rules.yaml", Scope.LOCAL),
    ("profiles/play/config.yaml", Scope.SHARED),
]


@pytest.mark.parametrize(("relpath", "expected"), TABLE)
def test_classification_matches_contract(relpath: str, expected: Scope) -> None:
    assert inventory.classify(relpath, RULES) is expected


def build_fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()

    def put(relpath: str, content: bytes = b"x", mode: int = 0o644) -> None:
        target = home / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(mode)

    put("config.yaml", b"model: test\n")
    put(".env", b"SECRET=1\n")
    put("auth.json", b"{}")
    put("state.db", b"sqlite")
    put("SOUL.md", b"soul\n")
    put("memories/m1.md", b"memory one")
    put("skills/x/SKILL.md", b"# skill")
    put("skills/x/run.sh", b"#!/bin/sh\n", mode=0o755)
    put("sessions/a.jsonl", b"{}\n")
    put("cron/jobs.json", b"[]")
    put("cache/junk", b"junk" * 100)
    put("logs/x.log", b"log")
    put("plugins/foo/plugin.yaml", b"name: foo\n")
    put("roam/rules.yaml", b"ignored: []\n")
    put("roam/state.json", b"{}")
    put("profiles/work/config.yaml", b"model: work\n")
    put("profiles/work/.env", b"SECRET=2\n")
    put("profiles/work/memories/w.md", b"work memory")
    put("profiles/work/cache/c", b"c")
    put("profiles/work/home/.mozilla/bookmarks", b"bm")
    put("profiles/play/config.yaml", b"model: play\n")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    (home / "link.txt").symlink_to(outside)
    (home / "linked-dir").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    return home


SHARED_EXPECTED = [
    "SOUL.md",
    "config.yaml",
    "memories/m1.md",
    "profiles/play/config.yaml",
    "profiles/work/config.yaml",
    "profiles/work/memories/w.md",
    "skills/x/SKILL.md",
    "skills/x/run.sh",
]

OPTED_IN_EXTRA = ["cron/jobs.json", "sessions/a.jsonl"]


def test_walk_shared_only(tmp_path: Path) -> None:
    home = build_fake_home(tmp_path)
    entries = inventory.walk_home(home, RULES, include={Scope.SHARED})
    assert [entry.relpath for entry in entries] == SHARED_EXPECTED
    for entry in entries:
        assert entry.scope is Scope.SHARED


def test_walk_never_lists_local_or_ignored(tmp_path: Path) -> None:
    home = build_fake_home(tmp_path)
    entries = inventory.walk_home(
        home, RULES, include={Scope.SHARED, Scope.SESSIONS, Scope.CRON, Scope.LOCAL}
    )
    relpaths = [entry.relpath for entry in entries]
    # symlinked file skipped even when every listable scope is included
    assert "link.txt" not in relpaths
    # IGNORED directories are pruned at directory level, also inside profiles
    for prefix in ("cache/", "logs/", "profiles/work/cache/"):
        assert not [r for r in relpaths if r.startswith(prefix)]
    # LOCAL files appear only when LOCAL is explicitly included
    assert ".env" in relpaths
    assert "profiles/work/.env" in relpaths  # rules re-applied inside profiles
    assert "sessions/a.jsonl" in relpaths


def test_walk_sessions_and_cron_are_opt_in(tmp_path: Path) -> None:
    home = build_fake_home(tmp_path)
    entries = inventory.walk_home(
        home, RULES, include={Scope.SHARED, Scope.SESSIONS, Scope.CRON}
    )
    assert [entry.relpath for entry in entries] == sorted(SHARED_EXPECTED + OPTED_IN_EXTRA)


def test_walk_is_deterministic(tmp_path: Path) -> None:
    home = build_fake_home(tmp_path)
    first = inventory.walk_home(home, RULES, include={Scope.SHARED})
    second = inventory.walk_home(home, RULES, include={Scope.SHARED})
    assert first == second


def test_walk_modes_and_hashes(tmp_path: Path) -> None:
    home = build_fake_home(tmp_path)
    entries = inventory.walk_home(home, RULES, include={Scope.SHARED})
    by_path = {entry.relpath: entry for entry in entries}
    assert by_path["skills/x/run.sh"].mode == 0o755
    assert by_path["skills/x/SKILL.md"].mode == 0o644
    assert by_path["config.yaml"].size == len(b"model: test\n")
    assert by_path["config.yaml"].sha256 == hashlib.sha256(b"model: test\n").hexdigest()
    assert by_path["config.yaml"].sha256 == inventory.sha256_file(home / "config.yaml")


def test_walk_missing_home(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        inventory.walk_home(tmp_path / "missing", RULES, include={Scope.SHARED})


def test_manifest_from_entries_and_diff(tmp_path: Path) -> None:
    home = build_fake_home(tmp_path)
    entries = inventory.walk_home(home, RULES, include={Scope.SHARED})
    manifest = inventory.manifest_from_entries(entries)
    assert set(manifest) == {entry.relpath for entry in entries}
    assert manifest["config.yaml"]["mode"] == 0o644

    base = dict(manifest)
    added_path = "memories/new.md"
    (home / added_path).write_bytes(b"new")
    changed_path = "SOUL.md"
    (home / changed_path).write_bytes(b"changed soul")

    other = inventory.manifest_from_entries(
        inventory.walk_home(home, RULES, include={Scope.SHARED})
    )
    added, changed, removed = inventory.diff_manifests(base, other)
    assert added == [added_path]
    assert changed == [changed_path]
    assert removed == []


def test_diff_manifests_removed_and_unchanged(tmp_path: Path) -> None:
    base = {"a": {"sha256": "1"}, "b": {"sha256": "2"}, "c": {"sha256": "3"}}
    other = {"a": {"sha256": "1"}, "b": {"sha256": "9"}, "d": {"sha256": "4"}}
    added, changed, removed = inventory.diff_manifests(base, other)
    assert added == ["d"]
    assert changed == ["b"]
    assert removed == ["c"]


def test_walk_user_ignored_rule_prunes(tmp_path: Path) -> None:
    home = build_fake_home(tmp_path)
    (home / "notes").mkdir()
    (home / "notes" / "n.md").write_bytes(b"n")
    (home / "roam" / "rules.yaml").write_text("ignored: [notes/]\n")
    rules = scope.load_rules(home)
    entries = inventory.walk_home(home, rules, include={Scope.SHARED})
    assert "notes/n.md" not in [entry.relpath for entry in entries]
    assert "config.yaml" in [entry.relpath for entry in entries]


def test_walk_never_opens_local_files(tmp_path: Path) -> None:
    """Classification happens before any open: a LOCAL file with no read
    permission must not raise and must never be listed or hashed."""
    home = build_fake_home(tmp_path)
    secret = home / "local-secret.txt"
    secret.write_bytes(b"secret")
    secret.chmod(0)
    entries = inventory.walk_home(home, RULES, include={Scope.SHARED})
    relpaths = [entry.relpath for entry in entries]
    assert "local-secret.txt" not in relpaths
    assert relpaths == SHARED_EXPECTED


# Structural snapshot of a real Hermes home's top-level names (os.listdir,
# 2026-11-05), private host/machine tokens neutralized; pattern shapes kept.
REAL_HOME_TOP_LEVEL: tuple[str, ...] = (
    ".antigravity_models.json", ".antigravity_oauth.json",
    ".codex_gpt55_autoraise_notice", ".curator_backups", ".env",
    ".env.bak-20260929-201147", ".env.lock",
    ".hermes-update-in-progress.lock", ".hermes_history", ".install_id.lock",
    ".npm_lock_hash_3c2af2389f90", ".npm_lock_hash_3c2af2389f90_desktop",
    ".provider_models_cache_ackphzic.tmp", ".provider_models_cache_go5rf7u6.tmp",
    ".restart_last_processed.json", ".skills_prompt_snapshot.json", "SOUL.md",
    "audio_cache", "auth.json", "auth.json.bak-20260902-181903", "auth.lock",
    "backups", "bin", "bootstrap-cache", "cache", "channel_directory.json",
    "config.yaml", "config.yaml.bak-20260902-181903",
    "config.yaml.bak-20260914-090131", "config.yaml.bak-20260914-095343",
    "config.yaml.bak-20260914-095732",
    "config.yaml.bak-20260929-192028-hoste",
    "config.yaml.bak-20260929-201315-hostf", "config.yaml.bak-hostg-20261004",
    "config.yaml.bak-hosta-20261005001335",
    "config.yaml.bak-hostb-20261004-1111",
    "config.yaml.bak-hostb-20261004235619",
    "config.yaml.bak-hostb-rename-20261005000842",
    "config.yaml.before-hostc-cyber-20261007T211728",
    "config.yaml.before-mig-a-20261013T1608",
    "config.yaml.before-mig-b-20261013T1604",
    "config.yaml.before-mig-c-ip-20261010T011658",
    "config.yaml.before-mig-d-20261012T013734", "context_length_cache.yaml",
    "context_length_cache.yaml.lock", "cron", "deliveries.db", "desktop",
    "desktop-plugins", "etherfi_addrs_raw.txt", "etherfi_scope_pairs.json",
    "executions.db", "fira_bounty.txt", "flight_payload.txt", "gateway",
    "gateway-service", "gateway-starts.log", "gateway.lock", "gateway.pid",
    "gateway.sock", "gateway_state.json", "hermes-agent", "hermes-setup.exe",
    "hf_token", "hooks", "image_cache", "install_id", "installs", "kanban",
    "kanban.db", "kanban.db-shm", "kanban.db-wal", "kanban.db.dispatch.lock",
    "kanban.db.init.lock", "locks", "logs", "lsp", "memories",
    "models_dev_cache.etag", "models_dev_cache.json", "node", "notepad.db",
    "ollama_cloud_models_cache.json", "outputs", "pairing", "pending",
    "pending_messages", "platforms", "plugin-update-checks", "plugins",
    "price-watches", "processes.json", "processes.json.lock", "projects.db",
    "provider_models_cache.json", "provider_models_cache.json.lock",
    "runtime", "sandboxes", "scripts", "sessions", "shared", "shared-state.db",
    "skills", "source-checks", "spawn-ledger.json", "state", "state.db",
    "state.db-shm", "state.db-wal", "state.db.auto-maintenance.lock",
    "state.db.fts_rebuild.lock", "state.db.quarantine.lock",
    "telegram_update_receipts_1000000001.json", "terminal-sessions", "tools",
    "vault", "verification_evidence.db", "web-ui-build-stamp.json",
)

# The allowlisted names that exist in that snapshot; everything else must
# stay behind (only these four paths may ever reach a bundle).
REAL_HOME_BUNDLED_EXPECTED = [
    "SOUL.md",
    "config.yaml",
    "memories/f",
    "skills/f",
]


def build_real_name_home(tmp_path: Path) -> Path:
    """Fake home with every real top-level name (dummy contents)."""
    home = tmp_path / "real-home"
    home.mkdir()
    for name in REAL_HOME_TOP_LEVEL:
        target = home / name
        if name in ("config.yaml", "SOUL.md"):
            target.write_bytes(b"dummy")
            continue
        target.mkdir()
        (target / "f").write_bytes(b"dummy")
    return home


def test_real_home_names_bundle_exactly_the_allowlist(tmp_path: Path) -> None:
    """Regression for the F0 allowlist flip: with the old denylist catch-all,
    most of these 118 names were SHARED (credentials included); now the
    bundled set is exactly the allowlisted ones present."""
    home = build_real_name_home(tmp_path)
    rules = scope.load_rules(home)
    entries = inventory.walk_home(home, rules, include={Scope.SHARED})
    assert [entry.relpath for entry in entries] == REAL_HOME_BUNDLED_EXPECTED
    # and none of the credential-ish names is even classified shared
    for name in ("hf_token", ".antigravity_oauth.json", "vault", "shared-state.db"):
        assert scope.classify_path(name, rules) is Scope.LOCAL


def test_walk_excludes_hub_installed_skills(tmp_path: Path) -> None:
    """A realistic skills tree: user skills bundle; hub installs (recorded in
    skills/.hub/lock.json, the marker Hermes's own code reads) and hub state
    never do."""
    home = tmp_path / "home"
    home.mkdir()
    for rel in (
        "SOUL.md",
        "skills/my-skill/SKILL.md",
        "skills/my-skill/scripts/run.sh",
        "skills/evil-skill/SKILL.md",
        "skills/mlops/training/nested/SKILL.md",
        "skills/.locks/abc.lock",
        "skills/.usage.json",
    ):
        target = home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
    hub = home / "skills" / ".hub"
    hub.mkdir(parents=True)
    (hub / "lock.json").write_text(
        json.dumps(
            {
                "installed": {
                    "evil-skill": {"install_path": "evil-skill"},
                    "nested": {"install_path": "mlops/training/nested"},
                }
            }
        ),
        encoding="utf-8",
    )
    rules = scope.load_rules(home)
    entries = inventory.walk_home(home, rules, include={Scope.SHARED})
    assert [entry.relpath for entry in entries] == [
        "SOUL.md",
        "skills/my-skill/SKILL.md",
        "skills/my-skill/scripts/run.sh",
    ]


def test_walk_records_real_permission_bits(tmp_path: Path) -> None:
    """r2 f8: the manifest records the file's real permission bits; a 0600
    private memory must not be flattened to 0644."""
    home = tmp_path / "home"
    home.mkdir()
    modes = {
        "memories/private.md": 0o600,
        "memories/ro-group.md": 0o640,
        "memories/ro.md": 0o644,
        "skills/x/run.sh": 0o755,
        "skills/x/dev.sh": 0o700,
    }
    for rel, mode in modes.items():
        target = home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
        target.chmod(mode)
    entries = inventory.walk_home(home, RULES, include={Scope.SHARED})
    assert {entry.relpath: entry.mode for entry in entries} == modes


def test_local_rule_trees_are_pruned_before_traversal(tmp_path: Path) -> None:
    """Directory-level pruning: a LOCAL tree is never traversed at all, and
    the shared trees beside it still list completely."""
    home = tmp_path / "home"
    home.mkdir()
    for rel in ("skills/mine/SKILL.md", "memories/m1.md", "SOUL.md"):
        target = home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    # an unreadable file inside a LOCAL tree: pruning means it is never opened
    secret = home / "installs" / "env" / "no-read.json"
    secret.parent.mkdir(parents=True)
    secret.write_text("x", encoding="utf-8")
    secret.chmod(0)
    entries = inventory.walk_home(home, RULES, include={Scope.SHARED})
    assert [entry.relpath for entry in entries] == [
        "SOUL.md",
        "memories/m1.md",
        "skills/mine/SKILL.md",
    ]
