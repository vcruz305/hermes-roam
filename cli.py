"""`hermes roam` CLI: init, status, diff, push, pull, devices, doctor, rules.

Human output is short aligned lines under 80 columns; ``--json`` prints one
machine-readable object. Exit codes: 0 ok, 1 error, 2 conflict (the remote
head moved ahead of this device). R7: remote-derived strings (backend error
text, head roster names, manifest paths) are never printed verbatim — a
backend error becomes a fixed summary (the detail is logged), and head names
and paths only ever appear when they pass the fleet charsets.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import time

from . import settings
from .settings import roam_module
from .tools import (
    safe_device_name,
    safe_error_text,
    safe_profile_name,
    safe_relpath,
    safe_session_id,
    safe_status_error,
    safe_title,
    sanitize_diff_dict,
    sanitize_history_list,
    sanitize_pull_dict,
    sanitize_push_dict,
    sanitize_restore_dict,
    sanitize_status_dict,
)

_MAX_LINES = 40
_MAX_VALUE = 160
# Engine-authored pull/push notices (kept key names, first-pull and
# brand-new-device guidance) are long fixed sentences; 160 chars would cut
# the guidance mid-command. They are engine-built, not free remote text —
# key names are the only remote-influenced fragment and stay bounded.
_NOTE_VALUE = 500
_MAX_ERROR_CHARS = 300

_RULES_TEMPLATE = """\
# hermes-roam user scope rules. User rules take precedence over the built-in
# defaults; the first matching pattern wins. Patterns are relative POSIX paths
# inside the Hermes home. This file is per-device: it never syncs and never
# leaves this machine. Uncomment and edit what you need:
#
# shared:   always synced, e.g. skills/, memories/, SOUL.md, config.yaml
# local:    never leaves this machine, e.g. .env, auth.json, state.db
# ignored:  never even listed, e.g. cache/, logs/
# host_keys: config.yaml keys kept local on pull (dotted paths)
#
#shared:
#  - skills/my-private-skill/**
#local:
#  - notebooks/
#ignored:
#  - vm-images/
#host_keys:
#  - custom.host.key
"""


def _short(value: object, width: int = _MAX_VALUE) -> str:
    text = str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def _row(label: str, value: object) -> str:
    return f"{label:<12} {_short(value, 120)}"


def _cap(lines: list[str]) -> list[str]:
    if len(lines) <= _MAX_LINES:
        return lines
    return lines[:_MAX_LINES] + [f"... {len(lines) - _MAX_LINES} more"]


def _fail(as_json: bool, exc: Exception) -> None:
    text = safe_error_text(exc)
    if as_json:
        print(json.dumps({"error": text[:_MAX_ERROR_CHARS], "kind": type(exc).__name__}))
    else:
        print(f"roam: {text}")


def _json(value: object) -> None:
    print(json.dumps(value))


def _classes(args: argparse.Namespace) -> object:
    """Pull scope classes from --cron; None keeps engine defaults (0.4.1:
    the read-only session archive is always among the defaults — reading
    what another device published needs no opt-in — so there is no
    --sessions flag anymore)."""
    model = roam_module("model")
    picked = {model.Scope.SHARED, model.Scope.SESSIONS}
    if getattr(args, "cron", False):
        picked.add(model.Scope.CRON)
    return None if picked == {model.Scope.SHARED, model.Scope.SESSIONS} else picked


def add_cli_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Build the `hermes roam` argparse tree (the plugin CLI setup_fn)."""
    subs = parser.add_subparsers(dest="command", metavar="command", required=True)

    init = subs.add_parser("init", help="check settings and initialize local state")
    init.add_argument("--create-remote", action="store_true",
                      help="create the remote store when it does not exist")
    init.add_argument("--json", action="store_true", help="print machine-readable JSON")

    for name, text in (
        ("status", "show local and remote sync status"),
        ("devices", "list the devices that ever pushed or pulled"),
        ("diff", "preview what a pull would change"),
    ):
        sub = subs.add_parser(name, help=text)
        sub.add_argument("--json", action="store_true", help="print machine-readable JSON")

    push = subs.add_parser("push", help="upload a new encrypted generation")
    push.add_argument("--force", action="store_true",
                      help="push even though the remote head moved ahead")
    push.add_argument("--dry-run", action="store_true", help="report without uploading")
    push.add_argument("--json", action="store_true", help="print machine-readable JSON")

    pull = subs.add_parser("pull", help="download and install the remote generation")
    pull.add_argument("--apply", action="store_true",
                      help="rewrite home files after making a backup")
    pull.add_argument("--overwrite-conflicts", action="store_true",
                      help="with --apply, also overwrite files changed here since "
                           "the last sync (a backup is kept)")
    pull.add_argument("--dry-run", action="store_true", help="verify without planning")
    pull.add_argument("--cron", action="store_true", help="include cron job definitions")
    pull.add_argument("--json", action="store_true", help="print machine-readable JSON")

    sessions = subs.add_parser(
        "sessions", help="list the read-only session archives this device pulled"
    )
    sessions.add_argument("--json", action="store_true", help="print machine-readable JSON")
    sessions_sub = sessions.add_subparsers(dest="sessions_action", metavar="action")
    sessions_show = sessions_sub.add_parser(
        "show", help="print one archived transcript (read-only)"
    )
    sessions_show.add_argument("device", help="the device that published the session")
    sessions_show.add_argument(
        "profile", help="the profile the session ran under ('default' for the root home)"
    )
    sessions_show.add_argument("session_id", help="the session id to print")
    sessions_show.add_argument("--json", action="store_true", help="print machine-readable JSON")

    doctor = subs.add_parser("doctor", help="run all sync health checks")
    doctor.add_argument("--json", action="store_true", help="print machine-readable JSON")

    history = subs.add_parser(
        "history", help="list the versions the remote store still holds"
    )
    history.add_argument(
        "path", nargs="?", default=None, help="only this relative path"
    )
    history.add_argument("--json", action="store_true", help="print machine-readable JSON")

    restore = subs.add_parser(
        "restore", help="recover one file version from the remote store"
    )
    restore.add_argument("path", help="the relative path to recover")
    restore.add_argument(
        "--generation", type=int, required=True,
        help="the generation to recover the path from (see `hermes roam history`)"
    )
    restore.add_argument(
        "--apply", action="store_true",
        help="write the recovered version into the home (a backup is kept)"
    )
    restore.add_argument("--json", action="store_true", help="print machine-readable JSON")

    rules = subs.add_parser("rules", help="show the effective scope rules")
    rules.add_argument("--init", action="store_true",
                        help="write a commented template to <home>/roam/rules.yaml")
    rules.add_argument("--json", action="store_true", help="print machine-readable JSON")

    auto = subs.add_parser(
        "auto", help="show or set the automatic session-boundary sync switches"
    )
    auto.add_argument(
        "action", nargs="?", default="status", choices=("on", "off", "status"),
        help="on: enable the session-end auto-push and the session-start "
             "auto-pull of skills/memories/SOUL.md; off: disable both; "
             "status (default): show both switches, the debounce and the "
             "hooks' state",
    )
    auto.add_argument("--json", action="store_true", help="print machine-readable JSON")
    return parser


