"""`/roam` slash command: short, Telegram-friendly text output.

Every reply is a handful of bullet lines — no tables, no secrets — and the
only destructive form is the literal `/roam pull apply`. R7: remote-derived
strings (backend error text, head roster names, manifest paths) are never
relayed verbatim — backend errors become a fixed summary, head names and
paths only ever appear when they pass the fleet charsets.
"""

from __future__ import annotations

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
)

_VERBS = ("status", "push", "pull", "diff", "devices", "sessions", "doctor", "history", "help")
_MAX_LINES = 20
_MAX_CHARS = 160
# Engine-authored pull notices (kept key names, first-pull and brand-new-
# device guidance) are long fixed sentences; 160 chars would cut the
# guidance mid-command (0.4.2).
_NOTE_CHARS = 500
_MAX_ERROR_CHARS = 300


def _short(value: object) -> str:
    text = str(value)
    return text if len(text) <= _MAX_CHARS else text[: _MAX_CHARS - 1] + "…"


def _short_note(value: object) -> str:
    text = str(value)
    return text if len(text) <= _NOTE_CHARS else text[: _NOTE_CHARS - 1] + "…"


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"• {_short(line)}" for line in lines[:_MAX_LINES])


def _help(note: str = "") -> str:
    text = ("Roam commands: /roam status, push, push force, push dry-run, "
            "pull, pull apply, pull apply overwrite, diff, devices, sessions, "
            "doctor, history, help. pull alone only shows the plan; pull apply "
            "rewrites home files after making a backup; pull apply overwrite "
            "also overwrites files changed here since the last sync. roam "
            "always runs on the ROOT home — under a named profile it covers "
            "the whole tree (root plus every profiles/<name>/) with settings "
            "from the root home's config. sessions lists pulled read-only "
            "session archives grouped by device and profile — a transcript "
            "is never dumped here; print one on the terminal with `hermes "
            "roam sessions show <device> <profile> <session_id>`. history "
            "lists the versions the remote store still holds; recovering "
            "one is a terminal command: hermes roam restore <path> "
            "--generation N --apply. The roam_pull tool refuses apply=true "
            "unless the user enabled allow_agent_apply. Secrets are never "
            "synced.")
    return f"Roam: {note}\n{text}" if note else f"Roam: {text}"


def handle(raw_args: str, **kwargs) -> str:
    """Slash-command entry point; never raises, never prints secrets."""
    try:
        words = [w for w in (raw_args or "").split() if w]
        verb = words[0] if words else "help"
        if verb not in _VERBS:
            return _help(f"unknown command '{verb}'")
        if verb == "help":
            return _help()
        if verb == "sessions":
            # no engine needed: the read-only archive listing reads local
            # files only and never touches the backend
            return _sessions()
        errors = roam_module("errors")
        try:
            engine = settings.build_engine()
        except errors.RoamError as exc:
            # RoamError messages are authored by this plugin and name the fix
            # (including the R9 lock holder); show them, wider than content.
            return f"Roam error: {safe_error_text(exc)}"
        if verb == "status":
            return _status(engine)
        if verb == "push":
            return _push(engine, words[1:])
        if verb == "pull":
            return _pull(engine, words[1:])
        if verb == "diff":
            return _diff(engine)
        if verb == "devices":
            return _devices(engine)
        if verb == "history":
            return _history(engine, words[1:])
        return _doctor(engine)
    except Exception as exc:  # noqa: BLE001 - slash handlers never raise
        return f"Roam error: unexpected failure: {safe_error_text(exc)}"


def _status(engine) -> str:
    status = engine.status()
    lines = [f"device {status.device}, backend {status.backend}",
             _covered_line()]
    opt_ins = getattr(status, "opt_ins", {}) or {}
    lines.append(
        "session archive publish "
        + ("ON" if opt_ins.get("session_archive") else "off")
        + f" (cap {int(opt_ins.get('session_archive_max_mb') or 0)} MB)"
        + "; cron job definitions "
        + ("ON" if opt_ins.get("cron_jobs") else "off")
    )
    if status.remote_generation is None and status.error:
        # R7: the backend error text may be remote-controlled — fixed summary.
        lines.append(f"remote unreachable: {safe_status_error(status.error)}")
    elif status.remote_generation is None:
        lines.append("remote unreachable or empty")
    elif status.remote_ahead:
        ahead = max(1, status.remote_generation - status.local.last_seen_generation)
        lines.append(f"remote is {ahead} generations ahead — run /roam pull")
    else:
        lines.append(f"remote generation {status.remote_generation} is not ahead of this device")
    lines.append(
        f"{status.dirty_count} shared files changed locally since the last sync"
        if status.local_dirty
        else "no local shared changes since the last sync"
    )
    lines.append(f"fleet: {len(status.devices)} devices "
                 f"({', '.join(safe_device_name(d.name) for d in status.devices) or 'none'})")
    recovered = getattr(status, "recovered", None)
    if recovered:
        lines.append(f"recovered: {recovered}")
    return "Roam status:\n" + _bullets(lines)


