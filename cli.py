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
import json
import time

from . import settings
from .settings import roam_module
from .tools import (
    safe_device_name,
    safe_error_text,
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
        _json(sanitize_status_dict(status.to_dict()))
        if status.remote_generation is None and status.error:
            return 1
        return 0
    if status.remote_generation is None and status.error:
        # R7: the backend error text may be remote-controlled — fixed summary.
        print(f"roam: remote unreachable: {safe_status_error(status.error)}")
        recovered = getattr(status, "recovered", None)
        if recovered:
            print(f"roam: recovered: {recovered}")
        return 1
    local = status.local
    print(_row("device", status.device))
    print(_row("backend", status.backend))
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
        _json(sanitize_push_dict(result.to_dict()))
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
        _json(sanitize_pull_dict(result.to_dict()))
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
        # the user reviews what the apply will really do.
        for path in sorted(getattr(plan, "configs", []) or []):
            print(f"roam: would merge {safe_relpath(path)} (host-scoped keys stay local)")
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
        if recovered:
            print(f"roam: recovered: {recovered}")
        print("roam: review the plan, then run `hermes roam pull --apply`")
        print("roam: --apply rewrites home files after making a backup")
        return 0
    print(f"roam: installed remote generation {result.generation}")
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
        print(f"roam: {_short(note)}")
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


def _sanitize_archive(devices: list) -> list:
    """The read-only archive listing with every remote-sourced field
    sanitized: device names and session ids charset-checked, titles
    scrubbed to one line (0.4.1)."""
    out: list = []
    for device in devices or []:
        if not isinstance(device, dict):
            continue
        sessions = []
        for session in device.get("sessions") or []:
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
        out.append({"device": safe_device_name(device.get("device", "")), "sessions": sessions})
    return out


def _cmd_sessions(args: argparse.Namespace, as_json: bool) -> int:
    """Read-only listing/printing of the session archives this device PULLED.

    The transcripts are remote-sourced text read back from disk: every
    identifier is charset-checked, every title is scrubbed, and a printed
    transcript goes through the block sanitizer. Nothing here can publish,
    resume, or re-push an archived session.
    """
    archive = roam_module("session_archive")
    home = settings.hermes_home()
    if getattr(args, "sessions_action", None) == "show":
        text, error = archive.read_local_session(home, args.device, args.session_id)
        if error:
            print(f"roam: {error}")
            return 1
        textsafe = roam_module("textsafe")
        if as_json:
            _json(
                {
                    "device": safe_device_name(args.device),
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
        print("roam: a device that enables sync_sessions publishes its transcripts; "
              "run `hermes roam pull --apply` to receive them")
        return 0
    print("roam: pulled session archives (read-only)")
    lines: list[str] = []
    for device in devices:
        total = sum(session["size"] for session in device["sessions"])
        lines.append(
            f"{device['device']}: {len(device['sessions'])} session(s), "
            f"{_human_size(total)} total"
        )
        for session in device["sessions"]:
            lines.append(
                f"  {session['session_id']}  {session['date'] or 'date unknown'}  "
                f"{_human_size(session['size'])}  {session['title'] or '(no title)'}"
            )
    print(*_cap(lines), sep="\n")
    print("roam: print one with `hermes roam sessions show <device> <session_id>`")
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
        _json({"init": summary, "next_steps": list(steps)})
        return 0
    print(f"roam: {summary}")
    print("roam: next steps:")
    for index, step in enumerate(steps, 1):
        print(f"  {index}. {_short(step, 160)}")
    print("roam: never paste secret values into chat or files roam writes")
    return 0


def _cmd_rules(args: argparse.Namespace, as_json: bool) -> int:
    home = settings.hermes_home()
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
        _json({"rules": entries, "host_keys": host_keys})
        return 0
    defaults = {(pattern, value.value) for pattern, value in scope.DEFAULT_RULES}
    user = [entry for entry in entries if entry not in defaults]
    counts = {"local": 0, "ignored": 0, "shared": 0}
    for _, value in entries:
        if value in counts:
            counts[value] += 1
    print(f"roam: effective scope rules (home {home})")
    lines = [f"{value:<8} {pattern} (user rule; takes precedence)" for pattern, value in user]
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
        "hub-installed skills, plugin code trees, roam/rules.yaml itself, and "
        "everything under <home>/roam/ (including pulled read-only session "
        "archives)",
    ])
    print(*_cap(lines), sep="\n")
    return 0
