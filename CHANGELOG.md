# Changelog

## 0.5.2 — 2026-11-07

- `push --dry-run` with the session archive on now reports the real bundle
  size. It used to add the whole `sessions_max_mb` cap (50 MB by default) on
  top, so a home with two short transcripts showed a 52 MB push. `doctor`'s
  pre-flight size check still uses the cap as its upper bound.

## 0.5.1 — 2026-11-07

Hermes bundles ~58 skills into every home's `skills/` tree (nested category
paths included) and every `profiles/<name>/skills/`, tracking each skill's
origin hash in `skills/.bundled_manifest`. 0.5.0 classified all of them
SHARED, so every push carried hundreds of Hermes-shipped files and a pull
onto a device running a different Hermes release overwrote that device's
bundled copies — Hermes's own `skills_sync` then saw a hash mismatch and
silently froze the receiver's bundled updates. Hub-installed skills already
never traveled; bundled skills now get the same treatment, proven the same
way Hermes proves them to itself.

### Bundled-skill provenance

- Every bundled tree whose current content hash still equals its
  `.bundled_manifest` origin hash — the exact `skills_sync` hash scheme
  (MD5 over the sorted package, runtime caches excluded), replicated from
  Hermes's read-only source without importing it — classifies LOCAL at the
  top level and inside every `profiles/<name>/skills/`, and the category
  `DESCRIPTION.md` files Hermes seeds beside bundled skills are covered
  too. Each device keeps (and keeps updating) the copy its own Hermes
  ships; a pull lists remote bundled paths as refused by local policy and
  never overwrites or deletes them.
- A bundled skill whose hash differs is the user's own work: it stays
  SHARED and syncs like any user skill. The receiving Hermes treats it as
  user-modified there — the correct verdict. A device that holds no
  pristine copy of such a modified skill receives it.
- A missing manifest, a v1 hashless line, or a tree the user deleted
  contributes nothing; a user `shared:` rule in `roam/rules.yaml` still
  wins over the generated rules. `hermes roam rules` labels the generated
  (hub + bundled) rules as device-generated, not user policy.

## 0.5.0 — 2026-11-07

Hermes Roam now works for a multi-profile fleet, not just the default
profile: profiles and bots travel as working Hermes profiles, roam always
operates on the ROOT home, and the session archive covers every profile.
This release also fixes first-pull `config.yaml` merging so a brand-new
device receives the fleet's shared settings, and extends that fix to every
profile config.

### Multi-profile

- `profiles/<name>/profile.yaml` — Hermes's non-secret per-profile identity
  metadata (description, display name, bot roster title) — joins the shared
  allowlist. Without it a pulled profile is not even listable by
  `hermes profile list`; with it the profile arrives with its soul,
  memories, skills, and skins intact, while its `.env`, session database,
  and `sessions/` folder stay machine-local.
- Roam always operates on the ROOT home. Under a profile context
  (`hermes -p NAME`, a profile `HERMES_HOME`, or the session hooks of a
  multi-profile gateway) every surface maps up to `<root>` by path
  structure only — one store holds the whole tree with profile-relative
  paths and one lock covers every context, so concurrent bots can never
  interleave generations or mix profiles into one namespace. The sync
  settings, the device name, and `allow_agent_apply` are read from the
  ROOT home's `config.yaml`, never from the profile's own config.
- Every CLI and slash surface names the covered root and lists every
  profile with its `synced`/`excluded` state (`hermes roam status`,
  `hermes roam rules`, push/pull/init output, `/roam status`).
- The opt-in session archive covers EVERY home of the root: the export
  runs once per home (the root home as profile `default`, then each
  `profiles/<name>/`) into `archive/sessions/<device>/<profile>/`, under
  one shared `sessions_max_mb` cap, globally newest first.
  `hermes roam sessions` and `/roam sessions` group the listing by device
  and profile, and `sessions show` takes the profile segment.
