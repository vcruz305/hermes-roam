"""Tests for roam.scope: rules loading, config merge/strip, plugin inventory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from roam import scope
from roam.errors import ConfigError
from roam.model import Scope

REMOTE_TEXT = """\
model:
  provider: openai
  name: gpt-test
terminal:
  shell: /bin/zsh
  font: mono
paths:
  downloads: /root/down
display:
  interface: terminal
  theme: dark
browser:
  executable: /root/.lepton/bin
  headless: true
mcp_servers:
  git:
    command: /root/bin/mcp-git
  s3:
    command: /usr/local/bin/mcp-s3
plugins:
  entries:
    hermes-roam:
      enabled: true
    other:
      enabled: true
memory_dir: /root/.hermes/mem
"""

LOCAL_TEXT = """\
model:
  provider: anthropic
  name: claude-test
terminal:
  shell: /bin/bash
  theme: dark
paths:
  downloads: /home/victor/down
display:
  interface: desktop
browser:
  executable: /usr/bin/firefox
mcp_servers:
  git:
    command: /home/victor/bin/mcp-git
plugins:
  entries:
    hermes-roam:
      enabled: false
memory_dir: /home/victor/.hermes/mem
"""

HOST_KEYS = list(scope.HOST_CONFIG_KEYS)


def test_load_rules_defaults_when_no_file(tmp_path: Path) -> None:
    rules = scope.load_rules(tmp_path)
    assert rules.rules == scope.DEFAULT_RULES
    assert rules.host_keys == scope.HOST_CONFIG_KEYS


def test_allowlist_unknown_paths_are_local(tmp_path: Path) -> None:
    """The denylist catch-all is gone: nothing unknown ever bundles."""
    rules = scope.load_rules(tmp_path)
    for rel in (
        "unknown.txt",
        "hf_token",
        ".antigravity_oauth.json",
        "vault/x",
        "pairing/y",
        "node/z",
        "tools/z",
        "lsp/z",
        "runtime/z",
        "kanban.db",
        "gateway_state.json",
        "shared-state.db",
        "hermes-setup.exe",
        "provider_models_cache.json",
        "x.tmp",
        "hooks/x",
        "tui-widgets/x",
        "desktop-plugins/x",
        "pets/x",
    ):
        assert scope.classify_path(rel, rules) is Scope.LOCAL


def test_allowlist_shared_set_is_exact(tmp_path: Path) -> None:
    rules = scope.load_rules(tmp_path)
    for rel in (
        "config.yaml",
        "SOUL.md",
        "hermes-pack.yaml",
        "memories/x.md",
        "skills/x/SKILL.md",
        "skins/x",
        "profiles/w/config.yaml",
        "profiles/w/SOUL.md",
        "profiles/w/hermes-pack.yaml",
        "profiles/w/memories/x.md",
        "profiles/w/skills/x/SKILL.md",
        "profiles/w/skins/x",
    ):
        assert scope.classify_path(rel, rules) is Scope.SHARED, rel


def test_rules_yaml_is_local_per_device(tmp_path: Path) -> None:
    """RULING: R2 — roam/rules.yaml is per-device policy: LOCAL at the top
    level and inside every profile, and no user rule can make it travel."""
    rules = scope.load_rules(tmp_path)
    for rel in (
        "roam/rules.yaml",
        "profiles/w/roam/rules.yaml",
    ):
        assert scope.classify_path(rel, rules) is Scope.LOCAL, rel
    (tmp_path / "roam").mkdir()
    (tmp_path / "roam" / "rules.yaml").write_text("shared: [roam/rules.yaml]\n")
    rules = scope.load_rules(tmp_path)
    assert scope.classify_path("roam/rules.yaml", rules) is Scope.LOCAL


def test_shared_dirs_exclude_junk_at_depth(tmp_path: Path) -> None:
    rules = scope.load_rules(tmp_path)
    for rel in (
        "skills/x/y.lock",
        "skills/x/y.bak-old",
        "skills/x/y.tmp",
        "skills/x/__pycache__/y",
        "skills/x/.git/config",
        "skills/x/node_modules/y",
        "memories/x.lock",
        "profiles/w/skills/x/y.tmp",
    ):
        assert scope.classify_path(rel, rules) is not Scope.SHARED, rel


def test_hermes_managed_skill_state_is_ignored(tmp_path: Path) -> None:
    """skills/.hub, .locks, .archive … are Hermes-managed, never listed."""
    rules = scope.load_rules(tmp_path)
    for rel in (
        "skills/.hub/lock.json",
        "skills/.locks/curator-run",
        "skills/.archive/x",
        "skills/.curator_backups/x",
        "skills/.usage.json",
        "profiles/w/skills/.hub/lock.json",
    ):
        assert scope.classify_path(rel, rules) is Scope.IGNORED, rel


def test_user_shared_entries_extend_the_allowlist(tmp_path: Path) -> None:
    (tmp_path / "roam").mkdir()
    (tmp_path / "roam" / "rules.yaml").write_text(
        "shared: [hooks/, tui-widgets/, notes/]\n"
    )
    rules = scope.load_rules(tmp_path)
    assert scope.classify_path("hooks/x", rules) is Scope.SHARED
    assert scope.classify_path("tui-widgets/x", rules) is Scope.SHARED
    assert scope.classify_path("notes/x", rules) is Scope.SHARED
    # the defaults still apply to everything the user did not list
    assert scope.classify_path("other-unknown.txt", rules) is Scope.LOCAL


def _write_hub_lock(home: Path, installed: dict) -> None:
    """A real-shaped skills/.hub/lock.json (the marker Hermes's own code reads)."""
    hub = home / "skills" / ".hub"
    hub.mkdir(parents=True, exist_ok=True)
    (hub / "lock.json").write_text(json.dumps({"installed": installed}), encoding="utf-8")


def test_hub_lock_install_paths_stay_local(tmp_path: Path) -> None:
    _write_hub_lock(
        tmp_path,
        {
            "evil-skill": {"install_path": "evil-skill", "trust_level": "community"},
            "nested-skill": {"install_path": "mlops/training/nested-skill"},
            "no-path": {"trust_level": "builtin"},
        },
    )
    rules = scope.load_rules(tmp_path)
    assert scope.classify_path("skills/evil-skill/SKILL.md", rules) is Scope.LOCAL
    assert scope.classify_path("skills/mlops/training/nested-skill/x", rules) is Scope.LOCAL
    # user-written skills keep syncing
    assert scope.classify_path("skills/my-skill/SKILL.md", rules) is Scope.SHARED


def test_hub_lock_traversal_entry_adds_nothing(tmp_path: Path) -> None:
    _write_hub_lock(tmp_path, {"evil": {"install_path": "../../etc"}})
    rules = scope.load_rules(tmp_path)
    assert scope.classify_path("skills/x/SKILL.md", rules) is Scope.SHARED
    # the malformed entry must not weaken the lock-tree exclusion
    assert scope.classify_path("skills/.hub/lock.json", rules) is Scope.IGNORED


def test_hub_lock_unreadable_is_tolerated(tmp_path: Path) -> None:
    hub = tmp_path / "skills" / ".hub"
    hub.mkdir(parents=True)
    (hub / "lock.json").write_text("not json at all", encoding="utf-8")
    rules = scope.load_rules(tmp_path)
    assert scope.classify_path("skills/.hub/lock.json", rules) is Scope.IGNORED


def test_user_shared_entry_overrides_the_hub_lock(tmp_path: Path) -> None:
    _write_hub_lock(tmp_path, {"evil-skill": {"install_path": "evil-skill"}})
    (tmp_path / "roam").mkdir()
    (tmp_path / "roam" / "rules.yaml").write_text("shared: [skills/evil-skill/**]\n")
    rules = scope.load_rules(tmp_path)
    assert scope.classify_path("skills/evil-skill/SKILL.md", rules) is Scope.SHARED


def test_load_rules_user_overrides_take_precedence(tmp_path: Path) -> None:
    (tmp_path / "roam").mkdir()
    (tmp_path / "roam" / "rules.yaml").write_text(
        "shared: [state.db]\nignored: [notes/]\nhost_keys: [extra.key]\n"
    )
    rules = scope.load_rules(tmp_path)
    assert scope.classify_path("state.db", rules) is Scope.SHARED
    assert scope.classify_path("notes/", rules) is Scope.IGNORED
    assert scope.classify_path("notes/x.md", rules) is Scope.IGNORED
    assert scope.classify_path(".env", rules) is Scope.LOCAL
    assert rules.host_keys[0] == "extra.key"
    assert "terminal" in rules.host_keys


def test_load_rules_user_rules_cannot_untag_roam_state(tmp_path: Path) -> None:
    (tmp_path / "roam").mkdir()
    (tmp_path / "roam" / "rules.yaml").write_text("shared: [roam/state.json]\n")
    rules = scope.load_rules(tmp_path)
    assert scope.classify_path("roam/state.json", rules) is Scope.LOCAL
    # RULING: R2 — rules.yaml itself is per-device and never synced either.
    assert scope.classify_path("roam/rules.yaml", rules) is Scope.LOCAL


@pytest.mark.parametrize(
    "key",
    ["shared", "local", "ignored", "host_keys"],
)
def test_load_rules_malformed_key_names_the_key(tmp_path: Path, key: str) -> None:
    (tmp_path / "roam").mkdir()
    (tmp_path / "roam" / "rules.yaml").write_text(f"{key}: 5\n")
    with pytest.raises(ConfigError, match=key):
        scope.load_rules(tmp_path)


def test_load_rules_unknown_key(tmp_path: Path) -> None:
    (tmp_path / "roam").mkdir()
    (tmp_path / "roam" / "rules.yaml").write_text("bogus: []\n")
    with pytest.raises(ConfigError, match="bogus"):
        scope.load_rules(tmp_path)


def test_load_rules_non_mapping(tmp_path: Path) -> None:
    (tmp_path / "roam").mkdir()
    (tmp_path / "roam" / "rules.yaml").write_text("- a\n- b\n")
    with pytest.raises(ConfigError):
        scope.load_rules(tmp_path)


def test_merge_config_keeps_local_host_values_and_remote_model() -> None:
    merged = yaml.safe_load(scope.merge_config(REMOTE_TEXT, LOCAL_TEXT, HOST_KEYS))
    assert merged["model"] == {"provider": "openai", "name": "gpt-test"}
    assert merged["terminal"] == {"shell": "/bin/bash", "theme": "dark"}
    assert merged["paths"] == {"downloads": "/home/victor/down"}
    assert merged["display"]["interface"] == "desktop"
    assert merged["display"]["theme"] == "dark"
    assert merged["browser"]["executable"] == "/usr/bin/firefox"
    assert merged["browser"]["headless"] is True
    # local has mcp_servers.git.command; the remote-only s3 entry is dropped
    assert merged["mcp_servers"] == {"git": {"command": "/home/victor/bin/mcp-git"}}
    assert merged["plugins"]["entries"]["hermes-roam"] == {"enabled": False}
    assert merged["plugins"]["entries"]["other"] == {"enabled": True}
    # remote home-path leaf replaced by the local value at that path
    assert merged["memory_dir"] == "/home/victor/.hermes/mem"
    assert list(merged) == [
        "model",
        "terminal",
        "paths",
        "display",
        "browser",
        "mcp_servers",
        "plugins",
        "memory_dir",
    ]


def test_merge_config_removes_host_keys_local_lacks() -> None:
    merged = yaml.safe_load(scope.merge_config(REMOTE_TEXT, "tui: {accent: blue}\n", HOST_KEYS))
    # model always comes from the remote; only host keys take local values,
    # and local-only non-host keys never enter the merged config
    assert merged["model"] == {"provider": "openai", "name": "gpt-test"}
    assert "tui" not in merged
    assert "terminal" not in merged
    assert "paths" not in merged
    assert merged["browser"] == {"headless": True}  # home-path leaf dropped
    assert "mcp_servers" not in merged
    assert "hermes-roam" not in merged["plugins"]["entries"]
    assert "memory_dir" not in merged


def test_merge_config_malformed_local_is_config_error() -> None:
    with pytest.raises(ConfigError, match="local"):
        scope.merge_config(REMOTE_TEXT, "a: [unclosed\n", HOST_KEYS)


def test_strip_host_keys_removes_host_scoped_values() -> None:
    stripped = yaml.safe_load(scope.strip_host_keys(REMOTE_TEXT, HOST_KEYS))
    assert stripped["model"] == {"provider": "openai", "name": "gpt-test"}
    assert "terminal" not in stripped
    assert "paths" not in stripped
    assert stripped["display"] == {"theme": "dark"}
    assert stripped["browser"] == {"headless": True}
    assert "mcp_servers" not in stripped
    assert stripped["plugins"] == {"entries": {"other": {"enabled": True}}}
    assert "memory_dir" not in stripped


def test_strip_then_merge_restores_local_values() -> None:
    """Push strips host paths; pull must still keep the local device's values."""
    stripped_text = scope.strip_host_keys(LOCAL_TEXT, HOST_KEYS)
    assert "terminal" not in yaml.safe_load(stripped_text)
    merged = yaml.safe_load(scope.merge_config(stripped_text, LOCAL_TEXT, HOST_KEYS))
    local = yaml.safe_load(LOCAL_TEXT)
    assert merged["terminal"] == local["terminal"]
    assert merged["paths"] == local["paths"]
    assert merged["browser"]["executable"] == local["browser"]["executable"]
    assert merged["mcp_servers"] == local["mcp_servers"]
    assert merged["memory_dir"] == local["memory_dir"]
    assert merged["model"] == local["model"]


def test_generate_installed_plugins(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    foo = plugins / "foo"
    (foo / ".git" / "refs" / "heads").mkdir(parents=True)
    (foo / "plugin.yaml").write_text("name: hermes-foo\nversion: 1.2.3\n")
    (foo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (foo / ".git" / "refs" / "heads" / "main").write_text("a" * 40 + "\n")
    bar = plugins / "bar"
    bar.mkdir()
    (bar / "plugin.yaml").write_text("name: hermes-bar\nversion: 0.1.0\n")
    (plugins / "stray.txt").write_text("not a plugin")
    assert scope.generate_installed_plugins(tmp_path) == {
        "hermes-foo": {"version": "1.2.3", "sha": "a" * 40},
        "hermes-bar": {"version": "0.1.0", "sha": ""},
    }


def test_generate_installed_plugins_packed_refs_and_missing_dir(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    baz = plugins / "baz"
    (baz / ".git").mkdir(parents=True)
    (baz / ".git" / "HEAD").write_text("ref: refs/heads/dev\n")
    (baz / ".git" / "packed-refs").write_text("b" * 40 + " refs/heads/dev\n")
    assert scope.generate_installed_plugins(tmp_path) == {"baz": {"version": "", "sha": "b" * 40}}
    assert scope.generate_installed_plugins(tmp_path / "empty") == {}


# ------------------------------------------------- secrets (review r2, f4)

SECRET_CONFIG = """\
model:
  provider: p-a
  name: gpt-test
  api_key: sk-abc123def456ghi789jkl
platforms:
  telegram:
    token: "123456:ABC-DEF"
custom:
  my_service:
    openai_api_key: ghp_abcdefghijklmnopqrstuvwxyz1234567890
    client_secret: supersecretvalue
  creds:
    - sk-abcdefghijklmnopqrstuvwxyz123456789
    - /root/leak/x
  keep: hello
"""


def test_secret_named_keys_never_travel() -> None:
    """r2 f4: keys that name secrets are host-scoped at any depth."""
    stripped = yaml.safe_load(scope.strip_host_keys(SECRET_CONFIG, HOST_KEYS))
    assert stripped == {"model": {"provider": "p-a", "name": "gpt-test"}, "custom": {"keep": "hello"}}


def test_secret_key_patterns_are_case_insensitive_and_suffix_anchored() -> None:
    for key in (
        "api_key", "API_KEY", "ApiKey", "apikey", "openai_api_key", "OPENAI_API_KEY",
        "token", "access_token", "refresh_token", "ssh_token", "gitlab_token",
        "secret", "client_secret", "my_secret", "password", "sudo_password",
        "passwd", "private_key", "bearer", "authorization", "cookie",
    ):
        assert scope.secret_like(key, "innocuous value") is True, key
    for key in (
        "model", "provider", "name", "enabled", "theme", "tokens", "secrets",
        "cookies", "authorized", "authorization_url", "tokenizer", "key",
        "auth", "apikey_url", "command", "path",
    ):
        assert scope.secret_like(key, "innocuous value") is False, key


def test_secret_key_names_are_normalised_before_matching() -> None:
    """RULING: R12 — key names are normalised (lowercase, '-' -> '_') before
    matching; the AWS credential names and every *_key name are secrets."""
    for key in (
        "aws_secret_access_key", "AWS_SECRET_ACCESS_KEY", "Aws-Secret-Access-Key",
        "aws_access_key_id", "aws_session_token", "secret_access_key",
        "access_key_id", "client_id_secret", "credentials",
        "service_credentials", "signin_credentials",
        "api-key", "API-KEY", "aws-secret-access-key",
        "signing_key", "encryption_key", "gpg-key", "my-app-key",
    ):
        assert scope.secret_like(key, "innocuous value") is True, key


def test_known_non_secret_key_names_keep_syncing() -> None:
    """RULING: R12 — the allowlist of known non-secret *_key names, plus the
    token-like shared settings, must keep syncing (no false positives)."""
    for key in (
        "cache_key", "sort_key", "primary_key", "key_strategy",
        "cache_key_strategy", "hotkey", "keybinding",
        "CACHE-KEY", "cache-Key-Strategy",
        "max_tokens", "token_budget",
    ):
        assert scope.secret_like(key, "innocuous value") is False, key


def test_aws_credentials_are_stripped_end_to_end() -> None:
    """RULING: R12 — the standard AWS credential key names match no pattern
    today; strip_host_keys must remove them (dash forms included)."""
    aws_secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLE"
    config = (
        "model:\n"
        "  provider: p-a\n"
        "mcp_servers:\n"
        "  fetch:\n"
        "    command: /usr/bin/fetch\n"
        "    env:\n"
        "      AWS_ACCESS_KEY_ID: NOTAKEYIDEXAMPLE00\n"
        f"      AWS_SECRET_ACCESS_KEY: {aws_secret}\n"
        "headers:\n"
        "  api-key: short-but-secret\n"
    )
    stripped = scope.strip_host_keys(config, HOST_KEYS)
    assert aws_secret not in stripped
    assert "short-but-secret" not in stripped
    assert "AWS_SECRET_ACCESS_KEY" not in stripped
    assert "AWS_ACCESS_KEY_ID" not in stripped
    parsed = yaml.safe_load(stripped)
    assert parsed["model"] == {"provider": "p-a"}  # the shared settings survive


def test_credential_looking_values_are_stripped_wherever_they_appear() -> None:
    values = (
        "sk-proj-0123456789abcdef",
        "ghp_0123456789abcdefghij",
        "github_pat_0123456789abcdefghij",
        "xoxb-0123456789abcdef",
        "xoxp-0123456789abcdef",
        "AKIA0123456789ABCDEF",
        "hf_abcdefghijklmnopqrstuvwxyz1234",
        "Bearer abc.def.ghi",
    )
    for value in values:
        assert scope.secret_like("innocuous", value) is True, value
    for value in ("sk-", "AKIA0123456789", "hf_short", "bearer", "usr/bin/sh"):
        assert scope.secret_like("innocuous", value) is False, value
    # a credential under an innocuous key still never travels, lists included
    config = "custom:\n  tools:\n    - plain value\n    - ghp_0123456789abcdefghij\n  ok: keep\n"
    stripped = yaml.safe_load(scope.strip_host_keys(config, HOST_KEYS))
    assert stripped == {"custom": {"ok": "keep"}}


def test_merge_config_keeps_local_secrets_local() -> None:
    """Push strips secrets; pull must keep this device's own secrets (they
    never left) and must not resurrect the remote's stripped ones."""
    remote = "model:\n  provider: p\n  name: n\n"
    local = (
        "model:\n  provider: q\n  name: n\n  api_key: sk-abc123def456ghi789\n"
        "platforms:\n  telegram:\n    token: t\n"
    )
    merged = yaml.safe_load(scope.merge_config(remote, local, HOST_KEYS))
    assert merged["model"] == {"provider": "p", "name": "n", "api_key": "sk-abc123def456ghi789"}
    assert merged["platforms"]["telegram"]["token"] == "t"
    # and the stripped remote never carries them in the first place
    assert "api_key" not in yaml.safe_load(scope.strip_host_keys(local, HOST_KEYS))["model"]


# --------------------------------------- home paths (review r2, f10)

def test_home_path_detector_covers_bare_home_dirs_and_windows_paths() -> None:
    for value in (
        "/root", "/root/", "/root/x/y", "/home/u", "/home/u/bin/f",
        "/Users/u", "/Users/u/x", "C:\\Users\\u", "C:\\Users\\u\\x",
        "C:/Users/u/x", "D:\\home\\u\\x",
    ):
        assert scope._is_home_path(value) is True, value
    for value in (
        "/homeless", "/roots", "/usr/bin/sh", "/etc/passwd", "Users/u",
        "C:\\Program Files\\x", "relative/path", "https://root.example.com",
    ):
        assert scope._is_home_path(value) is False, value


def test_lists_with_home_values_are_host_scoped_as_a_whole() -> None:
    """r2 f10: a list holding any home path (or credential) is stripped
    whole on push and the local list always wins on pull."""
    remote = (
        "mcp_servers:\n"
        "  fetch:\n"
        "    command: /root/bin/fetch\n"
        "    args:\n"
        "      - /root/leak-marker/server.py\n"
        "  keep:\n"
        "    - 1\n"
        "    - 2\n"
    )
    local = (
        "mcp_servers:\n"
        "  fetch:\n"
        "    command: /home/b/bin/fetch\n"
        "    args:\n"
        "      - /home/b/leak-marker/server.py\n"
        "  keep:\n"
        "    - 3\n"
    )
    host_keys = HOST_KEYS
    stripped = yaml.safe_load(scope.strip_host_keys(remote, host_keys))
    # the whole args list is gone (it held a home path), and so is the
    # host-covered command; the plain list stays
    assert stripped == {"mcp_servers": {"keep": [1, 2]}}

    merged = yaml.safe_load(scope.merge_config(remote, local, host_keys))
    assert merged["mcp_servers"]["fetch"]["args"] == ["/home/b/leak-marker/server.py"]
    assert merged["mcp_servers"]["fetch"]["command"] == "/home/b/bin/fetch"
    assert merged["mcp_servers"]["keep"] == [1, 2]


def test_merge_drops_remote_home_paths_local_lacks() -> None:
    remote = "custom:\n  workdir: /root\n  alt: C:\\Users\\bob\n"
    merged = scope.merge_config(remote, "custom:\n  other: keep\n", HOST_KEYS)
    # every remote value was a home path and the local device has none of
    # them: nothing survives, and local-only non-host keys never enter
    assert merged == ""
    # a remote sibling that is not host-scoped still travels
    merged2 = scope.merge_config(
        "custom:\n  workdir: /root\n  theme: dark\n", "custom:\n  other: keep\n", HOST_KEYS
    )
    assert yaml.safe_load(merged2) == {"custom": {"theme": "dark"}}


# ------------------------------------------- config path helper (review r2, f5)

def test_is_config_path_marks_exactly_the_config_files() -> None:
    assert scope.is_config_path("config.yaml") is True
    assert scope.is_config_path("profiles/p/config.yaml") is True
    assert scope.is_config_path("profiles/p-q_w/config.yaml") is True
    for rel in (
        "profiles/config.yaml", "profiles/a/b/config.yaml", "a/config.yaml",
        "config.yaml/x", "xconfig.yaml", "profiles//config.yaml",
        "profiles/../config.yaml", "profiles/./config.yaml", "CONFIG.YAML",
        "profiles/p/config.yaml/", "skills/config.yaml", "roam/config.yaml",
    ):
        assert scope.is_config_path(rel) is False, rel
