# Hermes Roam

Encrypted, host-scoped sync of your Hermes Agent home between your own machines.

You move between a desktop and a laptop, and your Hermes setup drifts: the skill
you wrote on one machine is missing on the other, your SOUL and memories are
stale, and your `config.yaml` disagrees with itself. Hermes Roam fixes that:
one device pushes an encrypted snapshot of the shared home to storage you own
— an S3 bucket, a Cloudflare R2 bucket, or a private git repository — and the
other device pulls it back with a reviewed, backed-up, roll-back-able install.
Nothing is merged silently, nothing is synced in the background without your
consent, and secrets never leave the machine.

This answers the roaming-home thread at
[nousresearch/hermes-agent#20510](https://github.com/NousResearch/hermes-agent/issues/20510):

- **Host-specific configuration stays local.** Terminal settings, paths,
  display interface, browser executable, and MCP server commands are stripped
  from the pushed `config.yaml` and kept from the local copy on pull; any
  config value that is an absolute path under your home directory is detected
  and treated the same way, without being listed.
- **Only an explicit allowlist ever syncs.** `config.yaml` (minus
  host-scoped keys), `SOUL.md`, `memories/`, `skills/`, `skins/`, and
  `hermes-pack.yaml` are the shared set; every other path — credentials,
  caches, runtime state, hooks — stays on its machine. A per-home
  `roam/rules.yaml` extends or narrows the allowlist; user rules override the
  built-in defaults.
- **Secrets never leave the machine.** `.env`, `auth.json`, and every other
  credential file are classified local by default and no bundle ever contains
  them. To have the same API keys on every machine, let each device fetch
  them from a secret source instead: Hermes's built-in Bitwarden Secrets Manager source, or a
  catalog secret-source plugin such as `vaultwarden`, `pass-secrets`,
  `hermes-wincred` or `hermes-dpapi-secrets`.
- **Nothing installs without a human decision.** A pull only previews until
  you pass `--apply`; the agent's `roam_pull` tool cannot apply at all
  unless you enable `allow_agent_apply` (default false); and an unreachable
  remote is reported as an error — never as "the remote is ahead".
- **Conflicts are detected, not clobbered.** The remote head is signed and
  compare-and-swap protected; a device that is behind must pull before it can
  push. There is no silent overwrite.

## Quick start (60 seconds)

On the first machine:

```sh
# 1. Install. `hermes plugins install hermes-roam` works once the plugin is
#    admitted to the Hermes catalog; until then install from a local
#    checkout of this repository instead (the file:// prefix is required —
#    a bare path is read as a GitHub owner/repo shorthand):
hermes plugins install file:///path/to/hermes-roam

# 2. Generate the archive key (32 random bytes) and keep it private
python -c "import secrets;print(secrets.token_urlsafe(32))"

# 3. Put it in {home}/.env (the active Hermes home's .env — your hand,
#    never paste secrets into chat) as ROAM_ARCHIVE_KEY=..., then
#    configure the backend. S3/Cloudflare R2:
hermes config set plugins.entries.hermes-roam.settings.backend s3
hermes config set plugins.entries.hermes-roam.settings.s3_bucket hermes-roam
hermes config set plugins.entries.hermes-roam.settings.s3_endpoint_url https://<account_id>.r2.cloudflarestorage.com
#    (git instead: settings.backend git + settings.git_remote <private repo
#    URL>; a plain shared directory for tests: settings.backend local +
#    settings.local_dir /shared/dir)

# 4. Initialize, verify, push
hermes roam init --create-remote
hermes roam doctor
hermes roam push
```

On the second machine: install the plugin, put the **same** `ROAM_ARCHIVE_KEY`
(and the S3 credentials, if using S3/R2) in that profile's `.env`, set the same
backend settings, then:

```sh
hermes roam pull            # prints the plan; nothing is written yet
hermes roam pull --apply    # installs after backing up replaced files
```

Host-specific keys in the local `config.yaml` survive the pull; the shared
parts (model settings, skills, memories, SOUL) arrive. The bundled
`roam-setup` skill walks a user through all of this in chat.

## What syncs and what stays local

Every file in the home is classified by its relative path, first match wins,
before anything is read for a bundle. The defaults are an ALLOWLIST: only the
named shared paths ever travel, and any path no rule matches stays on its
machine — an unknown file can never leak into a bundle. The default classes:

**Shared — always synced:** `config.yaml` (minus the host-scoped keys below),
`SOUL.md`, `memories/`, `skills/`, `skins/`, `hermes-pack.yaml`,
and the same set inside `profiles/*/`.

**Local — never leaves this machine:** everything the allowlist does not
name, plus the explicit rules: `.env`, `auth.json`, `auth.json.*`, lock
files (`*.lock`, `auth.lock`), `state.db` and its sidecars,
`deliveries.db*`, `executions.db*`, `gateway/`, `gateway-service/`, `bin/`,
`hermes-agent/`, `plugin-data/`, `desktop/`, per-profile homes
(`profiles/*/home/`), `context_length_cache.yaml*`,
`channel_directory.json`, `*.bak*`, `*.before-*`, `*.pid`, `*.tmp`,
`node_modules/`, `installs/`, the plugin code trees under `plugins/`,
`hooks/` (see below), `tui-widgets/`, `desktop-plugins/`, `pets/`,
`roam/rules.yaml`, and everything else under `<home>/roam/`.

**Hooks are local by default.** Hermes loads `hooks/` at startup, so syncing
executable hooks between machines would run remote code on the other device.
If you want them to travel anyway, opt in via a `shared:` entry in
`roam/rules.yaml` — you are choosing to run the other machine's code.

**Opt-in, off by default:** `sessions/` (transcripts) and `cron/` (job
definitions), at the top level and inside each profile. They sync only when
`sync_sessions` / `sync_cron` is enabled or `pull --sessions` / `--cron` is
passed.

**Ignored — never even listed:** `cache/`, `audio_cache/`, `image_cache/`,
`logs/`, `backups/`, `bootstrap-cache/`, `checkpoints/`, `sandboxes/`,
`state-snapshots/`, `tmp/`, `__pycache__/`, `.git/`, `venv/`, `.venv/`,
`reports/`, `profile-archives/`, and the Hermes-managed dot-state inside the
skills tree (`skills/.*`: the hub lockfile tree `.hub/`, `.locks/`,
`.archive/`, `.curator_backups/`, usage and curator bookkeeping).

**Hub-installed skills never travel.** Hermes records every skills-hub
install in `skills/.hub/lock.json` — the same marker its own code reads —
and roam turns each recorded `install_path` into a local rule. Hub installs
are reproducible from the hub, so the other device reinstalls them there
instead of trusting skill code that crossed a bundle; your own written
skills under `skills/` keep syncing as files.

The same rules are re-applied inside every `profiles/<name>/` subtree with
the more restrictive class winning, so a profile's `.env` is local and its
caches are ignored exactly like at the top level.

### config.yaml merging and host-scoped keys

On push, these keys are removed from the bundled `config.yaml`; on pull, your
local values for them are kept and everything else comes from the remote:
`terminal`, `terminal.*`, `paths`, `paths.*`, `display.interface`,
`browser.executable`, `mcp_servers.*.command`, and
`plugins.entries.hermes-roam` (this plugin's own settings). In addition, any
config value that is an absolute path under a user home directory
(`/root/...`, `/home/...`, `/Users/...`, `C:\Users\...`) is detected and
treated as host-scoped without being listed — it is stripped on push and the
local value wins on pull.

### Overriding the scope rules

Create `<home>/roam/rules.yaml` (or run `hermes roam rules --init`, which
writes a commented template and never overwrites an existing file):

```yaml
shared:
  - skills/my-cross-platform-skill/**
  - hooks/        # executable code: only if you accept running the other machine's code
local:
  - notebooks/
ignored:
  - vm-images/
host_keys:
  - custom.host.key
```

`shared:` entries extend the allowlist (this is how `hooks/`, `tui-widgets/`,
or a hub-installed skill tree can be made to travel); `local:` and `ignored:`
entries narrow it further and always win over the defaults. User rules take
precedence over the built-in defaults and are matched first; unknown keys and
non-list values are rejected with a configuration error. `hermes roam rules`
prints the effective rule set.

`roam/rules.yaml` is a per-device file: it is local, never synced, and never
leaves this machine. Each device's rules decide what that device can see and
publish — two devices in the same fleet may legitimately run different rules,
and a file neither device's rules allow stays put on both.

### Installed plugins and hub skills

Plugin code is never synced: each tree under `plugins/` is a checkout whose
provenance Hermes owns. Instead, roam records every installed plugin's name,
version, and pinned commit in `roam/installed-plugins.json`, which is synced.
After a pull, roam lists the plugins the remote home has that this one lacks
and tells you to install them with `hermes plugins install <name>` — it never
installs code by itself.

Skills installed from the skills hub are treated the same way: Hermes marks
them in `skills/.hub/lock.json`, roam keeps those trees local, and the other
device reinstalls them from the hub. Skills you wrote yourself under
`skills/` sync as ordinary files.

## How it is safe

**Encryption before upload.** A bundle is a zip of the shared files sealed as
a single AES-256-GCM envelope: a 12-byte random nonce, and the canonical-JSON
header (version, device, generation, digests) authenticated as AAD. The key is
32 bytes from the `ROAM_ARCHIVE_KEY` secret (base64 or 64 hex characters) and
must be identical on every device; a wrong key fails authentication before
anything is extracted. Zip extraction refuses absolute names, `..`, control
characters, duplicate members, symlinks and special files, encrypted members,
unsupported compression, and per-file or total overruns against the size limit
(default 2 GiB, configurable), and writes only through exclusive creates
inside `0700` directories with fsyncs along the way.

**A signed head, not a mutable directory listing.** The remote holds one
small signed object (`HEAD`): the current generation, the file manifest with
SHA-256 per file, and the device roster. It is signed with an HMAC-SHA256 key
derived from your archive key (HKDF, so the head key is not the bundle key).
Every status, diff, push, and pull verifies that signature first; a tampered
or foreign head fails verification instead of being trusted.

**Conflict detection, not silent clobbering.** Head updates are
compare-and-swap: S3 rejects a stale conditional write with HTTP 412, the git
backend pushes with `--force-with-lease` on the previously seen commit, and
bundles are immutable objects that cannot be overwritten. When another device
pushed first, `hermes roam status` reports
`remote  <N> generations ahead — run 'hermes roam pull'`, and `hermes roam
push` exits with code 2 and prints
`the remote head is at generation X but this device last saw Y; pull first
(or push with --force)`. There is no merge: one writer at a time, and the
loser is told to pull.

**The conflict model covers deletions and configs, not just edits.** A file
the last synced bundle managed that is now missing locally is a local
deletion, not an accident: if the remote also changed it, that is a conflict
kept local like any other; otherwise the deletion stands and this device's
next push publishes it. `config.yaml` is compared by its shared projection:
a local-only change is kept (and published by the next push), a remote-only
change is merged as described below, and a file that changed on both sides is
a config conflict — kept local, listed by name in the plan, and overwritten
only with `--overwrite-conflicts`. The pull plan states all of this before
anything is written: it lists the config files it would merge, the config
conflicts it would keep local, the unchanged files it will not rewrite, and
every added, changed or removed path.

**Carry-forward: a narrower device never deletes what it cannot see.** After
every successful pull or push the bundle just synced is kept (still
AES-GCM encrypted) at `<home>/roam/last.roam`. A push publishes the files
this device can see under its own rules and opt-ins union every entry of the
last synced bundle's base manifest whose path this device cannot see — those
entries' bytes are carried forward from `last.roam`, so a device whose scope
rules are narrower can never wipe files for the rest of the fleet. Only a
device that can see a path can delete it; if `last.roam` is missing and such
entries exist, the push refuses and tells you to pull first.

**Rollback floor.** `<home>/roam/floor.json` records the lowest remote
generation this remote identity may ever serve, keyed by the remote itself
(backend plus bucket/prefix, or git remote with credentials stripped plus
branch, or local directory). Deleting `state.json` does not reset it and a
new remote starts a new floor, so a fleet cannot be silently rolled back to
generation 0 by wiping local state; `hermes roam doctor` fails a `rollback`
check whenever the remote head is below the floor.

**Journaled, backed-up installs.** `hermes roam pull` alone only downloads,
verifies, and prints the plan — nothing is written. `hermes roam pull
--apply` installs through a journal: replaced files are first copied to
`<home>/roam/backups/<timestamp>/`, each write is recorded, and any failure
mid-install rolls the whole install back to that backup. `config.yaml` is
merged key-by-key as described above, never wholesale. Conflicting files —
local content that changed here since the last sync while the remote also
changed it — are never silently dropped: the install lists each one as
`kept local (changed here since last sync): <path>`, together with the
paths your local scope rules keep out (`kept local by your scope rules:`),
and everything else applies. To take the remote version of those files too,
pass `--overwrite-conflicts` (the local copy is still backed up first);
without it the conflicting paths are not written or deleted, only listed.
Files whose content is already identical to the remote copy are counted as
unchanged — they are never rewritten and no backup is taken for them, and
the plan says so. Only the newest `backup_keep` backup directories are kept
(default 5); after each successful install the older ones and their journals
are pruned, so the backup directory cannot grow without bound. When the
engine rolls a crashed previous operation back, the command prints
`roam: recovered: <what was rolled back>` so a half-finished install never
masquerades as a clean one — and a recovery whose backup is missing never
deletes the file: the journal is marked `unrecoverable` and the command
surfaces it instead of silently destroying anything.

**Retention and recovery (0.4).** The retention model has two layers. Locally,
`backup_keep` (default 5) is only a crash-recovery window: the newest
`<home>/roam/backups/` directories are kept so a failed install can be rolled
back, and older ones are pruned after each successful install so the home
cannot grow without bound — the local window is not an archive. Remotely,
the store is an archive: every generation ever pushed stays in it and roam
never deletes a stored bundle, so nothing pushed is ever lost to the
`backup_keep` window. `hermes roam history [path]` enumerates what that
store still holds — one line per path change (generation, device, date,
digest, size, and whether that generation deleted the path) — and it only
trusts bundles that decrypt and self-match under the fleet's archive key;
since 0.4 each signed head also names its parent bundle so the chain itself
is verifiable. `hermes roam restore <path> --generation N --apply` recovers
one version through the same journaled, backed-up install as a pull, refuses
paths that are local or ignored under this device's rules, merges configs
as shared-projection-plus-local-host-keys instead of writing them verbatim,
and leaves the restored version marked as a local change so the next push
publishes it. To prune remote history manually (storage cost is yours to
manage), delete old bundle objects from the store yourself — e.g. old
`bundles/` keys in the R2/S3 bucket or old commits touching `bundles/` in
the git repository — after checking `hermes roam history` for what you are
giving up; roam will simply report those generations as no longer
recoverable.

**Dry runs.** `hermes roam push --dry-run` refuses outright before
`hermes roam init` has written local state, and otherwise lists the bundle's
file paths (capped at 40 lines, then `... N more`) together with the total
size, without uploading anything. `hermes roam pull --dry-run` stops after
downloading and decrypting the remote bundle — nothing is written.

**Unreachable backends are reported, never guessed.** When the remote
cannot be reached, `hermes roam status` prints
`roam: remote unreachable: <error>` and exits 1 — it never computes an
ahead/behind difference from a missing generation, and never claims the
remote is ahead. The `/roam status` slash output and the `roam_status`
tool report the same error; the tool's result sets `remote_ahead` to
false and tells the model not to pull and not to claim the remote is
ahead. Recovery is a human decision with the real error in hand.

**The model cannot install by itself.** The `roam_pull` tool with
`apply=true` is refused unless the `allow_agent_apply` setting is enabled
(default false, and it fails closed when no configuration is available at
all). The refusal names the setting and tells the model to ask the user to
run `/roam pull apply` or `hermes roam pull --apply` in person. The
journaled install is the user's decision, not the model's.

**Honesty checks.** `hermes roam doctor` verifies the key decodes to 32
bytes, the backend is reachable, the head signature verifies, the remote
head is not below the rollback floor, the remote Hermes version is not
newer than this one, the estimated bundle fits the size limit, the staging
and backup directories are writable, and (git backend) that git is
installed. A pull from a newer Hermes is refused outright.

## Backends

**S3 / Cloudflare R2** (`backend: s3`). Settings: `s3_bucket`,
`s3_prefix` (default `hermes-roam`), `s3_endpoint_url`, `s3_region`.
Credentials come from the `ROAM_S3_ACCESS_KEY_ID` and
`ROAM_S3_SECRET_ACCESS_KEY` secrets — checked at runtime, never written to
disk by this plugin. For R2, the endpoint is
`https://<account_id>.r2.cloudflarestorage.com` and the region is the literal
`auto`; scope the API token to Object Read & Write on that one bucket. R2's
free tier (10 GB storage, zero egress fees) covers a typical Hermes home.
Compare-and-swap uses `IfMatch`/`IfNoneMatch` conditional writes, which both
AWS S3 and R2 support. Proven against real Cloudflare R2, not only against
the moto emulator: the full backend contract (conditional writes rejected
with real HTTP 412, create-once bundles, size caps, listing) and a complete
two-home sync — init, push, stale-push refusal at exit 2, pull --apply,
history and restore of every generation, green doctor — ran live against a
real R2 bucket in November 2026; transcript in `docs/R2_PROOF.md`.

**Private git repository** (`backend: git`). Settings: `git_remote` (the SSH
or HTTPS URL exactly as you configured it — roam never stores git
credentials; authentication is your existing SSH setup or credential helper),
`git_branch` (default `roam`). Roam keeps one mirror clone per home under
`<home>/roam/git-mirror/`, builds commits with git plumbing without a
checkout, and protects the head with `--force-with-lease` on the seen
commit. Every git call is an argv list with a hard timeout and terminal
prompts disabled, so an unauthenticated remote fails fast instead of
hanging. Size guidance: a git transfer is bounded by `git_transfer_timeout`
(default 600 s) inside the one-per-operation budget, so a slow link fails
after a real attempt rather than instantly — but git hosts also cap file
sizes (GitHub at 100 MB per file), and the whole shared home is one bundle
file. Keep the shared home well under ~50 MB on git; anything larger belongs
on S3/R2, which the 2 GiB bundle limit allows.

**Local directory** (`backend: local`). Settings: `local_dir`. Both
machines must reach the same disk; the head update takes an exclusive `flock`
and compares hashes (POSIX only). This backend exists for tests and demos —
do not use it as your real sync.

## Commands

**CLI** (`hermes roam <command> [--json]`; exit codes 0 ok, 1 error,
2 conflict):

- `hermes roam init [--create-remote]` — validate settings, ping the backend,
  create the generation-0 head on the first device, write local state.
- `hermes roam status` — local and remote generation, remote-ahead flag,
  local dirty count (what a push would carry), fleet size. When the backend
  is unreachable it prints `roam: remote unreachable: <error>` and exits 1
  instead of guessing.
- `hermes roam devices` — the roster of devices that ever pushed or pulled,
  with their last push/pull generations and Hermes versions.
- `hermes roam diff` — pull preview: the added / changed / removed paths a
  pull would install, grouped by scope class.
- `hermes roam push [--force] [--dry-run]` — upload a new encrypted
  generation; refuses with exit 1 before `init` has written local state,
  refuses when the remote moved ahead (exit 2) unless forced. `--dry-run`
  lists the bundle's file paths (40-line cap) and total size.
- `hermes roam pull [--apply] [--overwrite-conflicts] [--dry-run]
  [--sessions] [--cron]` — download and verify; `--apply` performs the
  journaled, backed-up install and lists every path it kept local
  (conflicts and scope-kept paths) instead of silently dropping it;
  `--overwrite-conflicts` takes the remote version of conflicting paths.
- `hermes roam doctor` — the health checks above; non-zero exit on failure.
- `hermes roam history [path] [--json]` — list every version the remote
  store still holds (newest generation first, 40-line cap in human output):
  generation, device, date, digest, size, and whether that generation
  deleted the path. Read-only; only bundles that verify under the fleet's
  archive key are listed.
- `hermes roam restore <path> --generation N [--apply]` — recover one file
  version from the store. Without `--apply` nothing is written. With
  `--apply` the version goes through the journaled, backed-up install, is
  refused for paths that are local or ignored under this device's rules, is
  merged (never written verbatim) for configs, and is left marked as a
  local change so the next push publishes it. There is no agent tool for
  this and no slash form: recovery is the user's decision on the terminal.
- `hermes roam rules [--init]` — print the effective scope rules or write the
  commented template.

**Slash command** (short, Telegram-friendly output, no tables):
`/roam status`, `/roam push`, `/roam pull`, `/roam pull apply` (the only
form that writes), `/roam pull apply overwrite` (also overwrites
conflicting paths), `/roam diff`, `/roam devices`, `/roam doctor`,
`/roam history` (read-only summary of the first ten recoverable versions;
restoring is a terminal command), `/roam help`. Extra words are validated
before anything runs: `push` accepts only `force` and `dry`/`dry-run`,
`pull` only `apply` and `overwrite`, and anything else is rejected with the
valid forms.

**Tools** (for the agent): `roam_status` (read-only; use before and after
the others), `roam_push` (`force`, `dry_run`; refuses before init, and its
dry run lists the bundle's file paths and total size), and `roam_pull` —
the only destructive tool when `apply=true`, which its schema says outright.
`apply=true` is refused unless the `allow_agent_apply` setting is enabled
(default false); the refusal tells the model to ask the user to run
`/roam pull apply` or `hermes roam pull --apply` themselves. All three
return JSON, including on failure.

**Hooks** (all off by default; none ever block a session):

- `on_session_end`: with `auto_push` enabled, pushes in a background daemon
  thread when shared files changed. The `push_debounce_seconds` timestamp
  (default 300 s) is consumed only by a successful push: a push that fails
  or is killed records a pending flag instead, and the next session start
  posts a one-line notice telling the user to push now — a lost auto-push is
  never silently dropped. The hook takes the home lock non-blocking and
  simply skips (with a debug log) when another roam command holds it; the
  user's foreground command always wins. The engine itself refuses to push a
  clean home and refuses outright before `hermes roam init`.
- `on_session_start`: a pending auto-push from a previous session is
  surfaced first. Otherwise it checks the remote head in the background
  using `status()` only. If the remote is verifiably ahead, it stashes a
  one-line notice; with `auto_pull_shared` enabled it first pulls only
  `skills/`, `memories/`, and `SOUL.md` (never `config.yaml`, and never
  overwriting files changed here since the last sync), then re-checks
  `status()` — a restricted pull never advances the seen generation, so the
  notice still fires for the full pull that remains.
- `pre_llm_call`: surfaces the pending notice once, on the session's first
  turn, then clears it.

Honest bounds: the hooks return immediately and run their work in daemon
threads, and a watchdog cannot stop a running thread — the spawn caps (120 s
for the auto-push, 15 s for the start check) only decide when an overrun is
logged at WARNING. What actually bounds the work is the per-operation
deadline budgets: one push or pull gets `operation_timeout` (default 900 s)
in total across every backend call it makes, status/doctor/diff get 60 s,
and a git data transfer is further capped by `git_transfer_timeout`
(default 600 s) within that budget — so a hung or blackholed remote fails
on its own instead of hanging a session.

**Bundled skill**: `roam-setup` — the in-chat walkthrough of key generation,
backend choice (R2, S3, git, local), init, doctor, push, and pull.

## Disclosure

- **Network calls.** Only when a roam command, tool, slash verb, or hook
  actually runs: S3/R2 requests via boto3 to the configured
  `s3_endpoint_url` (or AWS S3) — object reads for bundles/heads and
  ListObjectsV2 listing requests when `history`/`restore` enumerates the
  store — and git network operations against the configured `git_remote`
  via the git CLI (`fetch` also runs when `history`/`restore` refreshes
  the mirror before listing it). There are no other network calls and no
  telemetry of any kind. The local backend's listing never leaves the
  local filesystem.
- **Shell commands.** The git backend runs `git` as a subprocess (argv lists,
  never a shell): `init`, `remote get-url/add/set-url`,
  `config --get core.sshCommand`, `fetch`, `rev-parse`, `cat-file`,
  `hash-object`, `read-tree`, `update-index`, `write-tree`, `commit-tree`,
  `push` (with `--force-with-lease`), `show`, `ls-remote`, and `ls-tree`
  (0.4 history/restore: listing the fetched tree). No other shell commands
  are spawned anywhere in the plugin.
- **Files read.** The Hermes home being synced, excluding everything
  classified local or ignored — in particular this plugin never reads
  `.env`, `auth.json`, or any other credential file; secrets come only
  through Hermes's `agent.secret_scope.get_secret`.
- **Files written.** Under `<home>/roam/`: `roam/state.json`,
  `roam/floor.json` (the rollback floor), `roam/last.roam` (the last synced
  bundle, kept for carry-forward), `roam/rules.yaml` (the per-device scope
  rules), `roam/lock` (the home lock), staging directories, the backup
  directories and their install journals, and (git backend) the
  `git-mirror` clone. Files elsewhere in the home are written only by
  `pull --apply` / `roam_pull` with `apply=true`, after the backup copy.
- **Background threads.** The session hooks above may start daemon threads.
  A watchdog logs an overrun at WARNING (120 s for the auto-push, 15 s for
  the start check) but cannot stop the thread; the work is bounded by the
  per-operation deadline budgets (`operation_timeout`, default 900 s, for a
  push or pull across every backend call; 60 s for status, doctor and
  diff; `git_transfer_timeout`, default 600 s, for a single git data
  transfer), not by the watchdog caps. Nothing runs on a timer outside
  session boundaries. The session-start auto-pull, when
  enabled, pulls only `skills/`, `memories/`, and `SOUL.md`, passes
  `overwrite_conflicts=False`, never touches `config.yaml` or any other
  host-scoped file, and never advances the seen generation — the
  remote-ahead notice still fires for the full pull that remains. The
  session-end auto-push consumes its debounce only on success and skips
  when another roam command holds the home lock; a push that failed or was
  killed is surfaced as a one-line notice at the next session start.
- **Private core import.** The version-compat gate needs the running
  Hermes's true version; the documented surfaces return `0.0.0`
  placeholders in real installs, so the plugin lazily imports
  `hermes_cli.version_info.get_version_info` inside `settings.hermes_version()`
  (falling back to `unknown`) and discloses this in `docs/DISCLOSURES.md`.
- **Self-update.** None. The plugin never fetches or replaces its own
  files; updates come only from the catalog's pinned commit.

## Limitations

- **Secrets are not synced, by design.** Catalog rules require credentials
  to move only through Hermes's secret routes, so `.env` and `auth.json`
  stay per-machine; you recreate them on each device once.
- **Sessions and cron are opt-in** and stay local unless you enable them.
- **`config.yaml` comments are lost on merge.** The merge round-trips
  through PyYAML, which does not preserve comments; content and key order
  are preserved, comments are not.
- **One writer at a time.** Conflicts are detected with compare-and-swap,
  not merged: when two devices diverge, the second one to push must pull
  first. There is no three-way merge of file contents.
- **No Desktop pane.** Roam is a CLI, slash command, and tools; it has no
  Desktop UI.
- **Git backend size guidance.** The shared home is one bundle file; a
  transfer may run up to `git_transfer_timeout` (default 600 s), but git
  hosts cap file sizes (~100 MB per file on GitHub): keep the shared home
  well under ~50 MB on git and use S3/R2 for larger homes, which the 2 GiB
  bundle limit allows.
- **Windows is not supported by the local backend** (it relies on POSIX
  `flock`); the S3 and git backends are unaffected.
- **Python 3.11+** is required, with pinned-bounded dependencies
  (`cryptography>=42,<51`, `pyyaml>=6.0,<7`, `boto3>=1.34,<2`).

## Derived from

Hermes Roam extracts its state pipeline — the encrypted archive format, the
signed-head compare-and-swap, and the journaled restore — from the author's
high-availability backup project (Apache-2.0, same author), simplified to a
single-device push/pull model without leases, witnesses, or fencing.

## License

Apache-2.0. See [LICENSE](LICENSE). Copyright 2026 Victor Cruz.