- Per-profile exclusion is a one-liner (`local: [profiles/NAME/]` in that
  device's `roam/rules.yaml`), disclosed as `NAME(excluded)` at every
  surface. Carry-forward semantics are unchanged and pinned by tests: a
  device that excluded a profile can never delete it; a device that can
  see it can, and the deletion then propagates everywhere the profile is
  visible.
- Live two-home proof against real Hermes 0.22.5 (creation, push, pull,
  listability, profile-context behavior, exclusion, one-device-only
  arrival, and deletion) is captured in `docs/multi-profile-proof.log`;
  the merged 0.5.0 two-device run (default + profiles work and personal,
  per-profile first-pull config union, per-profile session export and
  grouped listing) is captured in `docs/proof-0.5.0.log`.

### First-pull config union and --overwrite-conflicts (0.4.2, extended to profile configs)

- Root cause: every pull attempt — plan-only, restricted, or refused —
  saved the downloaded bundle as `roam/last.roam`, and the three-way config
  verdict read its BASE digests from that file. After a single plan review
  on a fresh device, remote == base, so the device's own (nearly empty)
  shared projection looked like a "local-only change": the whole
  `config.yaml` was kept local, and the `--overwrite-conflicts` path was
  unreachable because the remote == base short-circuit ran first. Regular
  files were immune — their base (`base_files`) is only pinned on an
  applied pull.
- The config base is now the ACCEPTED projection, tracked per config in
  `state.json` (`config_base_digests`): recorded when a push publishes this
  device's own stripped config, when a pull apply merges or takes the
  remote projection, and when a pull proves the local copy already carries
  the remote projection — never when a bundle is merely downloaded or
  reviewed, and never when a first-pull union keeps local values. A bundle
  that was only looked at can no longer poison any verdict.
- On a device's FIRST pull for a config (no accepted projection), the merge
  is a UNION with local precedence: every remote shared key the local file
  LACKS is added (the fleet's model/provider settings reach a fresh device),
  every local value wins where both sides set the same key and is REPORTED by
  dotted key name with both resolutions (push to publish, or pull with
  `--overwrite-conflicts` to take the remote values), and host-scoped,
  secret-looking and roam-plugin keys stay local exactly as before. In
  0.5.0 the same first-pull union applies to EVERY synced config in the
  tree — the root `config.yaml` and each `profiles/NAME/config.yaml`.
- `pull --apply --overwrite-conflicts` now applies to `config.yaml` and to
  each synced `profiles/NAME/config.yaml`: the remote shared projection
  wins for shared keys while host-scoped/secret keys and the local roam
  plugin settings stay local, and the pre-merge config is backed up like
  every replaced file. Without the flag the union keeps local precedence,
  so the flag is the only way to take remote values wholesale.
- Byte-identical shared files were and remain non-conflicts (`unchanged`,
  never rewritten). A brand-new device's Hermes-seeded default `SOUL.md`
  cannot be detected from the plugin without private Hermes imports, so it
  stays a listed conflict; instead every first pull on a device that never
  synced tells the user plainly that a brand-new device should run
  `hermes roam pull --apply --overwrite-conflicts` to take the remote home
  (backups of everything replaced are kept). The README quick start says the
  same.
- Regression tests reproduce the exact repro end to end (real pipeline, real
  scope code, in-memory backend): the plan-review poisoning step, the
  first-pull union that brings the shared model keys while the device's own
  api_key and roam settings survive, the by-name kept-key report, the
  brand-new-device notice, and the `--overwrite-conflicts` takeover with a
  backup — now for the root config AND for `profiles/NAME/config.yaml`.
  The full suite and ruff are green; `scripts/demo-two-homes.sh`
  exits 0 with real Hermes homes on both backends.

## 0.4.1 — 2026-11-07

Session history becomes an opt-in, default-OFF, READ-ONLY archive, and
"what syncs" is stated up front everywhere.

- The raw `sessions/` folders (top level and per profile) are ALWAYS
  LOCAL: in Hermes 0.22.5 they are `agent.logs_dir` — redacted
  `request_dump_*.json` debug files plus rare fallback shards — never real
  transcript history, which lives in the local `state.db`. Request dumps
  never travel; regression tests pin the classification.
- `sync_sessions` (same key, default false, new meaning) publishes a
  READ-ONLY archive of this device's session transcripts: a push exports
  the home's sessions with Hermes's own documented CLI (`hermes sessions
  export --format md --redact`, argv subprocess, `HERMES_HOME` set, stdin
  closed, hard timeout inside the operation deadline, no prompts) into the
  bundle-only per-device namespace `archive/sessions/<device>/`, capped by
  the new `sessions_max_mb` setting (default 50 MB, NEWEST transcripts
  first). A device can only ever replace its OWN namespace; other
  devices' namespaces are carried forward byte-identical. A failed export
  degrades to a clear notice — everything else still syncs and the
  previously published archive is kept; turning the opt-in off unpublishes
  only this device's namespace.
- Reading needs no opt-in: pulls install published archives READ-ONLY
  (mode 0444) under `<home>/roam/session-archive/<device>/`, which is
  LOCAL, so a received archive can never be re-pushed; archived sessions
  can never be resumed; `hermes roam restore` refuses archive paths and
  history never lists them. `hermes roam sessions` lists devices and
  sessions (title/date/size, sanitized) and `hermes roam sessions show
  DEVICE SESSION_ID` prints one transcript on the terminal through the
  roam sanitizers; `/roam sessions` is the read-only listing form. There
  is deliberately NO agent tool for transcripts — nothing bulk-dumps
  session content into the model.
- `sync_cron` now syncs ONLY job definitions (`cron/jobs.json`, top level
  and per profile); run output under `cron/output/`, locks, and telemetry
  stamps are machine-local.
- What syncs is now obvious: the README opens with the three tiers
  (always / optional with the exact `hermes config set` commands / never),
  `hermes roam status` and `--json` show the optional items' on/off state,
  `hermes roam rules` prints the tiers, and every surface discloses the
  opt-in state. Honest limit, stated plainly: transcripts travel
  encrypted, but Hermes's `--redact` pass is best-effort — an archived
  transcript may still contain anything you typed.
- Tier-audit fix: the `roam rules` tier summary claimed "plugin code" and
  "the stripped rules.yaml" always sync — wrong on both counts (only the
  pinned plugin name-and-sha list travels, and `roam/rules.yaml` never
  travels at all); the README never-tier listed `hooks/` as unsyncable
  while the optional tier (and the code) allow the per-device `shared:`
  opt-in. Both now state the real contract, pinned by tests, and the
  never-tier names the only allowlist-extension lever and the hard
  `<home>/roam/` pin.
- Live two-home proof (docs/session-archive-proof.log): real Hermes 0.22.5,
  local backend, no network — sessions created with `hermes sessions
  import`, the opted-in device publishes, the peer pulls the archive
  read-only (0444) and lists/prints it, a device without the opt-in never
  publishes its own sessions (checked against the signed head manifest),
  a fresh pair with the opt-in off travels nothing session-related, and
  `roam status` shows every opt-in. The same flow is stage 4 of
  scripts/demo-two-homes.sh.

## 0.4.0 — 2026-11-06

Round-4: every version ever pushed is recoverable, and the convergence
failure found by the randomized campaign is fixed at the root.

- The store is the archive: every new signed head records its
  `parent_bundle_key`, so the bundle chain is verifiable end to end, and
  every backend (local, S3/R2, git) implements `list_blobs` — the only
  listing surface, bounded (8 directory levels local, 100 pages S3,
  deadline-checked) and returning exactly the keys that later pass
  verification.
- `hermes roam history [path] [--json]` enumerates what the remote store
  still holds for a path (generation, device, date, sha256, size,
  present/deleted), newest first, walking the chain and re-verifying every
  bundle header+tag with the archive key; unsigned or undecryptable
  bundles are never trusted. `/roam history` is the read-only summary;
  restore stays a terminal command (no agent tool can apply it).
- `hermes roam restore <path> --generation N [--apply]` recovers one
  version: plan first, then `--apply` writes it into the home through the
  normal journaled install (backup first), refuses LOCAL/IGNORED paths,
  merges restored configs with the local host keys, and leaves the
  restored version marked as a local change so the next push publishes
  it. The retention model is documented: `backup_keep` is a local crash
  window, the remote store keeps every generation and is never pruned by
  roam; pruning it is a manual operator decision.
- The verifier's no-data-loss invariant is upgraded: every pushed version
  must be in a home, in a backup, OR recoverable through the plugin's own
  history/restore surface — which the verifier actually exercises,
  restoring each missing version into a scratch home and hash-checking
  it. The verifier and the campaign harness accept
  `ROAM_CAMPAIGN_BASE`/`ROAM_TEST_KEY`/`ROAM_CAMPAIGN_LOG`/`ROAM_CHECKOUT`
  overrides so a rerun never touches archived evidence.
- Root-cause fix for the campaign's git convergence failure:
  `Engine.status()` no longer counts base entries for paths the device
  cannot see under its own rules and opt-ins as local dirt — they are the
  carry-forward fleet state push already handles (R1), and counting them
  pinned `local_dirty=True` forever while push correctly answered
  "nothing to push". Two deterministic regression tests cover both
  triggers (peer rules narrowing, own opt-out); real dirt on visible
  paths is still flagged.
- The campaign harness compares P2 convergence only on paths SHARED
  under every device's rules, and the rules.yaml scenario asserts that
  rules.yaml never travels.
- The demo proves the recovery surface live: `hermes roam history` lists
  every pushed generation of a file and `restore --apply` brings an older
  generation back verbatim (backup first), then rolls forward.
- The S3/R2 backend is proven against real Cloudflare R2, not only against
  the moto emulator: the full backend contract (verdict PASS) and a
  complete two-home sync — shared files travel, host keys/.env/api_key
  stay local, stale push exits 2, pull --apply converges, history/restore
  recover every generation, doctor green on both homes — ran live against
  a real R2 bucket on 2026-11-06; transcript in `docs/R2_PROOF.md`. Real
  R2 matched every contract-visible behavior of the moto suite.
- Release-audit fixes: the README quick start's pre-catalog install command
  now uses the required `file://` local-source form (a bare path is parsed
  as a GitHub owner/repo shorthand and fails), and the test fixture that
  snapshots a real home's top-level names no longer carries private
  host/machine tokens (classification coverage is unchanged — the pattern
  shapes stay).
- `hermes roam push --dry-run` on an unchanged home now prints the honest
  skip outcome ("nothing to push") instead of a would-push summary of an
  empty bundle ("N files, 0 bytes"); a dirty home still lists every path
  it would upload with the real size estimate. Regression tests cover the
  CLI ordering and the engine's skip answer under `dry_run`, and the
  behavior was verified live against a real Hermes home (clean home ->
  "nothing to push"; dirty home -> paths and sizes listed).
- The archived proof documents no longer carry author-machine absolute
  paths (neutralized to `<work>`/`<tmp>`/`<host-data-root>` placeholders)
  or the sha256 digests of the scratch homes' .env files (replaced by the
  statement that both homes were seeded with the same archive key); the
  R2 endpoint account id is kept only in `docs/R2_PROOF.md` and its raw
  transcript. Documented results — verdicts, exit codes, timings — are
  unchanged, and the transcripts now state these redactions explicitly.

## 0.3.0 — 2026-11-06

Round-3 review fixes: no surface ever relays remote-controlled text, the
hooks are honest about lost pushes, the plans are complete, and every
deadline is a real budget.

- Untrusted remote text never reaches the user or the model verbatim: a
  backend error becomes a fixed summary on every surface (tools JSON,
  `/roam`, CLI) with the detail only in the Hermes log, head roster names
  are displayed only when they pass the fleet charset, hostile manifest
  paths become a fixed placeholder in every plan and result JSON, and no
  tool JSON ever instructs the model to repeat an error.
- The auto-push hook is honest about failures: the debounce timestamp is
  consumed only by a successful push, a failed or killed push records a
  pending flag surfaced as a one-line notice at the next session start,
  and the hook takes the home lock non-blocking and skips (debug log)
  when a foreground command holds it — the user's command always wins.
- The pull plan is complete: it lists the config files that would be
  merged (host-scoped keys stay local), the config conflicts kept local,
  the unchanged files that will not be rewritten, and every added,
  changed or removed path; the push dry run reports the engine's real
  bundle size estimate on every surface.
- Deadline budgets are per user operation: `operation_timeout`
  (default 900 s) for one push or pull across every backend call, a fixed
  60 s budget for status/doctor/diff, and `git_transfer_timeout`
  (default 600 s) for a single git data transfer — all declared in the
  manifest's config schema and clamped by the loader (a sub-1 s value
  reverts to the default, the transfer cap never exceeds the operation
  budget).
