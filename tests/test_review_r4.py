"""Lane r4 adversarial review: plugin surface, catalog compliance, live behaviour.

Each test asserts the RULLED behaviour for one review finding (the original
finding is described in the comment above the test). Live command transcripts
for the findings that need real Hermes are committed in review-r4-transcript.md
next to this file.
"""

from __future__ import annotations

import fnmatch as fnmatch_mod
import json
import threading
import time
from types import SimpleNamespace

import fake_engine as fake_engine_mod
import pytest
from conftest import load_plugin_package

import roam.errors as errors
import roam.inventory as inventory
import roam.scope as scope

KEY = 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8='  # 32 real-entropy bytes, base64 (weak keys are rejected)


class FakeState:
    def __init__(self) -> None:
        self.store: dict = {}

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value):
        self.store[key] = value


class FakeCtx:
    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self.state = FakeState()

    def get_config(self, key, default=None):
        return self.config.get(key, default)


@pytest.fixture()
def plugin():
    return load_plugin_package()


@pytest.fixture()
def key(monkeypatch):
    monkeypatch.setenv("ROAM_ARCHIVE_KEY", KEY)
    return KEY


def real_engine(plugin, monkeypatch, tmp_path, *, config=None):
    """A real roam Engine on a tiny throwaway home with a local backend."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "skills" / "x").mkdir(parents=True)
    (home / "skills" / "x" / "SKILL.md").write_text("skill", encoding="utf-8")
    (home / "SOUL.md").write_text("soul", encoding="utf-8")
    ctx = FakeCtx(config or {
        "backend": "local",
        "local_dir": str(tmp_path / "remote"),
        "device_name": "r4-test",
    })
    plugin.settings.set_context(ctx)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return plugin.settings.build_engine()


# ---------------------------------------------------------------------------
# Finding: status with an unreachable backend crashes with a raw TypeError
# (live-proven with the git backend on a blackholed IP: exit 1, message
# "roam: unsupported operand type(s) for -: 'NoneType' and 'int'").
# RULING (pre-agreed API): on backend error, remote_ahead=False,
# remote_generation=None, error set — never True on error.
# RULING (round 3, R7): the backend error text itself may be
# remote-controlled (backend stderr relay, server error string), so no
# surface relays it verbatim any more: every surface prints the fixed
# summary and logs the detail. These tests were rewritten to assert that.


def test_status_unreachable_backend_reports_error_not_ahead(
    plugin, key, monkeypatch, tmp_path
):
    engine = real_engine(plugin, monkeypatch, tmp_path)

    def unreachable(*, deadline=None):  # noqa: ARG001
        raise errors.BackendError("git fetch timed out after 120s")

    engine.backend.get_head = unreachable
    status = engine.status()
    assert status.error == "git fetch timed out after 120s"  # engine-level truth
    assert status.remote_generation is None
    assert status.remote_ahead is False  # never True on a backend error


def test_cli_status_reports_unreachable_backend_and_exits_1(
    plugin, key, monkeypatch, tmp_path, capsys, caplog
):
    import logging

    engine = real_engine(plugin, monkeypatch, tmp_path)

    def unreachable(*, deadline=None):  # noqa: ARG001
        raise errors.BackendError("s3 request failed: connect timeout")

    engine.backend.get_head = unreachable
    monkeypatch.setattr(plugin.settings, "build_engine", lambda: engine)
    args = plugin.cli.make_parser().parse_args(["status"])
    with caplog.at_level(logging.ERROR):
        rc = plugin.cli.handler(args)
    out = capsys.readouterr().out
    assert rc == 1
    # RULING R7: fixed summary on the surface, detail in the log.
    assert "remote unreachable" in out
    assert plugin.tools.BACKEND_ERROR_SUMMARY in out
    assert "s3 request failed" not in out
    assert "generations ahead" not in out
    assert "NoneType" not in out
    assert "s3 request failed: connect timeout" in caplog.text


def test_slash_status_surfaces_backend_error(plugin, key, monkeypatch, tmp_path):
    """The slash command reports the failure without relaying the backend's
    error text (R7)."""
    engine = real_engine(plugin, monkeypatch, tmp_path)

    def unreachable(*, deadline=None):  # noqa: ARG001
        raise errors.BackendError("git fetch timed out after 120s")

    engine.backend.get_head = unreachable
    monkeypatch.setattr(plugin.settings, "build_engine", lambda: engine)
    out = plugin.commands.handle("status")
    assert "remote unreachable" in out
    assert plugin.tools.BACKEND_ERROR_SUMMARY in out
    assert "git fetch timed out" not in out
    assert "generations ahead" not in out


def test_tool_status_surfaces_unreachable_error_never_ahead(
    plugin, key, monkeypatch, tmp_path
):
    """The roam_status tool reports the unreachable backend with the fixed
    summary, never relays the backend text, never tells the model the remote
    is ahead, and never asks the model to repeat an error."""
    engine = real_engine(plugin, monkeypatch, tmp_path)

    def unreachable(*, deadline=None):  # noqa: ARG001
        raise errors.BackendError("s3 request failed: connect timeout")

    engine.backend.get_head = unreachable
    monkeypatch.setattr(plugin.settings, "build_engine", lambda: engine)
    out = json.loads(plugin.tools.roam_status({}))
    assert out["error"] == plugin.tools.BACKEND_ERROR_SUMMARY  # RULING R7
    assert "s3 request failed" not in json.dumps(out)
    assert out["status"]["remote_ahead"] is not True
    assert out["status"]["remote_generation"] is None
    assert "do not claim the remote is ahead" in out["fix"]
    assert "tell the user" not in out["fix"]  # never asks the model to repeat it


# ---------------------------------------------------------------------------
# Finding: dry_run claimed to report the bundle contents but never listed
# them; no command or tool could show which files a push would upload.
# RULING: dry runs list the files (the CLI caps the list at 40 lines plus
# "... N more"); the tool returns them in JSON.


def test_push_dry_run_lists_bundle_contents(plugin, monkeypatch, capsys):
    engine = fake_engine_mod.FakeEngine()
    engine.push_files = [f"skills/d{i}/SKILL.md" for i in range(50)]
    monkeypatch.setattr(plugin.settings, "build_engine", lambda: engine)

    args = plugin.cli.make_parser().parse_args(["push", "--dry-run"])
    rc = plugin.cli.handler(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "would push skills/d0/SKILL.md" in out
    assert "... 10 more" in out  # 40 lines shown, the rest counted

    tool_out = json.loads(plugin.tools.roam_push({"dry_run": True}))
    assert "skills/d0/SKILL.md" in tool_out["push"]["files"]
    assert len(tool_out["push"]["files"]) == 50


# ---------------------------------------------------------------------------
# Regression (0.4.0 polish): on an unchanged home the engine answers the dry
# run with skipped_reason="nothing to push" and dry_run=True; the CLI checked
# dry_run first and printed a would-push summary of the empty bundle
# ("would push N files, 0 bytes") instead of the honest skip outcome.

def test_push_dry_run_on_unchanged_home_prints_nothing_to_push(plugin, monkeypatch, capsys):
    engine = fake_engine_mod.FakeEngine()
    engine.push_skipped_reason = "nothing to push"
    engine.push_files = []
    monkeypatch.setattr(plugin.settings, "build_engine", lambda: engine)

    args = plugin.cli.make_parser().parse_args(["push", "--dry-run"])
    rc = plugin.cli.handler(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "nothing to push" in out, (
        "a dry run on an unchanged home must print the skip outcome"
    )
    assert "would push" not in out and "0 bytes" not in out, (
        "a skipped dry run must not describe an empty bundle as a would-push"
    )


# ---------------------------------------------------------------------------
# Finding: the scope classifier multiplies every walked path by every rule
# segment (fnmatch per segment); a realistic home takes tens of seconds of
# pure CPU and the session hooks always overrun their watchdog caps.


def test_scope_classification_is_quadratic_in_paths_and_rules(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for d in range(60):
        (home / "outputs" / f"d{d}").mkdir(parents=True)
        for f in range(20):
            (home / "outputs" / f"d{d}" / f"f{f}.json").write_text("x", encoding="utf-8")
    walked = 60 * 20  # files; directories are classified too

    calls = {"n": 0}
    real = fnmatch_mod.fnmatchcase

    def counting(pattern, name):
        calls["n"] += 1
        return real(pattern, name)

    monkeypatch.setattr(scope.fnmatch, "fnmatchcase", counting)
    rules = scope.Rules.default()
    entries = inventory.walk_home(home, rules, include={scope.Scope.SHARED})
    assert entries == []  # outputs/** is LOCAL: never listed, never traversed
    # every path still classifies exactly — pruning must not cut corners
    for d in range(60):
        for f in range(20):
            assert scope.classify_path(f"outputs/d{d}/f{f}.json", rules) is scope.Scope.LOCAL
    # linear: a small constant per path, not rules x segments (the old code
    # made >150 fnmatch calls per path; 39,204,300 on the realistic home)
    assert calls["n"] <= 2 * walked, (
        f"{calls['n']} fnmatch calls for {walked} paths: classification is "
        "still super-linear in paths and rules"
    )


# ---------------------------------------------------------------------------
# Finding: the hook watchdog caps did not bound the background work — the
# worker thread kept running long after the watchdog "abandoned" it, and the
# README's "15 s / 120 s" bounds were false. RULING: honest bounds. A Python
# thread cannot be stopped, so the watchdog logs the overrun at WARNING and
# says the work is bounded by the backend deadlines (git 60 s total,
# S3 2x30 s), not by the cap; the README states the real numbers.


def test_on_session_start_watchdog_logs_overrun_honestly(plugin, monkeypatch, caplog):
    ctx = FakeCtx()
    plugin.settings.set_context(ctx)
    monkeypatch.setattr(
        plugin.settings, "load_settings",
        lambda: SimpleNamespace(auto_pull_shared=False, push_debounce_seconds=0),
    )

    started = threading.Event()

    class SlowEngine:
        def status(self):
            started.set()
            time.sleep(3.0)
            return None

    monkeypatch.setattr(plugin.settings, "build_engine", lambda: SlowEngine())
    monkeypatch.setattr(plugin.hooks, "START_CAP_SECONDS", 1)

    t0 = time.monotonic()
    plugin.hooks.on_session_start()
    returned = time.monotonic() - t0
    assert returned < 0.5  # the hook itself returns immediately

    assert started.wait(5)  # the worker started
    workers = [t for t in threading.enumerate() if t.name == "roam-hook"]
    assert workers and workers[0].is_alive()  # threads cannot be stopped
    workers[0].join(5)
    # wait for the watchdog thread's record instead of a fixed grace sleep:
    # under load the log write can lag the join by more than 0.2 s
    wait_deadline = time.monotonic() + 5.0
    overrun: list[str] = []
    while time.monotonic() < wait_deadline and not overrun:
        overrun = [
            message
            for message in (r.getMessage() for r in caplog.records)
            if "overran its 1s watchdog cap" in message
        ]
        time.sleep(0.05)
    messages = [record.getMessage() for record in caplog.records]
    assert overrun, messages
    # the log is honest: it says the work continues and names the real bound.
    # RULING (round 3, R8): the bound is the per-operation deadline budget
    # (operation_timeout / git_transfer_timeout), not per-backend constants.
    assert "still running" in overrun[0]
    assert "operation deadlines" in overrun[0]
    assert not [m for m in messages if "abandoning" in m]  # the old lie is gone


# ---------------------------------------------------------------------------
# Finding: `hermes roam push` without init printed "run hermes roam init
# first" and then pushed anyway. RULING: refuse with exit 1.


def test_cli_push_refuses_when_not_initialised(plugin, key, monkeypatch, tmp_path, capsys):
    engine = real_engine(plugin, monkeypatch, tmp_path)  # home has no roam/state.json
    monkeypatch.setattr(plugin.settings, "build_engine", lambda: engine)
    args = plugin.cli.make_parser().parse_args(["push"])
    rc = plugin.cli.handler(args)
    out = capsys.readouterr().out
    assert rc == 1
    assert "run `hermes roam init` first" in out


def test_cli_push_without_init_never_reaches_the_engine(plugin, monkeypatch, capsys):
    engine = fake_engine_mod.FakeEngine()
    engine.home = fake_engine_mod.Path("/")  # no roam/state.json
    monkeypatch.setattr(plugin.settings, "build_engine", lambda: engine)
    args = plugin.cli.make_parser().parse_args(["push"])
    rc = plugin.cli.handler(args)
    out = capsys.readouterr().out
    assert rc == 1
    assert "`hermes roam init` first" in out
    assert not [c for c in engine.calls if c[0] == "push"]


def test_tool_push_maps_the_engine_init_refusal(plugin, monkeypatch):
    """Post-f2 the engine itself refuses; the tool maps it to error JSON."""
    engine = fake_engine_mod.FakeEngine()
    engine.home = fake_engine_mod.Path("/")  # the fake refuses like the 0.2.0 engine
    monkeypatch.setattr(plugin.settings, "build_engine", lambda: engine)
    out = json.loads(plugin.tools.roam_push({}))
    assert out["kind"] == "RoamError"
    assert "`hermes roam init` first" in out["error"]


# ---------------------------------------------------------------------------
# Finding: /roam argument parsing was asymmetric — pull validated its extra
# words, push silently ignored them. RULING: validate; accept force and
# dry/dry-run.


def test_slash_push_validates_and_maps_its_words(plugin, monkeypatch):
    engine = fake_engine_mod.FakeEngine()
    monkeypatch.setattr(plugin.settings, "build_engine", lambda: engine)
    out = plugin.commands.handle("push force")
    assert ("push", {"force": True, "dry_run": False}) in engine.calls
    out = plugin.commands.handle("push dry-run")
    assert ("push", {"force": False, "dry_run": True}) in engine.calls

    before = len(engine.calls)
    out = plugin.commands.handle("push extra")
    assert "takes no other arguments" in out
    assert len(engine.calls) == before  # rejected words never reach the engine


def test_slash_push_refuses_without_init(plugin, monkeypatch):
    engine = fake_engine_mod.FakeEngine()
    engine.home = fake_engine_mod.Path("/")
    monkeypatch.setattr(plugin.settings, "build_engine", lambda: engine)
    out = plugin.commands.handle("push")
    assert "no local roam state" in out
    assert "`hermes roam init` first" in out
    assert not [c for c in engine.calls if c[0] == "push"]


def test_slash_pull_rejects_extra_arguments(plugin, monkeypatch):
    engine = fake_engine_mod.FakeEngine()
    monkeypatch.setattr(plugin.settings, "build_engine", lambda: engine)
    out = plugin.commands.handle("pull apply extra")
    assert "takes no other arguments" in out
    assert not [c for c in engine.calls if c[0] == "pull"]


# ---------------------------------------------------------------------------
# Finding: roam_pull(apply=true) was not gated — the model alone could decide
# to rewrite the home. RULING: gate behind allow_agent_apply (default false);
# the refusal must tell the model to ask the user to run the pull themselves.


def test_tool_pull_apply_is_gated(plugin, monkeypatch):
    engine = fake_engine_mod.FakeEngine()
    ctx = FakeCtx()
    plugin.settings.set_context(ctx)
    monkeypatch.setattr(plugin.settings, "build_engine", lambda: engine)

    out = json.loads(plugin.tools.roam_pull({"apply": True}))
    assert out["kind"] == "PermissionError"
    assert "allow_agent_apply" in out["error"]
    assert "/roam pull apply" in out["error"]
    assert "hermes roam pull --apply" in out["error"]
    assert not [c for c in engine.calls if c[0] == "pull"]  # nothing ran

    ctx.config["allow_agent_apply"] = True  # the user opted in
    out = json.loads(plugin.tools.roam_pull({"apply": True}))
    pull_calls = [c for c in engine.calls if c[0] == "pull"]
    assert pull_calls and pull_calls[0][1]["apply"] is True
    assert "pull" in out