def _push(engine, rest: list[str]) -> str:
    if any(word not in ("force", "dry", "dry-run") for word in rest):
        return ("Roam: push takes no other arguments; send /roam push, or add "
                "force or dry-run (e.g. /roam push force)")
    if not (engine.home / "roam" / "state.json").is_file():
        return "Roam error: no local roam state — run `hermes roam init` first"
    result = engine.push(
        force="force" in rest,
        dry_run=any(word in ("dry", "dry-run") for word in rest),
    )
    if result.dry_run:
        notes = getattr(result, "notes", []) or []
        text = (f"Roam: dry run — would push {result.file_count} files, "
                f"{result.bundle_size} bytes; run `hermes roam push --dry-run` "
                "for the file list")
        if notes:
            text += "\n" + _bullets(notes)
        return text
    if result.skipped_reason:
        notes = getattr(result, "notes", []) or []
        text = f"Roam: nothing to push ({result.skipped_reason})"
        if notes:
            text += "\n" + _bullets(notes)
        return text
    notes = getattr(result, "notes", []) or []
    text = ("Roam: pushed generation "
            f"{result.generation} ({result.file_count} files, {result.bundle_size} bytes)\n"
            + _bullets([_covered_line(), *notes]))
    return text


def _pull(engine, rest: list[str]) -> str:
    if any(word not in ("apply", "overwrite") for word in rest):
        return ("Roam: pull takes no other arguments; send /roam pull to see "
                "the plan, /roam pull apply to install, or /roam pull apply "
                "overwrite to also overwrite files changed here since the "
                "last sync")
    apply = "apply" in rest
    kwargs = settings.supported_kwargs(
        engine,
        "pull",
        {"apply": apply, "classes": None, "dry_run": False,
         "overwrite_conflicts": "overwrite" in rest},
    )
    result = engine.pull(**kwargs)
    conflicts = sorted(safe_relpath(p) for p in getattr(result, "conflicts", []) or [])
    kept = sorted(safe_relpath(p) for p in getattr(result, "skipped_local", []) or [])
    recovered = getattr(result, "recovered", None)

    def conflict_lines() -> list[str]:
        lines = [f"{len(conflicts)} file(s) kept local (changed here since last sync)"]
        lines.extend(conflicts[:5])
        if len(conflicts) > 5:
            lines.append(f"... {len(conflicts) - 5} more kept local")
        lines.append("send /roam pull apply overwrite to overwrite them (a backup is kept)")
        return lines

    if not result.applied:
        counts = {group: sum(len(p) for p in getattr(result.plan, group).values())
                  for group in ("added", "changed", "removed")}
        lines = [f"plan for remote generation {result.generation} — nothing changed yet",
             _covered_line(),
                f"{counts['added']} added, {counts['changed']} changed, "
                f"{counts['removed']} removed files"]
        for entry in sorted(getattr(result.plan, "configs", []) or []):
            if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
                continue
            relpath, mode = entry
            if mode == "first_pull":
                lines.append(
                    f"would first-pull merge {safe_relpath(relpath)} "
                    "(remote-only shared keys added, local values win where "
                    "both are set, host-scoped and secret keys stay local)"
                )
            else:
                lines.append(
                    f"would merge {safe_relpath(relpath)} (host-scoped keys stay local)"
                )
        config_conflicts = [
            safe_relpath(path)
            for path in sorted(getattr(result.plan, "config_conflicts", []) or [])
        ]
        if config_conflicts:
            lines.append(
                f"{len(config_conflicts)} config file(s) changed here AND on the "
                "remote — the local copy is kept; /roam pull apply overwrite "
                "would overwrite them (a backup is kept)"
            )
        unchanged = list(getattr(result.plan, "unchanged", []) or [])
        if unchanged:
            lines.append(f"{len(unchanged)} file(s) unchanged (identical to the remote "
                         "copy — not rewritten, no backup taken)")
        if conflicts:
            lines.extend(conflict_lines())
        if kept:
            lines.append(f"{len(kept)} remote file(s) kept local by your scope rules")
        if recovered:
            lines.append(f"recovered: {recovered}")
        if result.plugins_to_install:
            lines.append("plugins to install with `hermes plugins install`: "
                         + ", ".join(result.plugins_to_install))
        notes = list(getattr(result, "notes", []) or [])
        text = "Roam: " + _bullets(lines)
        # 0.4.2/R11: the engine's plan notices — the kept local key names,
        # the first-pull union rules and the brand-new-device guidance —
        # must reach the plan review, not only the apply. They are
        # engine-authored sentences, wide-capped above.
        if notes:
            text += "\n" + "\n".join(
                f"• {_short_note(note)}" for note in notes[:10]
            )
        text += "\n• send /roam pull apply to install after making a backup"
        return text
    lines = [f"installed remote generation {result.generation}",
             _covered_line()]
    if recovered:
        lines.append(f"recovered: {recovered}")
    if result.backup_dir:
        lines.append(f"backup of replaced files: {result.backup_dir}")
    if conflicts:
        lines.extend(conflict_lines())
    if kept:
        lines.append(f"{len(kept)} remote file(s) kept local by your scope rules")
    if result.plugins_to_install:
        lines.append("plugins to install with `hermes plugins install`: "
                     + ", ".join(result.plugins_to_install))
    notes = list(result.notes)
    text = "Roam: " + _bullets(lines)
    # 0.4.2: apply notices are engine-authored long sentences (first-pull
    # merge rules, kept key names) — wide-capped, never silently dropped.
    if notes:
        text += "\n" + "\n".join(
            f"• {_short_note(note)}" for note in notes[:10]
        )
    return text


