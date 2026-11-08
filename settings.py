"""Settings resolution for hermes-roam: config namespace + secrets -> RoamSettings.

Everything here runs inside Hermes (register() stored the PluginContext) or in
tests without Hermes; every Hermes import is lazy with a documented fallback.
"""

from __future__ import annotations

import dataclasses
import importlib
import logging
import os
import re
import socket
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ctx: Any = None  # PluginContext stored by register(); None outside Hermes

_BACKENDS = {"s3", "git", "local"}
_SECRET_FIELDS = ("archive_key", "s3_access_key_id", "s3_secret_access_key")
_TRUE = {"1", "true", "yes", "on"}

# Canonical named-profile id grammar (Hermes's PROFILE_ID_RE): a home whose
# last two segments are profiles/<id> is a profile home of the parent root.
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def set_context(ctx: Any) -> None:
    """Store the PluginContext handed to register()."""
    global _ctx
    _ctx = ctx


def context() -> Any:
    """The stored PluginContext, or None outside Hermes."""
    return _ctx


def roam_module(name: str) -> Any:
    """Import ``roam.<name>`` with one module identity.

    Prefer the top-level package when the repository root is on sys.path
    (tests, source checkout); fall back to this plugin package's ``roam``
    subpackage, which is how Hermes imports a directory plugin. Keeping one
    identity matters: exception classes must be the same objects the rest of
    the repository raises.
    """
    try:
        return importlib.import_module(f"roam.{name}")
    except ImportError:
        return importlib.import_module(f"{__package__}.roam.{name}")


