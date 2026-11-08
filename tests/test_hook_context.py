"""0.5.6: hook work must run in the hook caller's context.

Hermes 0.23 serves CLI chats from a per-home gateway that binds profile
secrets in a ContextVar (agent.secret_scope) and fails closed outside it. A
plain thread starts with an empty context, so the auto-push could not read
ROAM_ARCHIVE_KEY and every automatic sync failed silently (found live)."""

import contextvars
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import load_plugin_package  # noqa: E402

SCOPE: contextvars.ContextVar = contextvars.ContextVar("scope", default=None)


def test_spawned_hook_work_sees_the_callers_context_vars():
    plugin = load_plugin_package()
    seen = {}
    done = threading.Event()

    def work():
        seen["scope"] = SCOPE.get()
        done.set()

    token = SCOPE.set({"ROAM_ARCHIVE_KEY": "bound-for-this-turn"})
    try:
        plugin.hooks._spawn(work, cap=5)
    finally:
        SCOPE.reset(token)
    assert done.wait(5)
    assert seen["scope"] == {"ROAM_ARCHIVE_KEY": "bound-for-this-turn"}
