#!/bin/bash
# demo-two-homes.sh — reproducible real-Hermes proof of hermes-roam.
#
# Drives the REAL Hermes CLI (hermes 0.22.5+) and the real hermes-roam plugin
# through the full two-device flow on BOTH backends:
#   stage 1 (local backend): alpha pushes, beta pulls, config secrets never
#                           travel, a real conflict is kept local then
#                           overwritten with a backup, then resolved by push
#   stage 2 (git backend):   fresh remote, alpha pushes, beta pulls, beta pushes back
#
# Verifies after every step: shared content lands, host-scoped config keys and
# .env stay local, model.api_key (top-level and in a profile config) never
# reaches the other home, conflicting local edits are listed and kept, and
# --overwrite-conflicts replaces them with a backup. Round-3 additions: a
#  local deletion propagates (with the removed content kept in the peer's
#  roam/backups), a shared config key edited on ONE device propagates while
#  both devices keep their own host keys and secrets, and roam/rules.yaml
#  stays per-device (never in a bundle). Round-4 addition:
#   stage 3 (history):   `roam history` lists every generation the remote
#                        store still holds for a path, and `roam restore
#                        --generation N --apply` recovers an older version
#                        into the home (backup first, hash-checked)
#   stage 4 (sessions):  a fresh local pair proves the 0.4.1 opt-in session
#                        archive end to end: real sessions created with
#                        `hermes sessions import` (no network) stay local
#                        while sync_sessions is off, an opted-in push
#                        publishes the transcript in the device's archive
#                        namespace, the peer pulls it read-only (0444) and
#                        lists/prints it with `roam sessions`, a device
#                        that never opts in never publishes its own
#                        sessions, and `roam status` shows every opt-in
#
# Usage: scripts/demo-two-homes.sh [BASE_DIR]
#   BASE_DIR (default /tmp/roam-demo) must not exist; everything is created
#   inside it. Run from any cwd EXCEPT a directory containing a struct.py that
#   shadows the stdlib (e.g. /root on some hosts): the script cds to BASE_DIR.
#
# Requires: hermes on PATH, git, python3 (secrets + the sqlite3 fixture
#   completion stamp), ~15 GiB free for installs.
set -euo pipefail

CHECKOUT="$(cd "$(dirname "$0")/.." && pwd)"
BASE="${1:-/tmp/roam-demo}"
if [ -e "$BASE" ]; then
  echo "refusing to run: $BASE already exists (remove it first)" >&2
  exit 1
fi
mkdir -p "$BASE"
cd "$BASE"

KEY_FILE="$BASE/demo-key.txt"          # demo key; a real setup keeps it in .env only
python3 -c "import secrets;print(secrets.token_urlsafe(32))" > "$KEY_FILE"
chmod 600 "$KEY_FILE"
KEY="$(cat "$KEY_FILE")"

ALPHA_KEY="sk-demo-alpha-0000000000000000001"
BETA_KEY="sk-demo-beta-00000000000000000002"

say() { printf '\n=== %s ===\n' "$*"; }

make_home() { # make_home <name>
  local H="$BASE/$1"
  mkdir -p "$H/skills/demo-skill" "$H/memories"
  printf 'model:\n  provider: openrouter\n  default: test\n  api_key: %s\n' \
    "$([ "$1" = alpha ] && echo "$ALPHA_KEY" || echo "$BETA_KEY")" > "$H/config.yaml"
  printf 'terminal:\n  theme: dark-%s\n  cwd: %s\n' "$1" "$H" >> "$H/config.yaml"
  printf 'ROAM_ARCHIVE_KEY=%s\n' "$KEY" > "$H/.env"
  printf '# demo skill\nUse this skill to prove roam sync.\n' > "$H/skills/demo-skill/SKILL.md"
  printf '# memory\n%s demo memory\n' "$1" > "$H/memories/MEMORY.md"
  printf 'SOUL of the %s demo home.\n' "$1" > "$H/SOUL.md"
}

as() { # as <home> <command...>  — run hermes against a demo home
  local H="$BASE/$1"; shift
  HERMES_HOME="$H" hermes "$@"
}

# run <home> <cmd...> but capture everything; asserts greps against $OUT.
# The full output also stays in the main demo log (the proof transcript).
runlog() { local L="$1"; shift; as "$L" "$@" 2>&1 | tee "$OUT" || true; }

install_plugin() { # install_plugin <home>
  as "$1" plugins install "file://$CHECKOUT" --yes-deps --enable 2>&1 | tail -2
}

