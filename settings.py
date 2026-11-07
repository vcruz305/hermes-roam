"""Settings resolution for hermes-roam: config namespace + secrets -> RoamSettings.

Everything here runs inside Hermes (register() stored the PluginContext) or in
tests without Hermes; every Hermes import is lazy with a documented fallback.
"""

from __future__ import annotations

import dataclasses
import importlib
import logging
import os
import socket
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ctx: Any = None  # PluginContext stored by register(); None outside Hermes

_BACKENDS = {"s3", "git", "local"}
_SECRET_FIELDS = ("archive_key", "s3_access_key_id", "s3_secret_access_key")
_TRUE = {"1", "true", "yes", "on"}


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


def _cfg(key: str, default: Any) -> Any:
    assert _ctx is not None  # guarded by load_settings()
    value = _ctx.get_config(key, default)
    return default if value is None else value


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
    """Build RoamSettings from the plugin config namespace plus secrets."""
    model = roam_module("model")
    errors = roam_module("errors")
    if _ctx is None:
        raise errors.ConfigError(
            "hermes-roam is not registered: run it inside Hermes via `hermes roam` or /roam"
        )
    defaults = model.RoamSettings()
    values: dict[str, Any] = {}
    for field in dataclasses.fields(model.RoamSettings):
        if field.name in _SECRET_FIELDS:
            continue
        default = getattr(defaults, field.name)
        raw = _cfg(field.name, default)
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
    journaled install is the user's decision, not the model's.
    """
    if _ctx is None:
        return False
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
    """Build the roam Engine against the active home and configured backend."""
    errors = roam_module("errors")
    settings = load_settings()
    home = hermes_home()
    try:
        engine_module = roam_module("engine")
        backends = roam_module("backends")
        backend = backends.open_backend(settings, home=home)
    except (ImportError, AttributeError) as exc:
        raise errors.ConfigError(f"roam engine modules are unavailable: {exc}") from exc
    return engine_module.Engine(home, settings, backend, hermes_version())