def _get_secret(name: str) -> str:
    """Read one secret by env name. Inside Hermes this goes through
    ``agent.secret_scope.get_secret``; this plugin never reads ``.env``,
    ``auth.json`` or any other credential file itself. Only when Hermes is
    absent entirely (tests, standalone demos) does the ImportError branch
    fall back to the plain process environment.
    """
    try:
        from agent.secret_scope import get_secret
    except ImportError:
        return os.environ.get(name, "")
    value = get_secret(name)
    return value or ""


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUE
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _deadline(value: Any, default: float) -> float:
    """A deadline setting: a value below 1 s would disable the deadline, so
    it is invalid and reverts to the ruled default (an excessive value is
    valid and is clamped by the caller against the operation budget)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 1.0 else default


# R8 (round 3): one deadline budget per user operation. The three ruled
# fields are owned by RoamSettings itself; these are the ruled defaults the
# clamps below enforce on the loaded object.
_OPERATION_TIMEOUT_DEFAULT = 900.0
_GIT_TRANSFER_TIMEOUT_DEFAULT = 600.0
_BACKUP_KEEP_DEFAULT = 5


def load_settings() -> Any:
    """Build RoamSettings from the plugin config namespace plus secrets.

    In a PROFILE context (the active home is <root>/profiles/<name>) every
    settings value comes from the ROOT home's config.yaml — the sync
    settings are fleet-wide, not per-profile — and the profile's own
    config.yaml is never consulted for them."""
    model = roam_module("model")
    errors = roam_module("errors")
    if _ctx is None:
        raise errors.ConfigError(
            "hermes-roam is not registered: run it inside Hermes via `hermes roam` or /roam"
        )
    root = roam_home()
    in_profile_context = root != hermes_home()

    def cfg(key: str, default: Any) -> Any:
        if in_profile_context:
            return _root_config_settings_value(key, default)
        value = _ctx.get_config(key, default)
        return default if value is None else value

    defaults = model.RoamSettings()
    values: dict[str, Any] = {}
    for field in dataclasses.fields(model.RoamSettings):
        if field.name in _SECRET_FIELDS:
            continue
        default = getattr(defaults, field.name)
        raw = cfg(field.name, default)
        if isinstance(default, bool):
            values[field.name] = _as_bool(raw, default)
        elif isinstance(default, float):
            values[field.name] = _as_float(raw, default)
        elif isinstance(default, int):
            values[field.name] = _as_int(raw, default)
        else:
            values[field.name] = str(raw)
    backend = values["backend"].strip().lower()
    if backend not in _BACKENDS:
        raise errors.ConfigError(
            f"roam backend must be one of {', '.join(sorted(_BACKENDS))}, not {values['backend']!r}"
        )
    if backend == "s3" and not values["s3_bucket"]:
        raise errors.ConfigError(
            "backend s3 needs a bucket: set plugins.entries.hermes-roam.settings.s3_bucket"
        )
    if backend == "git" and not values["git_remote"]:
        raise errors.ConfigError(
            "backend git needs a remote URL: set plugins.entries.hermes-roam.settings.git_remote"
        )
    if backend == "local" and not values["local_dir"]:
        raise errors.ConfigError(
            "backend local needs a directory: set plugins.entries.hermes-roam.settings.local_dir"
        )
    if not values["device_name"].strip():
        values["device_name"] = socket.gethostname().split(".")[0] or "device"
    values["push_debounce_seconds"] = max(0, values["push_debounce_seconds"])
    values["max_bundle_bytes"] = max(1, values["max_bundle_bytes"])
    # 0.4.1: the session archive cap (newest transcripts first). A zero or
    # negative cap would disable the cap, not the archive, so it reverts to
    # the ruled default.
    values["sessions_max_mb"] = max(
        1, _as_int(values["sessions_max_mb"], model.RoamSettings().sessions_max_mb)
    )
    archive_raw = _get_secret(model.SECRET_ARCHIVE_KEY)
    if not archive_raw:
        raise errors.ConfigError(
            f"{model.SECRET_ARCHIVE_KEY} is not set. Generate it with "
            'python -c "import secrets;print(secrets.token_urlsafe(32))" '
            "and put it in the active profile's .env; every device must use the same key."
        )
    try:
        values["archive_key"] = model.decode_key(archive_raw)
    except ValueError as exc:
        raise errors.ConfigError(str(exc)) from exc
    # S3 credentials are optional at load time (other backends do not use
    # them); the S3 backend and `hermes roam doctor` check them at runtime.
    values["s3_access_key_id"] = _get_secret(model.SECRET_S3_ACCESS_KEY_ID)
    values["s3_secret_access_key"] = _get_secret(model.SECRET_S3_SECRET_ACCESS_KEY)
    settings_obj = model.RoamSettings(**values)
    # R8 (round 3): clamp the deadline/retention fields the dataclass owns:
    # a zero/negative operation budget or transfer cap would disable the
    # deadlines entirely, a transfer cap above the operation budget is
    # meaningless, and keeping no backups would make recovery impossible.
    clamped = {
        "operation_timeout": _deadline(
            settings_obj.operation_timeout, _OPERATION_TIMEOUT_DEFAULT,
        ),
        "git_transfer_timeout": _deadline(
            settings_obj.git_transfer_timeout,
            _GIT_TRANSFER_TIMEOUT_DEFAULT,
        ),
        "backup_keep": max(1, settings_obj.backup_keep),
    }
    # a transfer cap above the operation budget is meaningless
    clamped["git_transfer_timeout"] = min(
        clamped["git_transfer_timeout"], clamped["operation_timeout"],
    )
    if any(getattr(settings_obj, name) != value
           for name, value in clamped.items()):
        settings_obj = dataclasses.replace(settings_obj, **clamped)
    return settings_obj


def allow_agent_apply() -> bool:
    """Whether the model may run a pull --apply by itself.

    Config key ``allow_agent_apply`` (default false). While it is false the
    roam_pull tool refuses apply=true and the model must ask the user to run
    ``/roam pull apply`` or ``hermes roam pull --apply`` in person; the
    journaled install is the user's decision, not the model's. Like every
    sync setting it is read from the ROOT home's config in a profile
    context.
    """
    if _ctx is None:
        return False
    if roam_home() != hermes_home():
        return _as_bool(_root_config_settings_value("allow_agent_apply", False), False)
    return _as_bool(_ctx.get_config("allow_agent_apply", False), False)


def supported_kwargs(engine: Any, method: str, kwargs: dict) -> dict:
    """Filter ``kwargs`` down to what ``engine.<method>`` actually accepts.

    The pre-agreed 0.2.0 engine API (pull ``overwrite_conflicts``, the
    conflicts/skipped_local/recovered result fields) lands with the engine
    lane; surfaces pass the new flags only once the engine takes them, so
    this tree stays green before that merge and fully wired after it.
    """
    import inspect

    try:
        parameters = inspect.signature(getattr(engine, method)).parameters
    except (AttributeError, TypeError, ValueError):
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return dict(kwargs)
    return {key: value for key, value in kwargs.items() if key in parameters}


def hermes_home() -> Path:
    """Active Hermes home: Hermes's own resolver, else $HERMES_HOME, else ~/.hermes."""
    try:
        from hermes_cli.config import get_hermes_home

        return Path(get_hermes_home())
    except ImportError:
        env = os.environ.get("HERMES_HOME")
        if env:
            return Path(env)
        return Path.home() / ".hermes"