def _diff(engine) -> str:
    diff = engine.diff()
    if diff.is_empty():
        return (f"Roam: pull preview — nothing would change against remote "
                f"generation {diff.remote_generation}")
    counts = {group: sum(len(p) for p in getattr(diff, group).values())
              for group in ("added", "changed", "removed")}
    paths = sorted(
        {safe_relpath(path) for group in ("added", "changed", "removed")
         for paths in getattr(diff, group).values() for path in paths}
    )
    lines = [f"pull preview: {counts['added']} added, {counts['changed']} changed, "
             f"{counts['removed']} removed vs remote generation {diff.remote_generation}"]
    lines.extend(paths[:10])
    if len(paths) > 10:
        lines.append(f"... {len(paths) - 10} more changed paths")
    lines.append("send /roam pull to review the plan")
    return "Roam: " + _bullets(lines)


def _devices(engine) -> str:
    status = engine.status()
    if not status.devices:
        return "Roam: no devices have pushed or pulled yet"
    lines = [f"{safe_device_name(d.name)}: pushed {d.last_push_generation}, "
             f"pulled {d.last_pull_generation}" for d in status.devices]
    return "Roam fleet:\n" + _bullets(lines)


def _covered_line() -> str:
    """The covered-tree line every /roam result names: the ROOT home (a
    profile context maps up to it) and each profile's sync state."""
    state = settings.profile_sync_state()
    summary = ", ".join(
        f"{item['profile']}({'synced' if item['synced'] else 'excluded'})"
        for item in state
    ) or "none"
    return f"covered root {settings.roam_home()} — profiles: {summary}"


def _sessions() -> str:
    """Read-only listing of pulled session archives — NEVER a transcript.

    A transcript is remote-sourced text and must never be bulk-dumped into
    the model: this surface lists ids/titles (sanitized) and points at the
    terminal command that prints one. Grouped by device, then by profile.
    """
    archive = roam_module("session_archive")
    home = settings.roam_home()
    devices = archive.list_local_archive(home)
    if not devices:
        return ("Roam: no pulled session archives on this device; a device "
                "that enables sync_sessions publishes the transcripts of "
                "every profile of its root home, and `hermes roam pull "
                "--apply` receives them")
    lines: list[str] = []
    for device in devices:
        for profile in device.get("profiles") or []:
            sessions = profile.get("sessions") or []
            lines.append(
                f"{safe_device_name(device.get('device'))} / "
                f"{safe_profile_name(profile.get('profile'))}: "
                f"{len(sessions)} archived session(s) (read-only)"
            )
            shown = sessions[:5]
            for session in shown:
                lines.append(
                    f"  {safe_session_id(session.get('session_id'))} "
                    f"{safe_title(session.get('title'), 80) or '(no title)'}"
                )
            if len(sessions) > len(shown):
                lines.append(f"  ... {len(sessions) - len(shown)} more")
    lines.append("print one on the terminal: `hermes roam sessions show <device> "
                 "<profile> <session_id>`")
    lines.append(archive.local_archive_note())
    return "Roam sessions:\n" + _bullets(lines)


def _history(engine, rest: list[str]) -> str:
    if len(rest) > 1:
        return ("Roam: history takes at most one path; send /roam history or "
                "/roam history <path>")
    entries = engine.history(rest[0] if rest else None)
    if not entries:
        return "Roam: no recoverable versions in the remote store"
    lines = [f"{len(entries)} recoverable version(s) in the remote store (newest first)"]
    for entry in entries[:10]:
        state = "deleted" if entry.get("deleted") else "present"
        lines.append(
            f"gen {entry.get('generation')} {state} "
            f"{safe_relpath(entry.get('path'))} sha {str(entry.get('sha256') or '')[:12]}"
        )
    if len(entries) > 10:
        lines.append(f"... {len(entries) - 10} more (run `hermes roam history` on the terminal)")
    lines.append("recover one on the terminal with `hermes roam restore <path> "
                 "--generation N --apply` (a backup is kept)")
    return "Roam history:\n" + _bullets(lines)


def _doctor(engine) -> str:
    checks = engine.doctor()
    failures = [f"{c.name}: {c.detail}" for c in checks if not c.ok]
    lines = failures or ["all checks passed"]
    return f"Roam doctor: {len(checks) - len(failures)}/{len(checks)} checks passed\n" + _bullets(lines)