- Per-device scope is enforced end to end: a publisher that cannot see a
  path carries it forward byte-identical instead of deleting it fleet-wide,
  a deletion of a visible path propagates with the removed content
  preserved in the peer's `roam/backups/`, `roam/rules.yaml` is LOCAL and
  never travels, and an init gate refuses to sync a home that is not a
  Hermes home.
- Config sync is three-way and local-first: `config.yaml` and profile
  configs are planned against base digests from the last sync — an edit
  that exists only locally is kept local and listed (publishing it is the
  next push), a shared-key change that exists only remotely merges with
  the host-scoped keys kept from the local config, a same-key change on
  both sides is a conflict that is kept local, and secret values are never
  bundled or relayed.
- Crash recovery is non-destructive: an interrupted install restores
  exactly from its journal, a recovery with a missing backup never deletes
  the file (the journal is marked unrecoverable and surfaced), and the
  floors that protect `roam/` itself cannot be lowered by a remote.
- Backups retention is bounded: only the newest `backup_keep` (default 5)
  backup directories are kept and their journals are pruned after each
  successful install; a recovery with a missing backup never deletes the
  file — the journal is marked unrecoverable and surfaced.
- The manifest now declares every real dependency (pyyaml was missing
  from `python_dependencies`), the disclosures list every git command the
  backend runs (including `git config --get core.sshCommand`) and every
  file written under `<home>/roam/` (`last.roam`, `floor.json`,
  `rules.yaml`, `lock`), the private core import is referenced by function
  name instead of a line number, the quick start names `{home}/.env`
  explicitly and shows the local-path install while catalog admission is
  pending, and the bundled skill never has the agent run the destructive
  commands or read credential files.

