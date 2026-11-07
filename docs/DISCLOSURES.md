# DISCLOSURES — hermes-roam integration lane

Honest limits and residual warnings of the plugin as proven in
`docs/REAL_HERMES_PROOF.md`. Everything here is either inherent to the design or
explicitly accepted with the reason given; nothing is hidden.

1. **Doctor WARN on boto3 — resolved in 0.2.0.** Before 0.2.0, boto3 hid in
   the optional `s3` extra of `pyproject.toml`, so a real
   `hermes plugins install --yes-deps` resolved the plugin's declared
   dependencies without it and `hermes roam doctor` reported
   `boto3 is not installed` on every S3/R2 home (a real pyproject owns
   packaging — the manifest's `python_dependencies` is ignored when one
   exists, so the manifest list alone could not fix it). 0.2.0 moves
   `boto3>=1.34,<2` into the main dependencies of both pyproject and the
   manifest. Verified live: a real install into a scratch home under
   `<work>/roam-homes/f4-*` with `--yes-deps` now carries boto3 and
   botocore into the plugin dependency environment, and `hermes roam
   doctor` on an S3 home no longer reports boto3 missing — its remaining
   FAILs are the honest ones (backend reachability and head signature with
   test credentials against no real endpoint).

2. **One private core import, kept deliberately: `hermes_cli.version_info.get_version_info`.**
   `hermes plugins validate` cautions: the lazy import inside
   `settings.hermes_version()` (catalog rule 9: public surfaces only).
   Reason: the plugin's version-compat gate compares remote heads against the running
   Hermes version, and this is the ONLY source of the true version — the module-level
   `__version__` attribute and the `hermes-agent` distribution metadata are both `0.0.0`
   placeholders in real installs (verified). No documented alternative exists
   (PluginContext exposes no version). Removing it would record `"unknown"` in every
   remote head and disable the gate. Everything else about the import is defensive:
   lazy, wrapped in try/except, and it falls back to `"unknown"`.

3. **Security-scan cautions.** None remain: the only `invisible_unicode` hits came from a
   reference copy of the Hermes plugin guide that lived under a `campaign/` directory during
   development; that directory is not part of the repository.

4. **Backend migration requires resetting local roam state.**
   Moving a device to a NEW remote (different backend or different store) requires
   deleting `<home>/roam/state.json` (and `<home>/roam/git-mirror/` for git). This is
   by design: the seen-generation belongs to the old remote, and the engine correctly
   refuses to push (`the remote head is at generation 0 but this device last saw 6;
   pull first`) rather than guess. The pull-side journal (`roam/backups/`) is not
   affected.

5. **Not proven live in the demo (covered by tests only):**
   - The `on_session_start` restricted auto-pull hook (`AUTO_PULL_PREFIXES`:
     skills/memories/SOUL.md only, never config.yaml): starting a real agent session
     needs provider credentials; the behavior is proven by
     `test_auto_pull_shared_pulls_shared_paths_only` and
     `test_auto_pull_shared_no_notice_when_nothing_remains`.
   - The S3/R2 backend against real AWS/Cloudflare: **proven against real
     Cloudflare R2 on 2026-11-06** (`docs/R2_PROOF.md`): the full backend
     contract ran live against a real R2 bucket (absent head → None, CAS
     token round trip, real HTTP 412 → `HeadConflict`/`BlobExists`,
     `max_bytes` cap, `list_blobs`, ping — verdict PASS), and a complete
     two-scratch-home sync through real R2 proved shared files travel,
     host keys/.env/api_key stay local, a stale push exits 2
     (RemoteAhead), pull --apply + push converge, `history`/`restore`
     recover every generation, and doctor is green (9/9) on both homes.
     Real R2 matched the moto-based tests (`tests/test_backend_s3.py`) on
     every contract-visible behavior; the only differences were favorable
     (R2 returns the ETag in the put response, so the fallback head
     lookup never fires, and the 412 refusals are genuinely
     server-side). AWS S3 itself remains covered by the moto suite.
   - The refusal surfaces (unreachable backend, apply gate, push-without-init,
     dry-run file list, slash argument validation): each is asserted by the
     review-driven tests in `tests/test_review_r4.py` and
     `tests/test_plugin_surface.py`, not by a live session.