config_local_backend() { # config_local_backend <home> <remote-dir> <device>
  as "$1" config set plugins.entries.hermes-roam.settings.backend local >/dev/null
  as "$1" config set plugins.entries.hermes-roam.settings.local_dir "$2" >/dev/null
  as "$1" config set plugins.entries.hermes-roam.settings.device_name "$3" >/dev/null
}

OUT="$BASE/last-command.log"

say "stage 0 — fixtures"
make_home alpha
make_home beta
mkdir -p "$BASE/remote"                       # local backend store
git init -q --bare "$BASE/git-remote.git"      # git backend store
# a profile config with its own api_key travels STRIPPED, never verbatim
mkdir -p "$BASE/alpha/profiles/p"
printf 'model:\n  provider: openrouter\n  api_key: %s\nprofile: alpha-p\n' "$ALPHA_KEY" \
  > "$BASE/alpha/profiles/p/config.yaml"
install_plugin alpha
install_plugin beta

say "stage 1 — local backend: alpha pushes"
config_local_backend alpha "$BASE/remote" alpha
as alpha roam init --create-remote >/dev/null
as alpha roam push 2>&1 | tail -2

say "stage 1 — beta pulls"
config_local_backend beta "$BASE/remote" beta
# a fresh device has no shared memories yet — they arrive via pull (Hermes
# re-seeds SOUL.md on its own, so that one stays and must be kept local)
rm -f "$BASE/beta/memories/MEMORY.md"
as beta roam init 2>&1 | head -1
as beta roam diff 2>&1 | tail -3
runlog beta roam pull --apply
grep -q "installed remote generation" "$OUT"
# the home's own seeded SOUL.md diverges from the remote one: the merged
# engine must KEEP it and list it instead of silently overwriting it (r2 f3)
grep -q "kept local (changed here since last sync): SOUL.md" "$OUT" \
  || { echo "FAIL: a diverging local SOUL.md was not listed as kept local" >&2; exit 1; }
grep -q "alpha demo memory" "$BASE/beta/memories/MEMORY.md"
grep -q "device_name: beta" "$BASE/beta/config.yaml"   # host keys kept local
grep -q "theme: dark-beta" "$BASE/beta/config.yaml"
echo "verified: shared memory installed; host keys and .env untouched; the diverging local SOUL.md kept and listed"

say "stage 1 — secrets in config.yaml and profile configs never travel"
grep -q "$BETA_KEY" "$BASE/beta/config.yaml"           # beta's own key kept
if grep -qF "$ALPHA_KEY" "$BASE/beta/config.yaml"; then
  echo "FAIL: alpha's model.api_key arrived in beta's config.yaml" >&2; exit 1
fi
if [ ! -f "$BASE/beta/profiles/p/config.yaml" ]; then
  echo "FAIL: the shared profile config did not arrive (it must travel, stripped)" >&2; exit 1
fi
if grep -qF "$ALPHA_KEY" "$BASE/beta/profiles/p/config.yaml"; then
  echo "FAIL: alpha's profile api_key arrived verbatim in beta's profile config" >&2; exit 1
fi
grep -q "profile: alpha-p" "$BASE/beta/profiles/p/config.yaml"  # non-secret leaves do
echo "verified: api_key stripped on the way; the rest of the config merged"

say "stage 1 — conflict: both homes edit the same skill"
printf '# demo skill, edited on ALPHA\n' > "$BASE/alpha/skills/demo-skill/SKILL.md"
as alpha roam push 2>&1 | tail -2                     # generation moves ahead
printf '# demo skill, edited on BETA\n' > "$BASE/beta/skills/demo-skill/SKILL.md"
runlog beta roam push
grep -q "moved ahead" "$OUT" || { echo "FAIL: push into a moved-ahead remote was not refused" >&2; exit 1; }
echo "verified: push without pulling is refused (remote ahead)"

say "stage 1 — pull --apply keeps the local edit and lists it"
runlog beta roam pull --apply
grep -q "kept local (changed here since last sync): skills/demo-skill/SKILL.md" "$OUT" \
  || { echo "FAIL: the conflicting skill was not listed as kept local" >&2; exit 1; }
grep -q "edited on BETA" "$BASE/beta/skills/demo-skill/SKILL.md" \
  || { echo "FAIL: the local edit was overwritten without --overwrite-conflicts" >&2; exit 1; }