def _is_profile_home(home: Path) -> bool:
    """Whether ``home`` has the exact shape <root>/profiles/<id> — decided by
    path structure ONLY, exactly like a Hermes profile home is laid out on
    disk (``<id>`` matches the canonical profile id grammar)."""
    return bool(
        home.parent.name == "profiles" and _PROFILE_ID_RE.match(home.name) is not None
    )


def sync_root(home: Path) -> Path:
    """The ROOT home roam always operates on: ``home`` itself, or the parent
    root when ``home`` is a profile home (<root>/profiles/<name>).

    Roam is a fleet-wide, whole-tree sync: one store holds the root home plus
    every profile under it with profile-relative paths, so a profile context
    must never become the sync root — that would publish profile files as
    top-level paths and mix profiles into one namespace."""
    home = Path(home)
    if _is_profile_home(home):
        return home.parent.parent
    return home


def active_profile_name(home: Path) -> str:
    """The profile the active home serves: 'default' for a root home, the
    profile id for a profile home (path structure only)."""
    home = Path(home)
    return home.name if _is_profile_home(home) else "default"


def roam_home() -> Path:
    """The ROOT home for every roam surface: the active home resolved to its
    root (a profile context maps up to <root> so the whole tree — root plus
    every profile — is covered by one store and one lock)."""
    return sync_root(hermes_home())


def _root_config_raw(path: Path) -> dict:
    """Read one config.yaml EXACTLY as written through Hermes's own raw
    reader (``hermes_cli.config.read_user_config_raw``); {} when Hermes is
    unavailable (tests, standalone demos). Never raises."""
    try:
        from hermes_cli.config import read_user_config_raw

        return read_user_config_raw(path) or {}
    except Exception:  # noqa: BLE001 - a missing reader is an empty config
        logger.debug("hermes_cli.config raw reader unavailable", exc_info=True)
        return {}


def _root_config_settings_value(key: str, default: Any) -> Any:
    """One plugin settings value from the ROOT home's config.yaml.

    The sync settings are fleet-wide, not per-profile: a profile context
    must read them from the root home's config (the store, the device name
    and the opt-ins are configured once at the root), never from the
    profile's own config.yaml. The value lives under
    ``plugins.entries.hermes-roam.settings`` exactly where
    ``PluginContext.get_config`` reads it for the active home."""
    root = roam_home()

    def dig(node: Any, name: str) -> dict:
        child = node.get(name) if isinstance(node, dict) else None
        return child if isinstance(child, dict) else {}

    entry = dig(dig(dig(_root_config_raw(root / "config.yaml"), "plugins"), "entries"), "hermes-roam")
    for tree in ("settings", "config"):  # settings first, legacy config subtree
        values = entry.get(tree)
        if isinstance(values, dict) and key in values:
            return values[key]
    return default


