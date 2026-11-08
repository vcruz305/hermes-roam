"""0.5.3 scope addition: the terminal-only `hermes roam auto on|off|status`
verb. RED-first: these tests ran before the verb existed.

Contract:
- `on` enables BOTH auto_push and auto_pull_shared for the ROOT home — a
  profile context writes the ROOT config, like every other verb — by running
  `hermes config set plugins.entries.hermes-roam.settings.KEY VALUE` as a
  BOUNDED argv subprocess (no shell, stdin closed, hard timeout); `off`
  disables both. `on`/`off` refuse before `hermes roam init`.
- `status` prints each switch, push_debounce_seconds, the last auto-push
  time/pending flag from the hooks' state, and one line stating exactly
  what auto does and does not do.
- Terminal-only: no `auto` agent tool exists, and the `/roam auto` slash
  form is READ-ONLY status (enabling unattended pulls is the user's
  decision, never the agent's).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import load_plugin_package  # noqa: E402

plugin = load_plugin_package()


KEY = "s" * 43  # token_urlsafe(32) stand-in; decode_key accepts the entropy


class State:
    def __init__(self):
        self.store = {}

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value):
        self.store[key] = value


class Ctx:
    def __init__(self, config):
        self.config = config
        self.state = State()
        self.tools = []
        self.hooks = []
        self.cli_commands = []
        self.commands = []
        self.skills = []

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_tool(self, *a, **k):
        self.tools.append(k)

    def register_hook(self, *a, **k):
        self.hooks.append(k)

    def register_cli_command(self, *a, **k):
        self.cli_commands.append(k)

    def register_command(self, *a, **k):
        self.commands.append(k)

    def register_skill(self, *a, **k):
        self.skills.append(k)


def make_ctx(tmp_path, monkeypatch, profile=False, initialized=True, extra=None):
    root = tmp_path / "root"
    active = root / "profiles" / "work" if profile else root
    active.mkdir(parents=True, exist_ok=True)
    if initialized:
        (root / "roam").mkdir(parents=True, exist_ok=True)
        (root / "roam" / "state.json").write_text("{}")
    config = {
        "backend": "local",
        "local_dir": str(tmp_path / "store"),
        "device_name": "dev",
    }
    if extra:
        config.update(extra)
    # the profile context resolves to the ROOT home like every other verb
    monkeypatch.setattr(plugin.settings, "hermes_home", lambda: active)
    ctx = Ctx(config)
    plugin.settings.set_context(ctx)
    return ctx, root, active


def parse(args):
    return plugin.cli.make_parser().parse_args(args)


def run(args):
    return plugin.cli.handler(parse(args))


# ------------------------------------------------------------------- parser


def test_parser_has_the_auto_verb_with_on_off_status() -> None:
    """The `hermes roam auto` subparser exists with exactly on/off/status."""
    args = parse(["auto", "on"])
    assert args.action == "on"
    assert parse(["auto", "off"]).action == "off"
    assert parse(["auto"]).action == "status"
    with pytest.raises(SystemExit):
        parse(["auto", "explode"])


# ------------------------------------------------------------------ refusal


def test_on_refuses_before_roam_init(tmp_path, monkeypatch) -> None:
    """`on` before `hermes roam init`: a clear error, nothing written."""
    monkeypatch.setenv("ROAM_ARCHIVE_KEY", KEY)
    make_ctx(tmp_path, monkeypatch, initialized=False)
    calls = []
    monkeypatch.setattr(plugin.cli, "_auto_config_set",
                        lambda home, key, value: calls.append((home, key, value)) or (True, ""))
    code = run(["auto", "on"])
    assert code == 1
    assert calls == []


def test_off_refuses_before_roam_init(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROAM_ARCHIVE_KEY", KEY)
    make_ctx(tmp_path, monkeypatch, initialized=False)
    calls = []
    monkeypatch.setattr(plugin.cli, "_auto_config_set",
                        lambda home, key, value: calls.append((home, key, value)) or (True, ""))
    assert run(["auto", "off"]) == 1
    assert calls == []


# ------------------------------------------------------------------ on / off


def test_on_sets_both_switches_for_the_root_home(tmp_path, monkeypatch, capsys) -> None:
    """`on` flips BOTH switches and always targets the ROOT home."""
    monkeypatch.setenv("ROAM_ARCHIVE_KEY", KEY)
    ctx, root, _ = make_ctx(tmp_path, monkeypatch, profile=True)  # active home is a profile
    calls = []
    monkeypatch.setattr(
        plugin.cli, "_auto_config_set",
        lambda home, key, value: calls.append((home, key, value)) or (True, ""),
    )
    code = run(["auto", "on"])
    assert code == 0
    assert [(str(h), k, v) for h, k, v in calls] == [
        (str(root), "auto_push", "true"),
        (str(root), "auto_pull_shared", "true"),
    ]
    out = capsys.readouterr().out
    assert "auto_push" in out and "auto_pull_shared" in out


def test_off_clears_both_switches(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("ROAM_ARCHIVE_KEY", KEY)
    ctx, root, _ = make_ctx(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        plugin.cli, "_auto_config_set",
        lambda home, key, value: calls.append((home, key, value)) or (True, ""),
    )
    assert run(["auto", "off"]) == 0
    assert [(k, v) for _h, k, v in calls] == [
        ("auto_push", "false"), ("auto_pull_shared", "false"),
    ]


def test_a_failed_config_set_fails_closed(tmp_path, monkeypatch, capsys) -> None:
    """If the config-set subprocess fails, the verb reports and exits 1."""
    monkeypatch.setenv("ROAM_ARCHIVE_KEY", KEY)
    make_ctx(tmp_path, monkeypatch)
    monkeypatch.setattr(
        plugin.cli, "_auto_config_set",
        lambda home, key, value: (False, "the config subprocess failed"),
    )
    assert run(["auto", "on"]) == 1
    assert "failed" in capsys.readouterr().out


def test_config_set_subprocess_is_bounded_and_promptless(tmp_path, monkeypatch) -> None:
    """The real subprocess discipline: argv (never a shell), stdin closed,
    HERMES_HOME pinned to the ROOT home, a hard timeout, no prompts."""
    import subprocess as subprocess_mod

    runs = []

    class Proc:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(argv, **kwargs):
        runs.append((argv, kwargs))
        return Proc()

    monkeypatch.setattr(subprocess_mod, "run", fake_run)
    monkeypatch.setattr(plugin.cli, "find_hermes", lambda: "/usr/bin/hermes")
    ok, message = plugin.cli._auto_config_set(tmp_path, "auto_push", "true")
    assert ok and not message
    argv, kwargs = runs[0]
    assert argv == [
        "/usr/bin/hermes", "config", "set",
        "plugins.entries.hermes-roam.settings.auto_push", "true",
    ]
    assert kwargs["stdin"] == subprocess_mod.DEVNULL
    assert kwargs["env"]["HERMES_HOME"] == str(tmp_path)
    assert 0 < kwargs["timeout"] <= 60


# ------------------------------------------------------------------- status


def test_status_shows_switches_debounce_and_hook_state(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("ROAM_ARCHIVE_KEY", KEY)
    ctx, _root, _active = make_ctx(
        tmp_path, monkeypatch, extra={"auto_push": True, "push_debounce_seconds": 90}
    )
    ctx.state.store["last_auto_push_at"] = 1736000000.0
    ctx.state.store["pending_auto_push"] = True
    assert run(["auto"]) == 0
    out = capsys.readouterr().out
    assert "auto_push" in out and "true" in out
    assert "auto_pull_shared" in out and "false" in out
    assert "90" in out  # push_debounce_seconds
    assert "pending" in out  # the hooks' pending flag
    assert "2025" in out  # the last auto-push time, rendered
    # one line states exactly what auto does and does not do
    assert "session end" in out and "skills" in out and "SOUL.md" in out
    assert "never" in out and "config" in out


def test_status_json_is_machine_readable(tmp_path, monkeypatch, capsys) -> None:
    import json

    monkeypatch.setenv("ROAM_ARCHIVE_KEY", KEY)
    make_ctx(tmp_path, monkeypatch, extra={"auto_push": True, "auto_pull_shared": True})
    assert run(["auto", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["auto_push"] is True
    assert payload["auto_pull_shared"] is True
    assert "push_debounce_seconds" in payload
    assert "pending_auto_push" in payload


# ------------------------------------------------------- terminal-only shape


def test_no_auto_agent_tool_and_readonly_slash_form(tmp_path, monkeypatch) -> None:
    """No `auto` agent tool is registered, and the /roam form is read-only."""
    monkeypatch.setenv("ROAM_ARCHIVE_KEY", KEY)
    ctx, _root, _active = make_ctx(
        tmp_path, monkeypatch,
        extra={"auto_push": False, "auto_pull_shared": False},
    )
    plugin.register(ctx)
    names = {t.get("name") for t in ctx.tools}
    assert "roam_auto" not in names
    assert "auto" not in names
    # the slash verb exists and only ever reports read-only status
    reply = plugin.commands.handle("auto")
    assert "auto" in reply
    assert "read-only" in reply
    assert "hermes roam auto on" in reply  # it points at the terminal verb
    assert " true" not in reply.replace("auto_push False", "")