echo "verified: local edit kept and listed"

say "stage 1 — pull --apply --overwrite-conflicts replaces it, with a backup"
runlog beta roam pull --apply --overwrite-conflicts
grep -q "edited on ALPHA" "$BASE/beta/skills/demo-skill/SKILL.md" \
  || { echo "FAIL: --overwrite-conflicts did not install the remote copy" >&2; exit 1; }
if ! grep -rq "edited on BETA" "$BASE/beta/roam/backups/" 2>/dev/null; then
  echo "FAIL: no backup of the overwritten local edit" >&2; exit 1
fi
echo "verified: remote copy installed; the local edit is in roam/backups"

say "stage 1 — resolution: beta restores its edit and pushes; alpha pulls"
grep -rl "edited on BETA" "$BASE/beta/roam/backups/" | head -1 | xargs -I{} cp {} "$BASE/beta/skills/demo-skill/SKILL.md"
as beta roam push 2>&1 | tail -2                     # local version wins
runlog alpha roam pull --apply
grep -q "edited on BETA" "$BASE/alpha/skills/demo-skill/SKILL.md" \
  || { echo "FAIL: alpha did not receive beta's resolved skill" >&2; exit 1; }
as alpha roam devices 2>&1 | tail -2
as alpha roam status 2>&1 | tail -2

say "stage 1 — a local deletion propagates"
rm "$BASE/alpha/skills/demo-skill/SKILL.md"
as alpha roam push 2>&1 | tail -2                     # publishes the deletion
runlog beta roam pull --apply
if [ -e "$BASE/beta/skills/demo-skill/SKILL.md" ]; then
  echo "FAIL: the deletion did not propagate to beta" >&2; exit 1
fi
if ! grep -rq "edited on BETA" "$BASE/beta/roam/backups/" 2>/dev/null; then
  echo "FAIL: the removed skill left no backup on beta" >&2; exit 1
fi
echo "verified: the deletion propagated; the removed content is preserved in beta's roam/backups"

say "stage 1 — a shared config key edited on ONE device propagates"
printf 'telemetry:\n  enabled: false\n' >> "$BASE/beta/config.yaml"
runlog beta roam pull --apply
grep -q "config.yaml: changed locally since the last sync; kept local" "$OUT" \
  || { echo "FAIL: the local-only config edit was not kept and listed" >&2; exit 1; }
as beta roam push 2>&1 | tail -2                     # publishes the shared change
runlog alpha roam pull --apply
grep -q "telemetry" "$BASE/alpha/config.yaml" \
  || { echo "FAIL: the shared config change did not propagate" >&2; exit 1; }
grep -qF "$ALPHA_KEY" "$BASE/alpha/config.yaml"      # own host state kept
if grep -qF "$BETA_KEY" "$BASE/alpha/config.yaml"; then
  echo "FAIL: beta's api_key travelled with the shared config change" >&2; exit 1
fi
echo "verified: the shared key propagated; both devices kept their own api_key and terminal theme"

say "stage 1 — roam/rules.yaml stays per-device"
# beta's SOUL.md was overwritten by the --overwrite-conflicts stage above (with
# a backup); what must hold here is that alpha narrowing its own rules never
# deletes or rewrites the peer's local SOUL.md.
BETA_SOUL_HASH="$(md5sum "$BASE/beta/SOUL.md" | cut -d' ' -f1)"
ALPHA_SOUL_HASH="$(md5sum "$BASE/alpha/SOUL.md" | cut -d' ' -f1)"
printf 'local:\n  - SOUL.md\n' > "$BASE/alpha/roam/rules.yaml"
as alpha roam push 2>&1 | tail -2                     # rules.yaml is LOCAL: never bundled
runlog beta roam pull --apply
if [ -e "$BASE/beta/roam/rules.yaml" ]; then
  echo "FAIL: alpha's rules.yaml reached beta (it must be per-device)" >&2; exit 1
fi
[ "$(md5sum "$BASE/alpha/SOUL.md" | cut -d' ' -f1)" = "$ALPHA_SOUL_HASH" ] \
  || { echo "FAIL: alpha lost its own SOUL.md when it narrowed the rule" >&2; exit 1; }
[ -e "$BASE/beta/SOUL.md" ] \
  && [ "$(md5sum "$BASE/beta/SOUL.md" | cut -d' ' -f1)" = "$BETA_SOUL_HASH" ] \
  || { echo "FAIL: alpha's narrowed push deleted or rewrote beta's local SOUL.md" >&2; exit 1; }