## 0.2.0 — 2026-11-06

Adversarial-review fixes: every surface now tells the truth about what it
did, what it refused, and what it left alone.

- An unreachable backend is reported, never guessed: `hermes roam status`
  prints `roam: remote unreachable: <error>` and exits 1 instead of crashing
  on a missing generation; the `/roam status` slash output and the
  `roam_status` tool surface the same error, force `remote_ahead` false,
  and tell the model not to pull and not to claim the remote is ahead.
- The journaled install is the user's decision, not the model's: the
  `roam_pull` tool refuses `apply=true` unless the new
  `allow_agent_apply` setting is enabled (default false, fails closed
  without a configuration context) and points the model at
  `/roam pull apply` / `hermes roam pull --apply` for the user to run.
- Pull conflicts are surfaced, never silently dropped: `pull --apply`
  lists every path it kept local — `kept local (changed here since last
  sync): <path>` and `kept local by your scope rules: <path>` — and a new
  `--overwrite-conflicts` flag (slash: `/roam pull apply overwrite`)
  takes the remote version of conflicting paths, backup first. Every
  surface prints `roam: recovered: <message>` when the engine rolled a
  crashed previous operation back.
- `push` refuses with exit 1 before `hermes roam init` has written local
  state, and `push --dry-run` lists the bundle's file paths (40-line cap,
  then `... N more`) with the total size instead of bare counts; the tool
  returns the complete list in its JSON.
