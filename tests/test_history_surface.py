"""0.4 surface tests: `hermes roam history`/`restore` CLI and `/roam history`."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from roam.errors import RoamError

HOSTILE = "notebooks/../../.ssh/id_rsa"
ENTRIES = [
    {
        "generation": 3,
        "device": "dev-a",
        "created_at": 1762000000.0,
        "path": "SOUL.md",
        "sha256": "a" * 64,
        "size": 7,
        "deleted": True,
    },
    {
        "generation": 2,
        "device": "dev-a",
        "created_at": 1761000000.0,
        "path": HOSTILE,
        "sha256": "b" * 64,
        "size": 9,
        "deleted": False,
    },
]
RESTORE_RESULT = {
    "path": "SOUL.md",
    "generation": 2,
    "device": "dev-a",
    "created_at": 1761000000.0,
    "sha256": "c" * 64,
    "size": 7,
    "applied": False,
    "backup_dir": "",
    "files_written": 0,
    "config_merged": False,
    "recovered": None,
}


class StubEngine:
    """Read-only stub: surfaces must sanitize whatever it returns."""

    home = Path("/tmp/never-used")
    backend = types.SimpleNamespace(name="stub")

    def __init__(self) -> None:
        self.history_calls: list = []
        self.restore_calls: list = []

    def history(self, path=None, *, limit=50):
        self.history_calls.append((path, limit))
        return [dict(entry) for entry in ENTRIES]

    def restore(self, path, *, generation, apply=False):
        self.restore_calls.append((path, generation, apply))
        result = dict(RESTORE_RESULT)
        if apply:
            result.update(
                applied=True,
                backup_dir="/tmp/home/roam/backups/20261101-000000",
                files_written=1,
            )
        return result


@pytest.fixture()
def stub_engine(plugin, monkeypatch):
    engine = StubEngine()
    monkeypatch.setattr(plugin.settings, "build_engine", lambda: engine)
    return engine


def cli_args(plugin, *argv):
    return plugin.cli.make_parser().parse_args(list(argv))


def test_cli_parser_builds_history_and_restore(plugin):
    args = cli_args(plugin, "history", "SOUL.md", "--json")
    assert args.path == "SOUL.md" and args.json
    args = cli_args(plugin, "history")
    assert args.path is None and not args.json
    args = cli_args(plugin, "restore", "SOUL.md", "--generation", "2", "--apply")
    assert args.generation == 2 and args.apply
    args = cli_args(plugin, "restore", "SOUL.md", "--generation", "2")
    assert not args.apply


def test_cli_history_json_sanitizes_remote_text(plugin, stub_engine, capsys):
    code = plugin.cli.handler(cli_args(plugin, "history", "--json"))
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert [e["path"] for e in data] == ["SOUL.md", "invalid-path"]
    assert all(e["device"] == "dev-a" for e in data)
    assert HOSTILE not in capsys.readouterr().out


def test_cli_history_human_output_names_the_fix(plugin, stub_engine, capsys):
    code = plugin.cli.handler(cli_args(plugin, "history"))
    assert code == 0
    out = capsys.readouterr().out
    assert "2 recoverable version(s)" in out
    assert "hermes roam restore <path> --generation N --apply" in out
    assert HOSTILE not in out
    assert "invalid-path" in out


def test_cli_history_empty_store(plugin, stub_engine, capsys):
    stub_engine.history = lambda path=None, *, limit=50: []
    code = plugin.cli.handler(cli_args(plugin, "history"))
    assert code == 0
    assert "no recoverable versions" in capsys.readouterr().out


def test_cli_restore_plan_only_without_apply(plugin, stub_engine, capsys):
    code = plugin.cli.handler(
        cli_args(plugin, "restore", "SOUL.md", "--generation", "2")
    )
    assert code == 0
    assert stub_engine.restore_calls == [("SOUL.md", 2, False)]
    out = capsys.readouterr().out
    assert "plan only" in out and "--apply" in out


def test_cli_restore_apply_reports_backup_and_publish(plugin, stub_engine, capsys):
    code = plugin.cli.handler(
        cli_args(plugin, "restore", "SOUL.md", "--generation", "2", "--apply")
    )
    assert code == 0
    assert stub_engine.restore_calls == [("SOUL.md", 2, True)]
    out = capsys.readouterr().out
    assert "restored SOUL.md from generation 2" in out
    assert "20261101-000000" in out
    assert "next `hermes roam push` publishes it" in out


def test_cli_restore_error_is_exit_1(plugin, stub_engine, capsys):
    def refuse(path, *, generation, apply=False):
        raise RoamError("generation 2 is not recoverable: no bundle of that generation exists")

    stub_engine.restore = refuse
    code = plugin.cli.handler(
        cli_args(plugin, "restore", "SOUL.md", "--generation", "2", "--apply")
    )
    assert code == 1
    assert "not recoverable" in capsys.readouterr().out


def test_slash_history_lists_and_points_to_the_terminal(plugin, stub_engine):
    out = plugin.commands.handle("history")
    assert "Roam history:" in out
    assert "2 recoverable version(s)" in out
    assert "hermes roam restore <path>" in out
    assert HOSTILE not in out and "invalid-path" in out


def test_slash_history_accepts_one_path(plugin, stub_engine):
    plugin.commands.handle("history SOUL.md")
    assert stub_engine.history_calls == [("SOUL.md", 50)]
    out = plugin.commands.handle("history a b")
    assert "at most one path" in out


def test_slash_has_no_restore_verb(plugin, stub_engine):
    out = plugin.commands.handle("restore SOUL.md")
    assert "unknown command 'restore'" in out
    assert not stub_engine.restore_calls


def test_slash_help_mentions_history(plugin):
    assert "history" in plugin.commands.handle("help")