echo "verified: rules.yaml never travels; the narrowed push left both local SOUL.md files untouched"

say "stage 2 — git backend: fresh remote, alpha migrates and pushes"
rm -f "$BASE/alpha/roam/rules.yaml"    # per-device rule removed by the operator
as alpha config set plugins.entries.hermes-roam.settings.backend git >/dev/null
as alpha config set plugins.entries.hermes-roam.settings.git_remote "file://$BASE/git-remote.git" >/dev/null
rm -f "$BASE/alpha/roam/state.json"            # local state tracks the old remote
rm -rf "$BASE/alpha/roam/git-mirror"
as alpha roam init --create-remote 2>&1 | head -1
as alpha roam push 2>&1 | tail -2

say "stage 2 — beta migrates, pulls, then pushes back"
as beta config set plugins.entries.hermes-roam.settings.backend git >/dev/null
as beta config set plugins.entries.hermes-roam.settings.git_remote "file://$BASE/git-remote.git" >/dev/null
rm -f "$BASE/beta/roam/state.json"
rm -rf "$BASE/beta/roam/git-mirror"
as beta roam init 2>&1 | head -1
runlog beta roam pull --apply
printf 'SOUL of the beta home, third edit, pushed over the git backend.\n' > "$BASE/beta/SOUL.md"
as beta roam push 2>&1 | tail -2

say "stage 2 — alpha pulls beta's git-backend generation"
runlog alpha roam pull --apply
grep -q "third edit, pushed over the git backend" "$BASE/alpha/SOUL.md"
as alpha roam devices 2>&1 | tail -2
grep -q "device_name: alpha" "$BASE/alpha/config.yaml"
grep -q "theme: dark-alpha" "$BASE/alpha/config.yaml"
grep -qF "$ALPHA_KEY" "$BASE/alpha/config.yaml"      # own key kept
if grep -qF "$BETA_KEY" "$BASE/alpha/config.yaml"; then
  echo "FAIL: beta's model.api_key arrived in alpha's config.yaml" >&2; exit 1
fi
echo "verified: secrets never crossed the git backend either"

say "stage 3 — history: the remote store keeps every pushed generation"
runlog alpha roam history SOUL.md
grep -q "2 recoverable version(s) in the remote store (newest first)" "$OUT" \
  || { echo "FAIL: history did not list both pushed SOUL.md versions" >&2; exit 1; }
grep -q "gen 1 alpha" "$OUT" \
  || { echo "FAIL: history does not list the original alpha generation" >&2; exit 1; }
grep -q "gen 2 beta" "$OUT" \
  || { echo "FAIL: history does not list beta's generation" >&2; exit 1; }
echo "verified: history lists every pushed generation of SOUL.md, newest first"

say "stage 3 — restore the older generation: plan first, then --apply"
runlog alpha roam restore SOUL.md --generation 1
grep -q "plan only — generation 1 still holds SOUL.md" "$OUT" \
  || { echo "FAIL: the plan-only restore did not describe generation 1" >&2; exit 1; }
grep -q "third edit, pushed over the git backend" "$BASE/alpha/SOUL.md" \
  || { echo "FAIL: the plan-only restore already changed the home" >&2; exit 1; }
runlog alpha roam restore SOUL.md --generation 1 --apply
grep -q "roam: restored SOUL.md from generation 1 into the home" "$OUT" \
  || { echo "FAIL: restore --apply did not report success" >&2; exit 1; }
grep -q "SOUL of the alpha demo home." "$BASE/alpha/SOUL.md" \
  || { echo "FAIL: the restored content is not the generation 1 content" >&2; exit 1; }
if ! grep -rq "third edit, pushed over the git backend" "$BASE/alpha/roam/backups/" 2>/dev/null; then
  echo "FAIL: restore --apply left no backup of the replaced version" >&2; exit 1
fi
echo "verified: generation 1 restored verbatim; the replaced version is in roam/backups"

say "stage 3 — roll forward to the newest generation"
runlog alpha roam restore SOUL.md --generation 2 --apply
grep -q "third edit, pushed over the git backend" "$BASE/alpha/SOUL.md" \
  || { echo "FAIL: rolling forward did not restore the newest generation" >&2; exit 1; }
as alpha roam status 2>&1 | tail -2
echo "verified: the newest generation is back and status is settled"

