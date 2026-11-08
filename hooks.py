"""Lifecycle hooks: bounded background sync at session boundaries.

``on_session_end`` auto-pushes when enabled (debounced, in a daemon thread
with a watchdog); ``on_session_start`` checks the remote head in the
background and stashes a one-line notice; ``pre_llm_call`` surfaces that
notice once, on the first turn. Every callback catches its own errors, never
raises, and returns immediately — the heavy work runs in daemon threads.

R9 accounting: the auto-push never waits on another roam command's lock —
it takes the home lock non-blocking and simply skips when one is held
(debug log). The debounce timestamp is written only after a SUCCESSFUL
auto-push; a failed or killed auto-push leaves the ``pending_auto_push``
state flag so the next session start posts a one-line notice telling the
user to push now.

Honest bounds: a watchdog cannot stop a running thread, so the caps below
only decide WHEN the watchdog logs an overrun at WARNING — the work itself
is bounded by the operation deadlines (R8: one budget per user operation —
`operation_timeout`, default 900 s, for push/pull; 60 s for status, doctor
and diff; the git reachability probe is capped at 15 s and git data
transfers at the smaller of the remaining budget and
`git_transfer_timeout`, default 600 s), not by these caps. The README
states these numbers.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time

from . import settings

logger = logging.getLogger(__name__)

PUSH_CAP_SECONDS = 120
START_CAP_SECONDS = 15
_NOTICE_KEY = "pending_notice"
_DEBOUNCE_KEY = "last_auto_push_at"
_PENDING_PUSH_KEY = "pending_auto_push"
_PENDING_PUSH_NOTICE = (
    "Roam: the last automatic push did not finish — run `hermes roam push` "
    "(or `/roam push`) now to publish this device's changes."
)
# The only paths an auto-pull may ever install: shared skills, memories and
# SOUL.md, at the home root and inside every named profile. config.yaml is
# deliberately absent — it is never auto-pulled.
AUTO_PULL_PREFIXES = (
    "skills/",
    "memories/",
    "SOUL.md",
    "profiles/*/skills/",
    "profiles/*/memories/",
    "profiles/*/SOUL.md",
)


def _state() -> object | None:
    ctx = settings.context()
    return getattr(ctx, "state", None) if ctx is not None else None


def _spawn(target, cap: int) -> None:
    """Start target in a daemon thread; a watchdog logs an overrun at WARNING.

    A Python thread cannot be stopped from the outside, so the cap bounds
    nothing by itself — the real bound on the work is the operation
    deadlines. The WARNING names the overrun so operators see it in the logs.
    """
    # Run the work in a copy of the hook's context. Under Hermes 0.23 a CLI
    # chat is served by a per-home gateway, and agent.secret_scope binds the
    # profile's secrets in a ContextVar for the turn (fail closed while
    # multiplexing). A bare thread starts with an EMPTY context, so the
    # archive-key read raised UnscopedSecretError and every auto-push and
    # startup auto-pull failed silently. Carrying the context keeps the
    # exact scope Hermes installed for this session.
    context = contextvars.copy_context()
    worker = threading.Thread(
        target=context.run, args=(target,), daemon=True, name="roam-hook"
    )
    worker.start()

    def watch() -> None:
        worker.join(cap)
        if worker.is_alive():
            logger.warning(
                "roam background task overran its %ss watchdog cap and is "
                "still running; roam work is bounded by operation deadlines, "
                "not by this cap",
                cap,
            )

    threading.Thread(target=watch, daemon=True, name="roam-watchdog").start()


def on_session_end(*args, **kwargs) -> None:
    """Auto-push when enabled and the debounce elapsed; returns in ~0.1 s."""
    try:
        state = _state()
        if state is None:
            return
        conf = settings.load_settings()
        if not conf.auto_push:
            return
        now = time.time()
        last = float(state.get(_DEBOUNCE_KEY, 0) or 0)
        if now - last < conf.push_debounce_seconds:
            return
        # R9: the debounce timestamp is written only after a SUCCESSFUL
        # push. The pending flag goes up before the push runs, so a push that
        # is killed at process exit is still reported at the next start.
        state.set(_PENDING_PUSH_KEY, True)
        # The engine refuses to push a clean home on its own (skipped_reason),
        # so the dirty check happens inside the thread, not here.
        _spawn(_auto_push, cap=PUSH_CAP_SECONDS)
    except Exception:  # noqa: BLE001 - hooks never raise
        logger.debug("roam on_session_end skipped", exc_info=True)


def _auto_push() -> None:
    state = _state()
    try:
        engine = settings.build_engine()
        # R9: the auto-push never waits on the home lock — another roam
        # command that holds it wins, and the push is simply skipped.
        holder = getattr(engine, "lock_holder", None)
        if callable(holder) and engine.lock_holder() is not None:
            logger.debug(
                "roam auto-push skipped: another roam command holds the home lock"
            )
            return
        engine.push()
    except Exception:  # noqa: BLE001
        logger.debug(
            "roam auto-push failed; it stays pending and is reported at the "
            "next session start",
            exc_info=True,
        )
        return
    logger.info("roam auto-push finished")
    if state is not None:
        try:
            state.set(_DEBOUNCE_KEY, time.time())
            state.set(_PENDING_PUSH_KEY, False)
        except Exception:  # noqa: BLE001
            logger.debug("roam auto-push bookkeeping failed", exc_info=True)


def on_session_start(*args, **kwargs) -> None:
    """Check the remote head in the background; stash a notice when ahead."""
    try:
        state = _state()
        if state is None:
            return
        # R9: a previous auto-push that failed or was killed left a flag —
        # tell the user before anything else, then clear the flag. The
        # pending push outranks the head check this session; the check runs
        # again at the next session start after the user has pushed.
        if state.get(_PENDING_PUSH_KEY, False):
            state.set(_NOTICE_KEY, _PENDING_PUSH_NOTICE)
            state.set(_PENDING_PUSH_KEY, False)
            return
        conf = settings.load_settings()
        _spawn(lambda: _startup_check(conf, state), cap=START_CAP_SECONDS)
    except Exception:  # noqa: BLE001
        logger.debug("roam on_session_start skipped", exc_info=True)


def _ahead_status(engine):
    """status() only: the Status when the remote is verifiably ahead, else None.

    An unreachable backend is never reported as ahead (remote_generation None
    plus an error), and no other engine call is made from this check.
    """
    status = engine.status()
    if status.error or not isinstance(status.remote_generation, int):
        logger.debug(
            "roam startup check: no verifiable remote head (%s)", status.error or "none"
        )
        return None
    return status if status.remote_ahead else None


def _startup_check(conf, state) -> None:
    try:
        engine = settings.build_engine()
        status = _ahead_status(engine)
        if status is None:
            return
        if conf.auto_pull_shared:
            try:
                engine.pull(**settings.supported_kwargs(
                    engine,
                    "pull",
                    {
                        "apply": True,
                        "classes": None,
                        "dry_run": False,
                        "include_prefixes": list(AUTO_PULL_PREFIXES),
                        "overwrite_conflicts": False,
                    },
                ))
            except Exception:  # noqa: BLE001 - the notice below still fires
                logger.debug("roam auto-pull failed", exc_info=True)
            # status() only — no diff() round trip. A restricted pull never
            # advances the seen generation, so a still-ahead remote is the
            # honest signal that the full pull remains to be reviewed.
            status = _ahead_status(engine)
            if status is None:
                return
            state.set(_NOTICE_KEY, (
                "Roam: auto-pulled the remote skills, memories and SOUL.md; "
                "the remote is still ahead of this device — run `hermes roam "
                "pull` to review the plan, then `hermes roam pull --apply`."
            ))
            return
        ahead = max(1, status.remote_generation - status.local.last_seen_generation)
        state.set(_NOTICE_KEY, (
            f"Roam: the remote home is {ahead} generations ahead of this "
            "device — run `hermes roam pull` to review the plan, then "
            "`hermes roam pull --apply`."
        ))
    except Exception:  # noqa: BLE001
        logger.debug("roam startup check failed", exc_info=True)


def pre_llm_call(is_first_turn: bool = False, **kwargs) -> dict | None:
    """Surface a pending sync notice once, on the first turn only."""
    try:
        if not is_first_turn:
            return None
        state = _state()
        if state is None:
            return None
        notice = state.get(_NOTICE_KEY, "")
        if notice:
            state.set(_NOTICE_KEY, "")
            return {"context": str(notice)}
        return None
    except Exception:  # noqa: BLE001
        logger.debug("roam pre_llm_call skipped", exc_info=True)
        return None