def profile_sync_state(root: Path | None = None) -> list[dict]:
    """Every profile of the ROOT home with its effective sync state:
    [{'profile': name, 'synced': bool}]. 'default' is the root home itself.

    The verdict is this device's own scope policy (roam/rules.yaml included):
    a profile excluded by a local: rule reports synced=False, and every
    surface that names the covered tree says so."""
    scope = roam_module("scope")
    home = sync_root(Path(root)) if root is not None else roam_home()
    rules = scope.load_rules(home)
    names = ["default"]
    profiles_dir = home / "profiles"
    if profiles_dir.is_dir():
        names.extend(
            entry.name
            for entry in sorted(profiles_dir.iterdir(), key=lambda item: item.name)
            if entry.is_dir() and _PROFILE_ID_RE.match(entry.name) is not None
        )
    state: list[dict] = []
    for name in names:
        # a shared file inside the profile decides the verdict: the bare
        # profiles/<name>/ directory matches no default rule and would
        # misclassify as LOCAL even when the profile syncs.
        probe = "SOUL.md" if name == "default" else f"profiles/{name}/SOUL.md"
        scope_value = scope.classify_path(probe, rules)
        state.append({"profile": name, "synced": scope_value is scope.Scope.SHARED})
    return state


def auto_status_payload() -> dict:
    """The `roam auto` status payload: both switches, the debounce, and the
    hooks' own last-auto-push/pending state. Read-only — assembling this
    changes nothing. Shared by the terminal verb (`hermes roam auto status`)
    and the read-only `/roam auto` form."""
    conf = load_settings()
    state = getattr(_ctx, "state", None) if _ctx is not None else None
    last = 0.0
    pending = False
    if state is not None:
        try:
            last = float(state.get("last_auto_push_at", 0) or 0)
        except (TypeError, ValueError):
            last = 0.0
        pending = bool(state.get("pending_auto_push", False))
    return {
        "auto_push": bool(conf.auto_push),
        "auto_pull_shared": bool(conf.auto_pull_shared),
        "push_debounce_seconds": int(conf.push_debounce_seconds),
        "last_auto_push_at": last,
        "pending_auto_push": pending,
    }


AUTO_EXPLANATION = (
    "what auto does: pushes at session end when shared files changed "
    "(debounced); pulls only skills, memories and SOUL.md at session start "
    "— never config, never over local edits"
)


def hermes_version() -> str:
    """Running Hermes version: the canonical hermes_cli resolver, else 'unknown'.

    The module-level version attribute and the hermes-agent dist metadata are
    both 0.0.0 placeholders in real installs, so version_info.base_version is
    the only source that carries the true version; the version-compat gate in
    roam.engine compares heads against it.
    """
    try:
        from hermes_cli.version_info import get_version_info

        version = get_version_info().base_version
        if version:
            return str(version)
    except Exception:  # noqa: BLE001 - any failure means we cannot know it
        logger.debug("hermes_cli.version_info unavailable", exc_info=True)
    return "unknown"


def build_engine() -> Any:
    """Build the roam Engine against the ROOT home and the configured backend.

    The engine always runs on the ROOT home — from a profile context
    (<root>/profiles/<name>) it maps up to <root> so one store and one lock
    cover the whole tree, profiles included."""
    errors = roam_module("errors")
    settings = load_settings()
    home = roam_home()
    try:
        engine_module = roam_module("engine")
        backends = roam_module("backends")
        backend = backends.open_backend(settings, home=home)
    except (ImportError, AttributeError) as exc:
        raise errors.ConfigError(f"roam engine modules are unavailable: {exc}") from exc
    return engine_module.Engine(home, settings, backend, hermes_version())