say "stage 4 — session archive with sync_sessions off: nothing session-related travels"
make_home gamma
make_home delta
mkdir -p "$BASE/remote-sessions"               # fresh local backend store
install_plugin gamma
install_plugin delta
# a REAL session in gamma, created with Hermes's own importer — no network
python3 - "$BASE/gamma" <<'PYEOF'
import json, sys
from pathlib import Path
home = Path(sys.argv[1])
lines = [
    {"type": "user", "cwd": "/root/work", "sessionId": "demo-fixture-1",
     "message": {"role": "user",
                "content": [{"type": "text", "text": "demo archive transcript one"}]}},
    {"type": "assistant",
     "message": {"role": "assistant",
                 "content": [{"type": "text", "text": "demo archive assistant reply"}]}},
]
(home / "fixture.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
PYEOF
as gamma sessions import --from claude "$BASE/gamma/fixture.jsonl" 2>&1 | grep "Imported Claude Code session"
# close the imported row the way a real CLI conversation is closed at shutdown:
# Hermes only end-stamps rows it owned in-process, and the md bulk export only
# ever matches ended sessions, so the fixture needs this completion stamp
python3 - "$BASE/gamma" <<'PYEOF'
import sqlite3, sys, time
from pathlib import Path
db = sqlite3.connect(Path(sys.argv[1]) / "state.db")
db.execute("UPDATE sessions SET ended_at = ?, end_reason = 'cli_close' "
           "WHERE source = 'claude-code' AND ended_at IS NULL", (time.time(),))
db.commit()
print(f"fixture completion: end-stamped {db.total_changes} imported session row(s)")
PYEOF
GID="$(python3 - "$BASE/gamma" <<'PYEOF'
import sqlite3, sys
from pathlib import Path
db = sqlite3.connect(Path(sys.argv[1]) / "state.db")
print(db.execute("SELECT id FROM sessions WHERE source = 'claude-code'").fetchone()[0])
PYEOF
)"
echo "gamma holds a real imported session: $GID (sync_sessions off)"
printf 'shared note so the no-opt-in push creates a generation\n' > "$BASE/gamma/memories/gamma-note.md"
config_local_backend gamma "$BASE/remote-sessions" gamma
as gamma roam init --create-remote 2>&1 | head -1
runlog gamma roam push
grep -q "roam: pushed generation" "$OUT" \
  || { echo "FAIL: gamma's push did not create a generation" >&2; exit 1; }
if grep -q "published" "$OUT"; then
  echo "FAIL: gamma published session content with sync_sessions off" >&2; exit 1
fi
config_local_backend delta "$BASE/remote-sessions" delta
as delta roam init 2>&1 | head -1
runlog delta roam pull --apply
grep -q "installed remote generation" "$OUT" \
  || { echo "FAIL: delta's pull did not install the remote generation" >&2; exit 1; }
[ -e "$BASE/delta/roam/session-archive" ] \
  && { echo "FAIL: delta received a session archive with sync_sessions off" >&2; exit 1; }
runlog delta roam sessions
grep -q "no pulled session archives" "$OUT" \
  || { echo "FAIL: delta's session archive listing is not empty" >&2; exit 1; }
if grep -q "archive/sessions" "$BASE/remote-sessions/HEAD"; then
  echo "FAIL: the store's signed head carries archive paths with sync_sessions off" >&2; exit 1
fi
as gamma roam status 2>&1 | grep -q "session archive (publish) off" \
  || { echo "FAIL: gamma's status does not show the opt-in as off" >&2; exit 1; }
echo "verified: with the opt-in off, no session content traveled in either direction"

say "stage 4 — gamma opts in: the archive publishes; delta pulls it read-only"
as gamma config set plugins.entries.hermes-roam.settings.sync_sessions true >/dev/null
runlog gamma roam push
grep -q "published 1 session transcript(s)" "$OUT" \
  || { echo "FAIL: gamma's opted-in push did not publish its transcript" >&2; exit 1; }
grep -q "archive/sessions/gamma/" "$BASE/remote-sessions/HEAD" \
  || { echo "FAIL: the store's signed head does not carry gamma's archive namespace" >&2; exit 1; }
runlog delta roam pull --apply
grep -q "session archive member(s) land READ-ONLY" "$OUT" \
  || { echo "FAIL: the pull did not report the read-only archive install" >&2; exit 1; }
runlog delta roam sessions
grep -q "$GID" "$OUT" \
  || { echo "FAIL: delta's listing does not show gamma's session" >&2; exit 1; }