def make_parser() -> argparse.ArgumentParser:
    """A standalone `hermes roam` parser (tests, --help outside Hermes)."""
    return add_cli_args(
        argparse.ArgumentParser(prog="hermes roam", description="Sync the Hermes home across devices.")
    )


def handler(args: argparse.Namespace) -> int:
    """CLI entry point (handler_fn); returns the process exit code."""
    return _run(args)


def _run(args: argparse.Namespace) -> int:
    errors = roam_module("errors")
    as_json = bool(getattr(args, "json", False))
    try:
        if args.command == "rules":
            return _cmd_rules(args, as_json)
        if args.command == "init":
            return _cmd_init(args, as_json)
        if args.command == "sessions":
            return _cmd_sessions(args, as_json)
        if args.command == "auto":
            return _cmd_auto(args, as_json)
        engine = settings.build_engine()
        return {
            "status": lambda: _cmd_status(engine, as_json),
            "devices": lambda: _cmd_devices(engine, as_json),
            "diff": lambda: _cmd_diff(engine, as_json),
            "push": lambda: _cmd_push(engine, args, as_json),
            "pull": lambda: _cmd_pull(engine, args, as_json),
            "doctor": lambda: _cmd_doctor(engine, as_json),
            "history": lambda: _cmd_history(engine, args, as_json),
            "restore": lambda: _cmd_restore(engine, args, as_json),
        }[args.command]()
    except (errors.RemoteAhead, errors.HeadConflict) as exc:
        _fail(as_json, exc)
        if not as_json:
            print("roam: the remote head moved ahead — run `hermes roam pull` first")
        return 2
    except errors.RoamError as exc:
        _fail(as_json, exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - the CLI reports, never crashes
        _fail(as_json, exc)
        return 1


def _cmd_status(engine, as_json: bool) -> int:
    status = engine.status()
    if as_json:
        payload = sanitize_status_dict(status.to_dict())
        payload.update(_covered_root_dict())
        _json(payload)
        if status.remote_generation is None and status.error:
            return 1
        return 0
    if status.remote_generation is None and status.error:
        # R7: the backend error text may be remote-controlled — fixed summary.
        print(f"roam: remote unreachable: {safe_status_error(status.error)}")
        recovered = getattr(status, "recovered", None)
        if recovered:
            print(f"roam: recovered: {recovered}")
        _print_covered_root()
        return 1
    local = status.local
    root, summary, _ = _covered_tree()
    print(_row("device", status.device))
    print(_row("root", root))
    print(_row("backend", status.backend))
    print(_row("profiles", summary))
    opt_ins = getattr(status, "opt_ins", {}) or {}
    print(_row(
        "opt-ins",
        "session archive (publish) "
        + ("ON" if opt_ins.get("session_archive") else "off")
        + f" (cap {int(opt_ins.get('session_archive_max_mb') or 0)} MB)"
        + "; cron job definitions "
        + ("ON" if opt_ins.get("cron_jobs") else "off"),
    ))
    print(_row("pushed", f"generation {local.last_pushed_generation}"))
    print(_row("pulled", f"generation {local.last_pulled_generation}"))
    if status.remote_generation is None:
        print(_row("remote", "unreachable or empty"))
    else:
        print(_row("remote head",
                  f"generation {status.remote_generation} by {safe_device_name(status.remote_device)}"))
    if status.remote_ahead and status.remote_generation is not None:
        ahead = max(1, status.remote_generation - local.last_seen_generation)
        print(_row("remote", f"{ahead} generations ahead — run `hermes roam pull`"))
    else:
        print(_row("remote", "not ahead of this device"))
    print(_row("local dirty", f"{status.dirty_count} shared file(s) changed — a push would carry them"
                             if status.local_dirty else "0 shared files changed — nothing to push"))
    print(_row("fleet", f"{len(status.devices)} devices"))
    recovered = getattr(status, "recovered", None)
    if recovered:
        print(_row("recovered", recovered))
    if status.error:
        print(_row("note", safe_status_error(status.error)))
    return 0


def _cmd_devices(engine, as_json: bool) -> int:
    status = engine.status()
    if as_json:
        _json([
            {**d.to_dict(), "name": safe_device_name(d.name)} for d in status.devices
        ])
        return 0
    if not status.devices:
        print("roam: no devices have pushed or pulled yet")
        return 0
    for device in status.devices:
        name = safe_device_name(device.name)
        print(_row(name, f"pushed {device.last_push_generation} pulled "
                       f"{device.last_pull_generation} hermes {device.hermes_version}"))
    return 0


def _cmd_diff(engine, as_json: bool) -> int:
    diff = engine.diff()
    if as_json:
        _json(sanitize_diff_dict(diff.to_dict()))
        return 0
    if diff.is_empty():
        print(f"roam: pull preview — nothing would change against remote "
              f"generation {diff.remote_generation}")
        return 0
    print(f"roam: pull preview against remote generation {diff.remote_generation}")
    for group, verb in (("added", "add"), ("changed", "update"), ("removed", "delete")):
        for scope, paths in getattr(diff, group).items():
            lines = [f"pull preview: would {verb} {scope.value} {safe_relpath(path)}"
                     for path in sorted(paths)]
            if lines:
                print(*_cap(lines), sep="\n")
    return 0


def _cmd_push(engine, args: argparse.Namespace, as_json: bool) -> int:
    if not (engine.home / "roam" / "state.json").is_file():
        message = "no local roam state — run `hermes roam init` first"
        if as_json:
            _json({"error": message, "kind": "RoamError"})
        else:
            print(f"roam: {message}")
        return 1
    result = engine.push(force=args.force, dry_run=args.dry_run)
    if as_json:
        payload = sanitize_push_dict(result.to_dict())
        payload.update(_covered_root_dict())
        _json(payload)
        return 0
    recovered = getattr(result, "recovered", None)
    if recovered:
        print(f"roam: recovered: {recovered}")
    notes = [f"roam: {_short(note)}" for note in getattr(result, "notes", []) or []]
    if result.skipped_reason:
        # An unchanged home skips before anything else — including on a dry
        # run, where the honest answer is the skip reason, not a would-push
        # summary of an empty bundle.
        print(f"roam: nothing to push ({result.skipped_reason})")
        if notes:
            print(*notes, sep="\n")
        return 0
    if result.dry_run:
        print(f"roam: dry run — would push {result.file_count} files, "
              f"{result.bundle_size} bytes as generation {result.generation}")
        files = list(getattr(result, "files", []) or [])
        if files:
            print(f"roam: would send {len(files)} file(s):")
            print(*_cap([f"would push {path}" for path in files]), sep="\n")
        else:
            print("roam: this build cannot list file names; counts only")
        if notes:
            print(*notes, sep="\n")
        return 0
    print(f"roam: pushed generation {result.generation} "
          f"({result.file_count} files, {result.bundle_size} bytes)")
    print(f"roam: bundle {_short(result.bundle_key)}")
    _print_covered_root()
    if notes:
        print(*notes, sep="\n")
    return 0


def _conflict_lines(result) -> tuple[list[str], list[str], str | None]:
    """(conflict lines, kept-local lines, recovered message) from a pull result.

    The conflicts/skipped_local/recovered fields are the pre-agreed 0.2.0
    engine API; getattr defaults keep this surface honest (it simply has
    nothing to report) until that engine change lands. Paths are
    remote-derived and charset-checked before display (R7).
    """
    conflicts = [f"kept local (changed here since last sync): {safe_relpath(path)}"
                 for path in sorted(getattr(result, "conflicts", []) or [])]
    kept = [f"kept local by your scope rules: {safe_relpath(path)}"
            for path in sorted(getattr(result, "skipped_local", []) or [])]
    return conflicts, kept, getattr(result, "recovered", None)


def _cmd_pull(engine, args: argparse.Namespace, as_json: bool) -> int:
    kwargs = settings.supported_kwargs(
        engine,
        "pull",
        {
            "apply": args.apply,
            "classes": _classes(args),
            "dry_run": args.dry_run,
            "overwrite_conflicts": args.overwrite_conflicts,
        },
    )
    result = engine.pull(**kwargs)
    if as_json:
        payload = sanitize_pull_dict(result.to_dict())
        payload.update(_covered_root_dict())
        _json(payload)
        return 0
    conflicts, kept, recovered = _conflict_lines(result)
    plan = result.plan
    if not result.applied:
        print(f"roam: plan only — nothing was written (remote generation {result.generation})")
        for group, verb in (("added", "add"), ("changed", "update"), ("removed", "delete")):
            for scope, paths in getattr(plan, group).items():
                lines = [f"would {verb} {scope.value} {safe_relpath(path)}"
                         for path in sorted(paths)]
                if lines:
                    print(*_cap(lines), sep="\n")
        # R11: the plan names every config merge and the unchanged files, so
        # the user reviews what the apply will really do. Configs carry a
        # 0.4.2 mode: a first-pull merge states the union rules.
        for relpath, mode in sorted(
            (entry[0], entry[1])
            for entry in getattr(plan, "configs", []) or []
            if isinstance(entry, (list, tuple)) and len(entry) == 2
        ):
            if mode == "first_pull":
                print(
                    f"roam: would first-pull merge {safe_relpath(relpath)} "
                    "(remote-only shared keys added, local values win where "
                    "both are set, host-scoped and secret keys stay local)"
                )
            else:
                print(f"roam: would merge {safe_relpath(relpath)} (host-scoped keys stay local)")
        config_conflicts = [
            safe_relpath(path)
            for path in sorted(getattr(plan, "config_conflicts", []) or [])
        ]
        if config_conflicts:
            print(f"roam: {len(config_conflicts)} config file(s) changed here AND "
                  "on the remote; --apply keeps the local copy:")
            print(*_cap(config_conflicts), sep="\n")
            print("roam: --apply --overwrite-conflicts overwrites them (a backup is kept)")
        unchanged = list(getattr(plan, "unchanged", []) or [])
        if unchanged:
            print(f"roam: {len(unchanged)} file(s) unchanged — identical to the remote "
                  "copy; they are not rewritten and no backup is taken")
        if conflicts:
            print(f"roam: {len(conflicts)} file(s) changed here since the last "
                  "sync; --apply would keep the local copy:")
            print(*_cap(conflicts), sep="\n")
            print("roam: --apply --overwrite-conflicts overwrites them (a backup is kept)")
        if kept:
            print(f"roam: {len(kept)} remote file(s) your scope rules keep local:")
            print(*_cap(kept), sep="\n")
        # 0.4.2/R11: the engine's plan notices — the kept local key names, the
        # first-pull union rules and the brand-new-device guidance — must
        # reach the plan review, not only the apply.
        for note in getattr(result, "notes", []) or []:
            print(f"roam: {_short(note, _NOTE_VALUE)}")
        if recovered:
            print(f"roam: recovered: {recovered}")
        print("roam: review the plan, then run `hermes roam pull --apply`")
        print("roam: --apply rewrites home files after making a backup")
        _print_covered_root()
        return 0
    print(f"roam: installed remote generation {result.generation}")
    _print_covered_root()
    if recovered:
        print(f"roam: recovered: {recovered}")
    if result.backup_dir:
        print(_row("backup", result.backup_dir))
    if conflicts:
        print(f"roam: {len(conflicts)} file(s) kept local (changed here since last sync):")
        print(*_cap(conflicts), sep="\n")
        print("roam: run `hermes roam pull --apply --overwrite-conflicts` "
              "to overwrite them (a backup is kept)")
    if kept:
        print(f"roam: {len(kept)} remote file(s) kept local by your scope rules:")
        print(*_cap(kept), sep="\n")
    if result.plugins_to_install:
        print(_row("plugins", ", ".join(result.plugins_to_install)))
        print("roam: install them with `hermes plugins install <name>`")
    for note in result.notes:
        print(f"roam: {_short(note, _NOTE_VALUE)}")
    return 0


def _cmd_doctor(engine, as_json: bool) -> int:
    checks = engine.doctor()
    if as_json:
        _json([c.to_dict() for c in checks])
        return 0 if all(c.ok for c in checks) else 1
    failed = 0
    for check in checks:
        mark = "ok" if check.ok else "FAIL"
        print(_row(f"[{mark}]", f"{check.name} — {_short(check.detail, 140)}"))
        failed += 0 if check.ok else 1
    print(f"roam: {len(checks) - failed}/{len(checks)} checks passed")
    return 0 if failed == 0 else 1


def _history_line(entry: dict) -> str:
    created = entry.get("created_at") or 0
    try:
        stamp = time.strftime("%Y-%m-%d", time.gmtime(float(created)))
    except (TypeError, ValueError, OverflowError):
        stamp = "unknown-date"
    state = "deleted" if entry.get("deleted") else "present"
    return (
        f"gen {entry.get('generation')} {safe_device_name(entry.get('device'))} {stamp} "
        f"{str(entry.get('sha256') or '')[:12]} {int(entry.get('size') or 0)}B {state} "
        f"{safe_relpath(entry.get('path'))}"
    )


def _cmd_history(engine, args: argparse.Namespace, as_json: bool) -> int:
    entries = engine.history(args.path)
    if as_json:
        _json(sanitize_history_list(entries))
        return 0
    if not entries:
        print("roam: no recoverable versions in the remote store")
        return 0
    print(f"roam: {len(entries)} recoverable version(s) in the remote store (newest first)")
    print(*_cap([_history_line(entry) for entry in entries]), sep="\n")
    print("roam: recover one with `hermes roam restore <path> --generation N --apply`")
    print("roam: the remote store keeps every pushed generation; it is never pruned by roam")
    return 0


def _cmd_restore(engine, args: argparse.Namespace, as_json: bool) -> int:
    result = engine.restore(args.path, generation=args.generation, apply=args.apply)
    if as_json:
        _json(sanitize_restore_dict(result))
        return 0
    recovered = result.get("recovered")
    if recovered:
        print(f"roam: recovered: {recovered}")
    for note in result.get("notes") or []:
        # 0.5.3: the engine's cron pause listing (with the exact
        # `hermes cron resume` command per job id) reaches both the plan
        # review and the applied restore.
        print(f"roam: {_short(note, _NOTE_VALUE)}")
    if not result.get("applied"):
        print(
            f"roam: plan only — generation {result.get('generation')} still holds "
            f"{safe_relpath(result.get('path'))} "
            f"(sha256 {str(result.get('sha256') or '')[:12]}, {int(result.get('size') or 0)}B)"
        )
        print("roam: review it, then run the same command with --apply")
        return 0
    print(
        f"roam: restored {safe_relpath(result.get('path'))} from generation "
        f"{result.get('generation')} into the home"
    )
    if result.get("config_merged"):
        print("roam: config merged; the shared projection was restored and host-scoped keys stayed local")
    if result.get("backup_dir"):
        print(_row("backup", result["backup_dir"]))
    print("roam: the restored version is a local change; the next `hermes roam push` publishes it")
    return 0


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "kB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def _covered_tree() -> tuple[str, str, list[dict]]:
    """(root path, human summary, JSON list) of the ROOT home and every
    profile's effective sync state under this device's rules — the
    covered-tree disclosure every naming surface shows."""
    root = settings.roam_home()
    state = settings.profile_sync_state()
    summary = ", ".join(
        f"{item['profile']}({'synced' if item['synced'] else 'excluded'})"
        for item in state
    ) or "none"
    return str(root), summary, [
        {"profile": item["profile"], "synced": item["synced"]} for item in state
    ]


def _print_covered_root() -> None:
    """Say which root and profiles the command covered (a profile context
    maps up to the ROOT home; the CLI names the whole covered tree)."""
    root, summary, _ = _covered_tree()
    print(f"roam: covered root {root} — profiles: {summary}")


def _covered_root_dict() -> dict:
    """The covered-tree fields added to --json payloads."""
    root, _, state = _covered_tree()
    return {"covered_root": root, "profiles": state}


def _sanitize_archive(devices: list) -> list:
    """The read-only archive listing with every remote-sourced field
    sanitized: device names, profile names and session ids charset-checked,
    titles scrubbed to one line. Grouped by device, then by profile."""
    out: list = []
    for device in devices or []:
        if not isinstance(device, dict):
            continue
        profiles = []
        for profile in device.get("profiles") or []:
            if not isinstance(profile, dict):
                continue
            sessions = []
            for session in profile.get("sessions") or []:
                if not isinstance(session, dict):
                    continue
                sessions.append(
                    {
                        "session_id": safe_session_id(session.get("session_id", "")),
                        "title": safe_title(session.get("title", "")),
                        "date": safe_title(session.get("date", ""), 40),
                        "size": int(session.get("size") or 0),
                    }
                )
            profiles.append(
                {
                    "profile": safe_profile_name(profile.get("profile", "")),
                    "sessions": sessions,
                }
            )
        out.append(
            {"device": safe_device_name(device.get("device", "")), "profiles": profiles}
        )
    return out


def _cmd_sessions(args: argparse.Namespace, as_json: bool) -> int:
    """Read-only listing/printing of the session archives this device PULLED,
    grouped by device and by profile.

    The transcripts are remote-sourced text read back from disk: every
    identifier is charset-checked, every title is scrubbed, and a printed
    transcript goes through the block sanitizer. Nothing here can publish,
    resume, or re-push an archived session.
    """
    archive = roam_module("session_archive")
    home = settings.roam_home()
    if getattr(args, "sessions_action", None) == "show":
        text, error = archive.read_local_session(
            home, args.device, args.profile, args.session_id
        )
        if error:
            print(f"roam: {error}")
            return 1
        textsafe = roam_module("textsafe")
        if as_json:
            _json(
                {
                    "device": safe_device_name(args.device),
                    "profile": safe_profile_name(args.profile),
                    "session_id": safe_session_id(args.session_id),
                    "note": archive.local_archive_note(),
                    "transcript": textsafe.clean_block(text),
                }
            )
            return 0
        print(textsafe.clean_block(text))
        print(f"roam: {archive.local_archive_note()}")
        return 0
    devices = _sanitize_archive(archive.list_local_archive(home))
    if as_json:
        _json({"note": archive.local_archive_note(), "devices": devices})
        return 0
    if not devices:
        print("roam: no pulled session archives on this device")
        print("roam: a device that enables sync_sessions publishes its transcripts "
              "(every profile of its root home); run `hermes roam pull --apply` to "
              "receive them")
        return 0
    print("roam: pulled session archives (read-only, grouped by device then profile)")
    lines: list[str] = []
    for device in devices:
        for profile in device["profiles"]:
            total = sum(session["size"] for session in profile["sessions"])
            lines.append(
                f"{device['device']} / {profile['profile']}: "
                f"{len(profile['sessions'])} session(s), {_human_size(total)} total"
            )
            for session in profile["sessions"]:
                lines.append(
                    f"  {session['session_id']}  {session['date'] or 'date unknown'}  "
                    f"{_human_size(session['size'])}  {session['title'] or '(no title)'}"
                )
    print(*_cap(lines), sep="\n")
    print("roam: print one with `hermes roam sessions show <device> <profile> "
          "<session_id>`")
    print(f"roam: {archive.local_archive_note()}")
    return 0


def _cmd_init(args: argparse.Namespace, as_json: bool) -> int:
    engine = settings.build_engine()
    summary = engine.init(create_remote=args.create_remote)
    steps = (
        "put ROAM_ARCHIVE_KEY in the active home's <home>/.env "
        "(same value on every device)",
        "backend s3 also needs ROAM_S3_ACCESS_KEY_ID and "
        "ROAM_S3_SECRET_ACCESS_KEY in <home>/.env",
        "run `hermes roam doctor`",
        "run `hermes roam push`",
        "on the second device: `hermes roam pull`, review, then "
        "`hermes roam pull --apply`",
    )
    if as_json:
        _json({"init": summary, "next_steps": list(steps), **_covered_root_dict()})
        return 0
    print(f"roam: {summary}")
    print("roam: next steps:")
    for index, step in enumerate(steps, 1):
        print(f"  {index}. {_short(step, 160)}")
    print("roam: never paste secret values into chat or files roam writes")
    _print_covered_root()
    return 0


def find_hermes() -> str:
    """Locate the hermes executable on PATH; '' when it cannot be found."""
    return shutil.which("hermes") or ""


_AUTO_KEYS = ("auto_push", "auto_pull_shared")
# Hard cap on the config-set child (rule 12: every wait bounded). The child
# is hermes's own CLI config setter — argv only, stdin closed so no prompt
# can ever block, and a non-zero exit or a timeout fails the verb CLOSED.
_AUTO_SET_TIMEOUT_SECONDS = 60


def _auto_config_set(home, key: str, value: str) -> tuple[bool, str]:
    """Set ONE plugin setting for the ROOT home through Hermes's own CLI
    (`hermes config set plugins.entries.hermes-roam.settings.KEY VALUE`).

    Disciplined argv subprocess, never a shell; stdin closed so nothing can
    prompt; HERMES_HOME pinned to the ROOT home so a profile context still
    writes the root config; a hard timeout. Returns (ok, message); ok=False
    carries a fixed human summary — the child's output text is never
    relayed (it is not needed and may be noisy).
    """
    hermes_exe = find_hermes()
    if not hermes_exe:
        return False, "the hermes executable was not found on PATH"
    argv = [
        hermes_exe, "config", "set",
        f"plugins.entries.hermes-roam.settings.{key}", value,
    ]
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_AUTO_SET_TIMEOUT_SECONDS,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "the config-set subprocess exceeded its hard timeout"
    except OSError:
        return False, "the config-set subprocess could not be started"
    if proc.returncode != 0:
        return False, "the config-set subprocess failed (non-zero exit)"
    return True, ""


def _auto_switch_rows(payload: dict) -> list[str]:
    """The status rows shared by the terminal verb and the read-only
    `/roam auto` form: both switches, the debounce, the hooks' own state,
    and the one-line statement of exactly what auto does and does not do."""
    last = float(payload.get("last_auto_push_at") or 0)
    rendered = (
        datetime.datetime.fromtimestamp(last, datetime.UTC)
        .strftime("%Y-%m-%d %H:%M:%S UTC")
        if last > 0 else "never"
    )
    rows = [
        _row("auto_push", "true" if payload.get("auto_push") else "false"),
        _row("auto_pull_shared", "true" if payload.get("auto_pull_shared") else "false"),
        _row("debounce", f"{int(payload.get('push_debounce_seconds') or 0)}s"),
        _row("last auto-push", rendered),
        _row("pending", "yes — the last auto-push did not finish; run `hermes roam push` now"
             if payload.get("pending_auto_push") else "no"),
    ]
    rows.append(_short(settings.AUTO_EXPLANATION, 400))
    return rows


def _cmd_auto(args: argparse.Namespace, as_json: bool) -> int:
    """`hermes roam auto on|off|status` — terminal-only, ROOT home.

    on/off run Hermes's own config setter as a bounded argv subprocess with
    HERMES_HOME pinned to the ROOT home (a profile context writes the root
    config, like every other verb) and refuse before `hermes roam init`.
    status is read-only. No slash form or agent tool can enable auto —
    enabling unattended sync is the user's decision.
    """
    root = settings.roam_home()
    if args.action in ("on", "off"):
        if not (root / "roam" / "state.json").is_file():
            message = (
                "roam is not initialized on this home — run `hermes roam init` "
                "first; auto sync only works on an initialized home"
            )
            if as_json:
                _json({"error": message})
            else:
                print(f"roam: {message}")
            return 1
        value = "true" if args.action == "on" else "false"
        applied: list[str] = []
        for key in _AUTO_KEYS:
            ok, message = _auto_config_set(root, key, value)
            if not ok:
                if as_json:
                    _json({"error": f"setting {key} failed: {message}",
                           "applied": applied})
                else:
                    print(f"roam: setting {key} failed: {message}")
                    if applied:
                        print(f"roam: applied before the failure: {', '.join(applied)}")
                return 1
            applied.append(key)
        if as_json:
            _json({"auto_push": value == "true",
                   "auto_pull_shared": value == "true",
                   "applied": applied})
        else:
            print(f"roam: auto_push={value} auto_pull_shared={value} written to the "
                  f"root home's config by hermes config set")
            print("roam: run `hermes roam auto status` in a fresh command to confirm")
            print(_short(settings.AUTO_EXPLANATION, 400))
        return 0
    payload = settings.auto_status_payload()
    if as_json:
        _json(payload)
        return 0
    for row in _auto_switch_rows(payload):
        print(f"roam: {row}")
    return 0


def _cmd_rules(args: argparse.Namespace, as_json: bool) -> int:
    home = settings.roam_home()
    rules_path = home / "roam" / "rules.yaml"
    if args.init:
        if rules_path.exists():
            print(f"roam: {rules_path} already exists; showing it, not overwriting")
        else:
            rules_path.parent.mkdir(parents=True, exist_ok=True)
            rules_path.write_text(_RULES_TEMPLATE, encoding="utf-8")
            print(f"roam: wrote a commented template to {rules_path}")
    scope = roam_module("scope")
    rules = scope.load_rules(home)
    entries = [(pattern, scope_value.value) for pattern, scope_value in rules.rules]
    host_keys = list(rules.host_keys)
    if as_json:
        _json({"rules": entries, "host_keys": host_keys, **_covered_root_dict()})
        return 0
    defaults = {(pattern, value.value) for pattern, value in scope.DEFAULT_RULES}
    generated = {
        (pattern, value.value) for pattern, value in getattr(rules, "device_rules", ())
    }
    user = [entry for entry in entries if entry not in defaults and entry not in generated]
    counts = {"local": 0, "ignored": 0, "shared": 0}
    for _, value in entries:
        if value in counts:
            counts[value] += 1
    print(f"roam: effective scope rules (home {home})")
    lines = [f"{value:<8} {pattern} (user rule; takes precedence)" for pattern, value in user]
    if generated:
        lines.append(
            f"{'local':<8} {len(generated)} generated rule(s): hub-installed skill trees and "
            "this device's UNMODIFIED Hermes bundled skills (each device's Hermes ships its own; "
            "a bundled skill you edited stays shared and syncs like your own)"
        )
    lines.append(
        f"defaults: {counts['local']} local, {counts['ignored']} ignored, "
        f"{counts['shared']} shared rules; full list: hermes roam rules --json"
    )
    lines.append("host keys kept local on pull:")
    lines.extend(f"{'keep':<8} {key}" for key in host_keys)
    # 0.4.1: the three sync tiers, stated at the rules surface too. The
    # opt-in state is best-effort here: this surface must still render when
    # settings cannot be loaded.
    try:
        opt = settings.load_settings()
        sess = str(bool(opt.sync_sessions)).lower()
        cron = str(bool(opt.sync_cron)).lower()
    except Exception:  # noqa: BLE001 - the rules listing must never fail
        sess = cron = "unknown"
    lines.extend([
        "",
        "what syncs (tiers):",
        "always:   skills/, memories/, SOUL.md, hermes-pack.yaml, skins/, "
        "config.yaml (host-specific keys kept local), and the plugin "
        "name-and-sha list roam/installed-plugins.json — never plugin code",
        f"optional: session archive publish (sync_sessions={sess}; opt-in per device), "
        f"cron job definitions (sync_cron={cron}; opt-in per device), and hooks "
        "via an explicit shared: rule in this device's roam/rules.yaml",
        "never:    secrets/.env/auth.json, the live state.db session database, "
        "raw sessions/ request dumps, caches/logs, host-specific config keys, "
        "hub-installed skills, Hermes bundled skills (unmodified — each "
        "device's Hermes ships its own; a bundled skill you MODIFIED syncs "
        "like your own), plugin code trees, roam/rules.yaml itself, and "
        "everything under <home>/roam/ (including pulled read-only session "
        "archives)",
        "",
        "profiles and bots: every profiles/<name>/ travels as one tree with "
        "the root home (profile.yaml, config.yaml, SOUL.md, memories/, "
        "skills/, skins/, hermes-pack.yaml shared; .env, auth.json, "
        "state.db and sessions/ local per profile) — exclude one with a "
        "local: rule for profiles/<name>/ in this file",
    ])
    print(*_cap(lines), sep="\n")
    _print_covered_root()
    return 0
