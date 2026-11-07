# Changelog

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