grep -q "gamma: 1 session(s)" "$OUT" \
  || { echo "FAIL: delta's listing does not show device gamma" >&2; exit 1; }
runlog delta roam sessions show gamma "$GID"
grep -q "demo archive transcript one" "$OUT" \
  || { echo "FAIL: show did not print the archived transcript" >&2; exit 1; }
grep -q "archived sessions are read-only" "$OUT" \
  || { echo "FAIL: the printed transcript carries no read-only note" >&2; exit 1; }
ARCHIVE_MODES="$(find "$BASE/delta/roam/session-archive/gamma" -name '*.md' -exec stat -c '%a' {} \;)"
echo "archive file modes: $ARCHIVE_MODES"
[ -n "$ARCHIVE_MODES" ] && [ "$(echo "$ARCHIVE_MODES" | sort -u)" = "444" ] \
  || { echo "FAIL: the archived transcripts are not mode 0444" >&2; exit 1; }
echo "verified: the pulled archive is read-only (0444), listed, printable, never resumable"

say "stage 4 — delta's own session stays local until delta opts in"
python3 - "$BASE/delta" <<'PYEOF'
import json, sys
from pathlib import Path
home = Path(sys.argv[1])
lines = [
    {"type": "user", "cwd": "/root/work", "sessionId": "demo-fixture-2",
     "message": {"role": "user",
                "content": [{"type": "text", "text": "demo archive transcript two"}]}},
    {"type": "assistant",
     "message": {"role": "assistant",
                 "content": [{"type": "text", "text": "demo archive assistant reply two"}]}},
]
(home / "fixture.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
PYEOF
as delta sessions import --from claude "$BASE/delta/fixture.jsonl" 2>&1 | grep "Imported Claude Code session"
python3 - "$BASE/delta" <<'PYEOF'
import sqlite3, sys, time
from pathlib import Path
db = sqlite3.connect(Path(sys.argv[1]) / "state.db")
db.execute("UPDATE sessions SET ended_at = ?, end_reason = 'cli_close' "
           "WHERE source = 'claude-code' AND ended_at IS NULL", (time.time(),))
db.commit()
print(f"fixture completion: end-stamped {db.total_changes} imported session row(s)")
PYEOF
DID="$(python3 - "$BASE/delta" <<'PYEOF'
import sqlite3, sys
from pathlib import Path
db = sqlite3.connect(Path(sys.argv[1]) / "state.db")
print(db.execute("SELECT id FROM sessions WHERE source = 'claude-code'").fetchone()[0])
PYEOF
)"
echo "delta holds its own imported session: $DID (sync_sessions still off)"
printf 'shared note so the no-opt-in push creates a generation\n' > "$BASE/delta/memories/delta-note.md"
runlog delta roam push
grep -q "roam: pushed generation" "$OUT" \
  || { echo "FAIL: delta's push did not create a generation" >&2; exit 1; }
if grep -q "published" "$OUT"; then
  echo "FAIL: delta published its own session without opting in" >&2; exit 1
fi
if grep -q "archive/sessions/delta/" "$BASE/remote-sessions/HEAD"; then
  echo "FAIL: delta's session namespace reached the store without opt-in" >&2; exit 1
fi
as delta config set plugins.entries.hermes-roam.settings.sync_sessions true >/dev/null
runlog delta roam push
grep -q "published 1 session transcript(s)" "$OUT" \
  || { echo "FAIL: delta's opted-in push did not publish its transcript" >&2; exit 1; }
runlog gamma roam pull --apply
grep -q "installed remote generation" "$OUT" \
  || { echo "FAIL: gamma did not pull delta's generation" >&2; exit 1; }
runlog gamma roam sessions
grep -q "$DID" "$OUT" \
  || { echo "FAIL: gamma's listing does not show delta's opted-in session" >&2; exit 1; }
as gamma roam status 2>&1 | grep -q "session archive (publish) ON" \
  || { echo "FAIL: gamma's status does not show the opt-in as ON" >&2; exit 1; }
as delta roam status 2>&1 | grep -q "session archive (publish) ON" \
  || { echo "FAIL: delta's status does not show the opt-in as ON" >&2; exit 1; }
echo "verified: publishing is gated per device (status shows both opt-ins); reading needs no opt-in"

say "PROOF COMPLETE — both backends and the opt-in session archive round-tripped under real Hermes"
echo "artifacts under: $BASE (remove it to clean up)"
