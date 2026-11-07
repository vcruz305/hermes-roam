"""Tool handlers — the code that runs when the LLM calls each tool.

Every handler returns a JSON string, on success and on failure, and never
raises. R7: a BackendError message may embed remote-controlled text (backend
stderr relays, server error strings) and every head field is
remote-controlled — such strings are never relayed verbatim to the model or
the user: backend/unexpected errors become a fixed summary (the detail goes
to the log), and head-derived names and paths are displayed only when they
pass the fleet charsets, anything else becomes a fixed placeholder. The
JSON never instructs the model to repeat an error.
"""

from __future__ import annotations

import json
import logging
import re

from . import settings
from .settings import roam_module

logger = logging.getLogger(__name__)

_MAX_ERROR_CHARS = 300

# R6: the only charset a fleet device name may ever have; a name that does
# not match it can never be displayed by a surface.
_DEVICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

BACKEND_ERROR_SUMMARY = (
    "the remote backend failed or is unreachable; the detailed error is in "
    "the Hermes log — run `hermes roam doctor`"
)
UNEXPECTED_ERROR_SUMMARY = (
    "the operation failed with an unexpected error; the detail is in the Hermes log"
)


def safe_device_name(name: object) -> str:
    """Head/roster device names are remote-controlled text (R7): a surface
    displays one only when it matches the fleet charset (R6); anything else
    becomes a fixed placeholder that carries no remote text."""
    text = str(name)
    return text if _DEVICE_NAME_RE.match(text) else "invalid-device-name"


def safe_profile_name(name: object) -> str:
    """Session-archive profile names are remote-controlled text (R7): a
    surface displays one only when it passes the profile id grammar; anything
    else becomes a fixed placeholder that carries no remote text."""
    archive = roam_module("session_archive")
    text = str(name)
    return text if archive.is_valid_profile_name(text) else "invalid-profile-name"


def safe_relpath(path: object) -> str:
    """Head manifest paths are remote-controlled text (R7): a surface
    displays one only when it passes the head path rules (R6) — relative,
    no backslash, no '..' or '.' segment, no control characters, bounded
    segment and total length; anything else becomes a fixed placeholder."""
    text = str(path)
    if not text or len(text) > 1024 or text.startswith("/") or "\\" in text:
        return "invalid-path"
    for segment in text.split("/"):
        if (
            not segment
            or segment in (".", "..")
            or len(segment) > 255
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in segment)
        ):
            return "invalid-path"
    return text


def safe_session_id(value: object) -> str:
    """An archived session identifier is remote-sourced text read back from
    disk (0.4.1): a surface shows one only when it passes the strict
    archive charset; anything else becomes a fixed placeholder."""
    text = str(value)
    return text if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", text) else "invalid-session-id"


def safe_title(value: object, limit: int = 120) -> str:
    """An archived session title is remote-sourced text read back from disk
    (0.4.1): sanitized to one line and capped before any listing shows it."""
    return roam_module("textsafe").clean_line(value, limit)


def safe_error_text(exc: BaseException) -> str:
    """What a surface may say about an exception (R7).

    BackendError messages are the class that may embed remote-controlled
    text (a backend stderr relay or a server error string) — they are never
    relayed: fixed summary here, full detail in the log. Other RoamError
    messages are authored by this plugin (they name the fix and carry no
    remote text) and are shown. Anything else is an unexpected error and is
    summarized the same way. Surfaces never ask the model to repeat the text
    either way.
    """
    errors = roam_module("errors")
    if isinstance(exc, errors.BackendError):
        logger.error(
            "roam backend error (detail withheld from user/model surfaces): %s", exc
        )
        return BACKEND_ERROR_SUMMARY
    if isinstance(exc, errors.RoamError):
        return str(exc)[:_MAX_ERROR_CHARS]
    logger.error(
        "roam %s (detail withheld from user/model surfaces): %s", type(exc).__name__, exc
    )
    return UNEXPECTED_ERROR_SUMMARY


def safe_status_error(error: object) -> str:
    """Status.error carries whatever the backend layer raised — including
    remote-controlled text — so no surface relays it verbatim (R7)."""
    text = str(error)
    if not text:
        return ""
    if text in (BACKEND_ERROR_SUMMARY, UNEXPECTED_ERROR_SUMMARY):
        return text
    logger.error("roam backend error (detail withheld from user/model surfaces): %s", text)
    return BACKEND_ERROR_SUMMARY


def sanitize_status_dict(data: dict) -> dict:
    """A status dict that carries no remote-controlled text verbatim (R7):
    the error becomes the fixed summary, head-derived device names are
    charset-checked, and everything local (generations, counts, the local
    device roster entry) is kept as-is."""
    out = dict(data)
    out["error"] = safe_status_error(out.get("error", ""))
    out["remote_device"] = safe_device_name(out.get("remote_device", ""))
    for device in out.get("devices") or []:
        if isinstance(device, dict) and "name" in device:
            device["name"] = safe_device_name(device["name"])
    return out


def _sanitize_plan_dict(plan: dict) -> dict:
    """A plan dict without verbatim remote-controlled paths (R7): every path
    comes from the remote manifest, so each one is charset-checked and
    hostile paths become the fixed placeholder. R11 fields (configs,
    config_conflicts, unchanged) pass through the same check — the plan the
    model sees must name them, never relay them raw."""
    out = dict(plan)
    for group in ("added", "changed", "removed"):
        scopes = out.get(group)
        if isinstance(scopes, dict):
            out[group] = {
                scope: [safe_relpath(path) for path in paths or []]
                for scope, paths in scopes.items()
            }
    for key in ("config_conflicts", "unchanged"):
        if key in out:
            out[key] = [safe_relpath(path) for path in out[key] or []]
    if "configs" in out:
        # 0.4.2: each entry is a [relpath, mode] pair ("merge" or
        # "first_pull"); the path is remote-manifest-derived so it is
        # charset-checked, the mode is engine-authored.
        out["configs"] = [
            [safe_relpath(entry[0]), entry[1]]
            for entry in out["configs"] or []
            if isinstance(entry, (list, tuple)) and len(entry) == 2
        ]
    return out


