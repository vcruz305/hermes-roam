"""Read-only session archive: export collection, parsing, local listing.

The archive is the opt-in publish side of session sync (0.4.1). A device
that enables ``sync_sessions`` exports sessions with Hermes's own documented
CLI — ``hermes sessions export --format md --redact`` — and publishes the
transcripts in its per-device bundle namespace
``archive/sessions/<device>/``. Those paths exist only inside bundles: no
home path ever classifies into the namespace, and the raw ``sessions/``
folder (request dumps) is always LOCAL.

0.4.2: the export covers EVERY profile of the ROOT home, one export per
home (the ROOT home itself as profile ``default``, then each
``profiles/<name>/``), and every transcript lands in the per-profile
namespace ``archive/sessions/<device>/<profile>/``. The cap stays shared
across all profiles.

Receivers never publish or resume archived sessions: a pull installs them
READ-ONLY under ``<home>/roam/session-archive/<device>/<profile>/``
(everything under ``roam/`` is LOCAL, so the files can never be
re-pushed), and the only surfaces that read them are the ``hermes roam
sessions`` CLI and the read-only ``/roam sessions`` summary. Nothing
bulk-dumps transcripts into the model.

Export subprocess discipline: a disclosed argv list, never a shell;
``HERMES_HOME`` set to the home being exported; stdin closed; a hard
timeout inside the caller's operation deadline; no prompts (the md export
path asks nothing). Every failure degrades to a clear notice — the push of
everything else still succeeds.

Redaction: the export runs with Hermes's own ``--redact`` pass, but that
pass is pattern-based and best-effort. Transcripts travel encrypted and may
still contain anything the user typed; the README says so.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import head as head_mod

# Bundle-only namespace (head manifest keys) and the LOCAL read-only install
# location inside the receiving home.
ARCHIVE_PREFIX = "archive/sessions/"
LOCAL_ARCHIVE_DIR = "roam/session-archive/"

# Hard cap on the export subprocess (the caller also bounds it by the
# remaining operation deadline and passes the smaller value).
EXPORT_TIMEOUT_CAP = 240.0

# A fleet device name (already enforced for head fields) and the strict
# charset an exported transcript file name must pass.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")

# Canonical Hermes named-profile id grammar (PROFILE_ID_RE): a profile
# namespace segment must pass it, and the ROOT home's own export is named
# 'default'.
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEFAULT_PROFILE = "default"

# Frontmatter lines of a Hermes md session export (hermes_cli/session_export_md.py).
_HEADER_KEYS = ("session_id", "title", "created_at", "updated_at", "message_count")
_HEADER_SCAN_BYTES = 4096


def find_hermes() -> str:
    """Locate the hermes executable on PATH; '' when it cannot be found."""
    return shutil.which("hermes") or ""


def export_argv(hermes_exe: str, out_dir: Path) -> list[str]:
    """The disclosed argv list for the export subprocess (never a shell).

    ``--format md`` writes one file per session into ``out_dir``;
    ``--redact`` turns on Hermes's own secret-redaction pass;
    ``--after 2000-01-01`` is the bulk filter Hermes requires (every real
    session started after that date, pinned and archived rows included);
    ``--force`` overwrites any stale file in the throwaway scratch dir.
    The md path asks no questions, so no prompt can ever block it.
    """
    return [
        hermes_exe,
        "sessions",
        "export",
        str(out_dir),
        "--format",
        "md",
        "--redact",
        "--after",
        "2000-01-01",
        "--force",
    ]


def parse_header(text: str) -> dict[str, Any]:
    """The frontmatter fields of one exported transcript, as plain strings."""
    header: dict[str, Any] = {key: "" for key in _HEADER_KEYS}
    for line in text[:_HEADER_SCAN_BYTES].splitlines():
        stripped = line.strip()
        if stripped == "---":
            continue
        for key in _HEADER_KEYS:
            if not header[key] and stripped.startswith(f"{key}:"):
                header[key] = _quoted(stripped[len(key) + 1 :].strip())
    return header


def _quoted(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def collect_export(
    home: Path, scratch: Path, hermes_exe: str, timeout: float
) -> tuple[list[dict[str, Any]], str]:
    """Run Hermes's own session export and collect the transcripts.

    Returns ``(entries, notice)``: entries are the exported ``.md`` files as
    ``{name, data, header}``, sorted NEWEST first (by the session's own
    ``updated_at``); notice is "" on success and a fixed human summary on
    any failure — the caller degrades, it never raises.
    """
    if not hermes_exe:
        return [], "the hermes executable was not found on PATH"
    scratch.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    try:
        proc = subprocess.run(
            export_argv(hermes_exe, scratch),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=max(1.0, float(timeout)),
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [], "the export subprocess exceeded its hard timeout"
    except OSError:
        return [], "the export subprocess could not be started"
    if proc.returncode != 0:
        # a broken export must never look like "this device has no sessions"
        # (that would silently unpublish the previous archive)
        return [], "the export subprocess failed (non-zero exit)"
    entries: list[dict[str, Any]] = []
    for path in sorted(scratch.glob("*.md")):
        if not path.is_file() or not _FILE_NAME_RE.match(path.name):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        header = parse_header(data.decode("utf-8", "replace"))
        entries.append({"name": path.name, "data": data, "header": header})
    entries.sort(key=lambda item: str(item["header"].get("updated_at") or ""), reverse=True)
    return entries, ""


def cap_entries(
    entries: list[dict[str, Any]], max_bytes: int
) -> tuple[list[dict[str, Any]], int]:
    """Keep the NEWEST transcripts until the total cap; (kept, dropped).

    0.4.2: the entries come from EVERY profile's export, so the caller can no
    longer rely on any one export's ordering — the global NEWEST-first order
    is established here, and the cap is shared across all profiles."""
    ordered = sorted(
        entries,
        key=lambda item: str(item.get("header", {}).get("updated_at") or ""),
        reverse=True,
    )
    kept: list[dict[str, Any]] = []
    total = 0
    for entry in ordered:
        size = len(entry["data"])
        if total + size > max_bytes:
            break
        kept.append(entry)
        total += size
    return kept, len(ordered) - len(kept)


def export_homes(root: Path) -> list[tuple[str, Path]]:
    """Every home of the ROOT home that must be session-exported, as
    ``(profile_name, home_dir)`` pairs: the ROOT home itself first as
    profile ``default``, then each ``profiles/<name>/`` directory in name
    order. ``root`` must already be the ROOT home (the engine resolves it);
    a profile context never narrows the export to its own sessions."""
    root = Path(root)
    targets: list[tuple[str, Path]] = [(DEFAULT_PROFILE, root)]
    profiles_dir = root / "profiles"
    if profiles_dir.is_dir():
        for entry in sorted(profiles_dir.iterdir(), key=lambda item: item.name):
            if entry.is_dir() and _PROFILE_ID_RE.match(entry.name) is not None:
                targets.append((entry.name, entry))
    return targets


def is_valid_profile_name(name: str) -> bool:
    """Whether a namespace profile segment passes the profile id grammar."""
    return _PROFILE_ID_RE.match(name) is not None


def bundle_relpath(device: str, profile: str, name: str) -> str:
    """The bundle-only namespace key for one exported transcript:
    ``archive/sessions/<device>/<profile>/<name>``."""
    if (
        not head_mod.is_valid_device_name(device)
        or not is_valid_profile_name(profile)
        or not _FILE_NAME_RE.match(name)
    ):
        raise ValueError(f"unsafe archive path: {device!r}/{profile!r}/{name!r}")
    return f"{ARCHIVE_PREFIX}{device}/{profile}/{name}"


def is_archive_relpath(relpath: str) -> bool:
    """Whether a bundle/head path is inside the session archive namespace."""
    return isinstance(relpath, str) and relpath.startswith(ARCHIVE_PREFIX)


def archive_device(relpath: str) -> str:
    """The pushing device whose namespace a bundle path belongs to ('' when
    the path is not an archive path or is malformed)."""
    if not is_archive_relpath(relpath):
        return ""
    parts = relpath[len(ARCHIVE_PREFIX) :].split("/")
    return parts[0] if parts and head_mod.is_valid_device_name(parts[0]) else ""


def archive_profile(relpath: str) -> str:
    """The profile whose namespace a bundle path belongs to ('' when the
    path is malformed)."""
    if not is_archive_relpath(relpath):
        return ""
    parts = relpath[len(ARCHIVE_PREFIX) :].split("/")
    if len(parts) < 2 or not is_valid_profile_name(parts[1]):
        return ""
    return parts[1]


def install_relpath(relpath: str) -> str:
    """Where a pulled archive member lands: read-only under <home>/roam/."""
    if not is_archive_relpath(relpath):
        return relpath
    return LOCAL_ARCHIVE_DIR + relpath[len(ARCHIVE_PREFIX) :]


def _archive_root(home: Path) -> Path:
    return Path(home) / "roam" / "session-archive"


def list_local_archive(home: Path) -> list[dict[str, Any]]:
    """Devices, profiles and sessions this device pulled, for the read-only
    listing — grouped by device, then by profile.

    Every device and profile name passes its strict charset and every
    title/identifier is remote-sourced text read back from disk, so the
    callers sanitize it again before display. Sizes are the on-disk
    transcript sizes."""
    root = _archive_root(home)
    groups: list[dict[str, Any]] = []
    if not root.is_dir():
        return groups
    for device_dir in sorted(root.iterdir(), key=lambda item: item.name):
        if not device_dir.is_dir() or not head_mod.is_valid_device_name(device_dir.name):
            continue
        profiles: list[dict[str, Any]] = []
        for profile_dir in sorted(device_dir.iterdir(), key=lambda item: item.name):
            if not profile_dir.is_dir() or not is_valid_profile_name(profile_dir.name):
                continue
            sessions: list[dict[str, Any]] = []
            for path in sorted(profile_dir.glob("*.md")):
                if not path.is_file() or not _FILE_NAME_RE.match(path.name):
                    continue
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                header = parse_header(data.decode("utf-8", "replace"))
                sessions.append(
                    {
                        "session_id": str(header.get("session_id") or ""),
                        "title": str(header.get("title") or ""),
                        "date": str(header.get("updated_at") or header.get("created_at") or ""),
                        "size": len(data),
                    }
                )
            if sessions:
                profiles.append({"profile": profile_dir.name, "sessions": sessions})
        if profiles:
            groups.append({"device": device_dir.name, "profiles": profiles})
    return groups


def read_local_session(
    home: Path, device: str, profile: str, session_id: str
) -> tuple[str, str]:
    """One pulled transcript's text (''), or ('', error). Names are strict."""
    if (
        not head_mod.is_valid_device_name(device)
        or not is_valid_profile_name(profile)
        or not _SESSION_ID_RE.match(session_id)
    ):
        return "", "no such device or session in the local session archive"
    device_dir = _archive_root(home) / device / profile
    if not device_dir.is_dir():
        return "", "no such device or session in the local session archive"
    matches = sorted(device_dir.glob(f"{session_id}-*.md"))
    if not matches:
        # the export file may carry the bare id without a title slug
        matches = sorted(device_dir.glob(f"{session_id}.md"))
    if not matches:
        return "", "no such device or session in the local session archive"
    path = matches[0]
    try:
        root = _archive_root(home).resolve()
        if not path.resolve().is_relative_to(root):
            return "", "no such device or session in the local session archive"
        return path.read_text(encoding="utf-8", errors="replace"), ""
    except OSError:
        return "", "the archived transcript could not be read"


def local_archive_note() -> str:
    """The fixed one-line explanation every archive surface shows."""
    return (
        "archived sessions are read-only: they cannot be resumed, are never "
        "re-pushed, and live under <home>/roam/session-archive/"
    )