6. **Environment artifact, not a plugin defect.** `hermes plugins validate` run with
   cwd=<host-home> on THIS VPS fails in the capability probe because a stray
   `<host-home>/struct.py` (unrelated campaign tooling) shadows the stdlib `struct` module.
   Run gates from any other cwd. Recorded in REAL_HERMES_PROOF §0.

7. **Session and cron sync are opt-in; the 0.4.1 archive is proven live.**
   `sync_sessions` and `sync_cron` default to False. Since 0.4.1 the raw
   `sessions/` folders are ALWAYS local (they hold redacted request dumps,
   not transcripts), and `sync_sessions` instead publishes a READ-ONLY
   archive exported by Hermes's own CLI; `sync_cron` moves ONLY
   `cron/jobs.json`, never run output. Both opt-ins' on/off state is shown
   by `hermes roam status`. The live proof (demo stage 4) enables the
   archive on one home, publishes a real imported session, and reads it
   back read-only on the second home.

8. **Probe pollution during debugging.** While isolating the git CAS bug, a manual
   probe pushed a synthetic head to the demo git remote; the branch was reset to the
   last good commit before the recorded git-backend round trip. The demo remotes are
   throwaway directories under `<work>/roam-homes` and `<work>/roam-demo`,
   not user data.

9. **Engine result fields are merged.** The 0.2.0 surface is implemented
   against the pre-agreed engine API: `conflicts` / `skipped_local` /
   `recovered` on pull and status, `files` / `recovered` on push,
   `overwrite_conflicts` on pull, engine-level push init refusal, and
   `remote_ahead=False` on backend error. The engine lane is merged into
   main: the former `xfail(strict=False)` placeholder on
   `test_status_unreachable_backend_reports_error_not_ahead` was removed and
   the test passes against the merged engine (391-test suite, 0 failures,
   0 xfails), and `settings.supported_kwargs()` now flows the flags straight
   through.

10. **0.4 listing calls, disclosed.** `hermes roam history`/`restore`
   enumerate the store through the new backend `list_blobs` surface: the
   S3/R2 backend issues ListObjectsV2 requests (bounded to 100 pages,
   each under the client timeouts), the git backend runs `git fetch` to
   refresh the local mirror and then `git ls-tree` on the fetched tip, and
   the local backend only walks the store directory (never a network
   call). These are the only new network calls in 0.4; both README
   disclosures list them. Every key a backend lists is re-verified against
   the archive key before it is trusted, so a listing can never inject
   content into a home.

11. **Round-4 doctor WARN — host environment, not a plugin defect.**
   `hermes plugins doctor <work>/hermes-roam` (exit 0, manifest 0.4.0,
   runtime discovery/import/registration OK) now prints
   `WARN: declared python_dependencies not installed` for pyyaml/boto3.
   The check inspects the HOST data root (`<host-data-root>`), where the
   plugin is deliberately NOT installed — the campaign never modifies
   `<host-data-root>`. The WARN appeared after the doctor's own source-update
   pass ("completing source-update dependencies", "✓ Update complete!
   (v0.22.5)") rebuilt the host dependency environment; the round-3
   archived doctor log shows no WARN under otherwise identical commands. The packaging itself is proven:
   `hermes plugins validate --install-deps` resolves and installs all 3
   requirements, and every real install into a campaign or demo home
   carries pyyaml and boto3 (the 0.2.0 fix holds — the S3 import works in
   every home that configures it).

12. **0.4.1 session-export subprocess, disclosed.** With `sync_sessions`
   enabled, a push runs Hermes's own documented export as an argv
   subprocess — `hermes sessions export <tmpdir> --format md --redact
   --after 2000-01-01 --force` — with `HERMES_HOME` set to the active home,
   stdin closed, and a hard timeout inside the armed operation deadline; it
   never uses a shell and the path asks no prompts. The transcripts are
   capped (`sessions_max_mb`, default 50 MB, newest first) and published
   under the pushing device's own namespace only. Hermes's `--redact` pass
   is best-effort pattern matching: an archived transcript may still
   contain anything the user typed, which the README states plainly. If
   the export fails, the push degrades to a clear notice, everything else
   still syncs, and the previously published archive is kept.