def sanitize_diff_dict(data: dict) -> dict:
    """A diff/preview dict without verbatim remote-controlled paths (R7);
    same shape and rules as a pull plan dict."""
    return _sanitize_plan_dict(data)


def sanitize_pull_dict(data: dict) -> dict:
    """A pull result dict that carries no remote-controlled text verbatim
    (R7) while keeping every count and the R11 plan fields (config merges,
    config conflicts, unchanged files) visible to the model."""
    out = dict(data)
    plan = out.get("plan")
    if isinstance(plan, dict):
        out["plan"] = _sanitize_plan_dict(plan)
    for key in ("conflicts", "skipped_local", "plugins_to_install"):
        if key in out:
            out[key] = [safe_relpath(path) for path in out[key] or []]
    return out


def sanitize_push_dict(data: dict) -> dict:
    """A push result dict that carries no remote-controlled text verbatim
    (R7): the file list comes from the shared manifest and the bundle key
    embeds a device name, so both are charset-checked for display."""
    out = dict(data)
    for key in ("files",):
        if key in out:
            out[key] = [safe_relpath(path) for path in out[key] or []]
    if "bundle_key" in out:
        out["bundle_key"] = safe_relpath(out["bundle_key"])
    return out


def sanitize_history_list(entries: object) -> list:
    """0.4 history entries carry remote-controlled paths and device names
    (R7): every one is charset-checked before it reaches the user or the
    model; counts and digests stay untouched."""
    out: list = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        item = dict(entry)
        item["path"] = safe_relpath(item.get("path", ""))
        item["device"] = safe_device_name(item.get("device", ""))
        out.append(item)
    return out


def sanitize_restore_dict(data: dict) -> dict:
    """A 0.4 restore result dict without verbatim remote-controlled text
    (R7): the restored path and the pushing device are charset-checked."""
    out = dict(data)
    out["path"] = safe_relpath(out.get("path", ""))
    out["device"] = safe_device_name(out.get("device", ""))
    return out


def _fail(exc: Exception) -> str:
    return json.dumps({"error": safe_error_text(exc), "kind": type(exc).__name__})


def _classes(raw: object) -> tuple[object, str]:
    """Map the schema's class names to Scope values; (classes, error).

    0.4.1: the read-only session archive is always among the pull
    defaults — reading what another device published needs no opt-in — so
    SESSIONS is added whatever the caller picked."""
    model = roam_module("model")
    if raw in (None, ""):
        return None, ""
    if not isinstance(raw, list):
        return None, "classes must be a list of shared, sessions, cron"
    allowed = {
        "shared": model.Scope.SHARED,
        "sessions": model.Scope.SESSIONS,
        "cron": model.Scope.CRON,
    }
    picked = {model.Scope.SESSIONS}
    for item in raw:
        scope = allowed.get(str(item).strip().lower())
        if scope is None:
            return None, f"unknown scope class {item!r}; use shared, sessions or cron"
        picked.add(scope)
    return picked, ""


def roam_status(args: dict, **kwargs) -> str:
    """Fleet and generation status of this device's Hermes home."""
    try:
        status = settings.build_engine().status()
        data = sanitize_status_dict(status.to_dict())
        if status.remote_generation is None and status.error:
            # Never claim the remote is ahead when it could not be reached,
            # and never relay the backend's error text (R7).
            data["remote_ahead"] = False
            return json.dumps({
                "error": data["error"],
                "kind": "BackendError",
                "status": data,
                "fix": (
                    "do not pull and do not claim the remote is ahead; the "
                    "remote backend is unreachable — the user can run "
                    "`hermes roam doctor` to diagnose it"
                ),
            })
        return json.dumps({"status": data})
    except Exception as exc:  # noqa: BLE001 - handlers never raise
        return _fail(exc)


def roam_push(args: dict, **kwargs) -> str:
    """Upload a new encrypted generation of the shared home."""
    try:
        result = settings.build_engine().push(
            force=bool(args.get("force")),
            dry_run=bool(args.get("dry_run")),
        )
        return json.dumps({"push": sanitize_push_dict(result.to_dict())})
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


def roam_pull(args: dict, **kwargs) -> str:
    """Download (and with apply=true, install) the newest shared generation."""
    try:
        if bool(args.get("apply")) and not settings.allow_agent_apply():
            # The user did not opt into model-initiated installs: the
            # journaled rewrite of the home is their decision, not the model's.
            return json.dumps({
                "error": (
                    "apply=true is not allowed for the model on this device: "
                    "config key allow_agent_apply is false. Ask the user to "
                    "run `/roam pull apply` or `hermes roam pull --apply` "
                    "themselves; they can enable allow_agent_apply in the "
                    "plugin settings to change that."
                ),
                "kind": "PermissionError",
                "fix": (
                    "ask the user to run `/roam pull apply` or "
                    "`hermes roam pull --apply`; do not retry apply=true"
                ),
            })
        classes, bad = _classes(args.get("classes"))
        if bad:
            return json.dumps({"error": bad, "kind": "ValueError"})
        result = settings.build_engine().pull(
            apply=bool(args.get("apply")),
            classes=classes,
            dry_run=bool(args.get("dry_run")),
        )
        return json.dumps({"pull": sanitize_pull_dict(result.to_dict())})
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)
