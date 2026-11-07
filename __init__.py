"""Hermes Roam — encrypted, host-scoped sync of the Hermes Agent home.

``register(ctx)`` wires the three tools, the three lifecycle hooks, the
``hermes roam`` CLI tree, the ``/roam`` slash command, and the bundled
roam-setup skill. The engine itself lives in the ``roam`` package and is
imported lazily, so this module imports cleanly even when engine pieces are
missing.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import cli, commands, hooks, schemas, settings, tools

logger = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).resolve().parent

_TOOLS = (
    ("roam_status", schemas.ROAM_STATUS, tools.roam_status),
    ("roam_push", schemas.ROAM_PUSH, tools.roam_push),
    ("roam_pull", schemas.ROAM_PULL, tools.roam_pull),
)
_HOOKS = ("on_session_start", "on_session_end", "pre_llm_call")
_SKILL_MD = _PLUGIN_DIR / "skills" / "roam-setup" / "SKILL.md"


def register(ctx) -> None:
    """Wire the plugin's tools, hooks, CLI, slash command and bundled skill."""
    settings.set_context(ctx)
    for name, schema, handler in _TOOLS:
        ctx.register_tool(name=name, toolset="hermes-roam", schema=schema, handler=handler)
    for hook in _HOOKS:
        ctx.register_hook(hook, getattr(hooks, hook))
    ctx.register_cli_command(
        name="roam",
        help="Sync the Hermes home across devices (encrypted, host-scoped)",
        setup_fn=cli.add_cli_args,
        handler_fn=cli.handler,
    )
    ctx.register_command(
        "roam",
        handler=commands.handle,
        description="Hermes home sync: status, push, pull apply, diff, devices, doctor",
        args_hint="[status|pull apply|push|diff|devices|doctor]",
    )
    if _SKILL_MD.exists():
        ctx.register_skill(
            "roam-setup",
            _SKILL_MD,
            description="Set up hermes-roam sync between two machines (key, "
                        "backend, init, doctor, push, pull)",
        )
    else:
        logger.warning("hermes-roam: bundled skill missing at %s", _SKILL_MD)