- `/roam` validates its arguments before anything runs: `push` accepts
  only `force` and `dry`/`dry-run`, `pull` only `apply` and `overwrite`;
  anything else is rejected with the valid forms.
- The session-start hook is honest about its bounds: it checks the remote
  with `status()` only, ignores an unreachable backend instead of
  claiming it ahead, its watchdog logs an overrun at WARNING without
  claiming it abandoned the task, and its restricted auto-pull passes
  `overwrite_conflicts=False` and never touches `config.yaml` or advances
  the seen generation. The README states the real numbers: watchdog caps
  of 120 s (auto-push) and 15 s (start check) only decide when an overrun
  is logged; the work itself is bounded by the backend deadlines (git 60 s
  total per operation chain, each network-facing call 20 s, ssh connect
  10 s; S3 5 s connect + 30 s read per attempt, at most two attempts per
  operation).
- Packaging: boto3 moved from the optional `s3` extra into the main
  dependencies (plugin.yaml and pyproject agree), so a real
  `hermes plugins install --yes-deps` carries it into the dependency
  environment and `hermes roam doctor` no longer reports it missing on an
  S3/R2 home.
- Scope classification is precompiled (anchored first-segment dispatch,
  memoized directory pruning): on the realistic 1.6 GB dummy home, real
  `hermes roam status` / `push --dry-run` / `doctor` run in ~2 s each
  instead of ~25 s; classification cost is linear in paths, semantics
  unchanged.
- Secret-named config keys (`api_key`, `*token*`, `*secret*`, `password`,
  `bearer`, …) and credential-like values (`sk-…`, `ghp_…`, `AKIA…`, …)
  are host-scoped at any depth, lists included: stripped on push, kept local
  on pull, never logged. Home-directory path values (including whole lists
  holding any host value) are treated the same way, and
  `profiles/*/config.yaml` now goes through exactly the same strip/merge
  pipeline as the top-level `config.yaml`.
- Weak archive keys (32 identical bytes) are rejected by every entry point
  with a named error; `hermes roam doctor` flags them.
- Trust guards: a pull refuses a validly signed remote head below this
  device's sync floor (rollback guard); crash recovery runs at the start of
  every command and reports what it rolled back (`recovered:`); state
  writes are atomic (unique tmp + rename) under a per-home process lock.
- Three-way conflict detection: every path a pull would write or delete is
  compared against the last sync point; a file changed locally since then
  (or never synced and diverging) is kept local, listed, and carried by the
  next push from this device — the documented resolution is push after
  review or `pull --apply --overwrite-conflicts` (backup kept).
- Local rules win on pull: remote paths this device classifies LOCAL or
  IGNORED (or an opt-in class not enabled here) are never written or
  deleted and are listed as kept-local. A publisher that never synced an
  opt-in class cannot wipe it fleet-wide (opt-in carry-forward).
- Push fails closed on bundles over `max_bundle_bytes` before any upload,
  and reports its file list; dry-runs list files without uploading.
- Backends fail fast and never leak credentials: git ssh is pinned to
  BatchMode with a 10 s connect timeout, every git operation chain runs
  under a 60 s total deadline with each network call capped at 20 s, a
  missing git binary and a scheme-less S3 endpoint are named errors, all
  URL authorities in messages are redacted, and the S3 backend makes
  exactly one conditional PUT per push (5 s connect + 30 s read, at most
  two attempts).

## 0.1.1 — 2026-11-05

- Scope classification flipped from a denylist with a shared catch-all to an
  ALLOWLIST: only `config.yaml`, `SOUL.md`, `hermes-pack.yaml`, `memories/`,
  `skills/`, `skins/` and `roam/rules.yaml` (plus the same inside
  `profiles/*/`) ever travel; every unmatched path — credentials (`hf_token`,
  oauth files), `vault/`, `pairing/`, `node/`, `tools/`, `lsp/`, `runtime/`,
  `*.tmp`, shared state databases — stays local. `hooks/` is local by default
  (Hermes auto-loads it at startup; opt in via `rules.yaml`). `*.tmp` is
  excluded at any depth inside shared dirs, and the Hermes-managed dot-state
  under `skills/` (`skills/.*`) is never even listed.
- Skills installed from the skills hub never travel: Hermes records them in
  `skills/.hub/lock.json`, and roam keeps each recorded install tree local;
  the other device reinstalls from the hub. User-written skills still sync.
- `hermes roam devices` now shows a device's real last-pull generation: a
  push records it from local state instead of copying the stale previous
  roster entry (pulls never write the roster).

## 0.1.0 — 2026-11-05

First public release of hermes-roam.

- Encrypted, host-scoped sync of the Hermes Agent home between the user's own
  machines: one AES-256-GCM bundle per generation on S3, Cloudflare R2, a
  private git repository, or a local directory.
- Path-classified scope model with user overrides: shared files sync,
  secrets and host-specific state stay local, sessions and cron are opt-in,
  caches are never even listed (`roam/rules.yaml` overrides).
- Host-scoped `config.yaml` handling: host keys and home-directory path
  values are stripped on push and kept local on pull.
- Signed, compare-and-swap remote head with a device roster; conflicting
  pushes are refused (exit code 2) instead of silently clobbering.
- Journaled pull installs with a backup directory and rollback on failure;
  pull previews and dry runs for push and pull.
- CLI (`hermes roam init|status|devices|diff|push|pull|doctor|rules`),
  `/roam` slash command, `roam_status` / `roam_push` / `roam_pull` tools,
  optional debounced auto-push and a skills/memories/SOUL-only auto-pull at
  session start (both off by default, time-capped), and a bundled
  `roam-setup` skill.
