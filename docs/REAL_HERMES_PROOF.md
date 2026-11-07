# REAL_HERMES_PROOF — hermes-roam 0.4.0 (main)

Date: 2026-11-06/07 (UTC). Host: the campaign VPS, Hermes 0.22.5 (`hermes` on
PATH). Plugin: hermes-roam **0.4.0**, checkout `<work>/hermes-roam`
(main, round-4 lanes merged — commits at the bottom). Every command below was
run for real; outputs are verbatim except that archive key values are redacted
(`[REDACTED]`) and author-machine absolute paths are neutralized to
placeholders (`<work>`, `<tmp>`, `<host-data-root>`); every decision line,
exit code, and timing is unchanged. Raw transcripts are archived next to this file:
`docs/gates-r4-final.log`, `docs/timing-r4-final.log`,
`docs/campaign-r4-final.log`, `docs/verifier-r4-final.log`,
`docs/demo-run-round4.log`.

Round-4 adds two properties to the round-3 proof: (1) every version ever
pushed is recoverable — `hermes roam history` enumerates the store and
`hermes roam restore --apply` brings any generation back, proven live and by a
post-hoc verifier over a full randomized campaign; (2) the campaign's git
convergence failure (round-4 lane h2) is fixed at the root and the rerun
converges on BOTH backends.

## 0. Environment note

`hermes plugins validate` must be run from a working directory that does not
contain a `struct.py` (this VPS has a stray `<host-home>/struct.py` from unrelated
campaign tooling; with cwd=<host-home> it shadows the stdlib `struct` module inside
the validator's capability-probe subprocess and the probe fails). All gates
below were run from `<work>`. This is a host artifact, not a plugin
defect. The campaign also never modifies `<host-data-root>`; every Hermes home
below lives under `<work>/roam-homes` or `<tmp>`.

## 1. Plugin gates (real Hermes CLI)

### 1.1 `hermes plugins validate --install-deps`

Command (cwd `<work>`):

```
hermes plugins validate <work>/hermes-roam --install-deps
```

Verdict (verbatim, exit 0):

```
  → Resolving Python dependencies…
  ✓ Resolving Python dependencies
  → Installing Python dependencies…
  ✓ Installing Python dependencies

✓ manifest — plugin.yaml parses
✓ manifest fields — name, version, description present
✓ requires_hermes — spec '>=0.22.0' parses
✓ config schema — shape valid
✓ requires_env — all entries UPPER_SNAKE
✓ oauth — not declared
✓ loadable — entry: __init__.py
✓ python dependencies — 3 requirements from pyproject
✓ capability probe — register() ran in isolation
✓ declared tools — matches registrations
✓ declared hooks — matches registrations
✓ declared middleware — matches registrations
✓ built-in tool collisions — no collisions
✓ security scan — safe
✓ no core override — no runtime rebinds of Hermes core
⚠ private core imports (catalog rule 9: public surfaces only; these can break on
any release): settings.py:252 hermes_cli.version_info.get_version_info. Use
ctx.* / documented plugin APIs, or open an issue for the seam you need.
◆ Isolation — runs in the plugin host (plugins.isolation: host)
  · ctx.register_cli_command() is skipped in the host (`hermes <command>`
subcommands are wired into the local CLI parser)

Validation passed.
```

The one caution is the deliberate private version import, disclosed in
`docs/DISCLOSURES.md` §2. The security scan is clean.

### 1.2 `hermes plugins doctor`

Command (cwd `<work>`): `hermes plugins doctor <work>/hermes-roam`.
Exit 0. Verdict (verbatim):

```
Plugin Doctor: <work>/hermes-roam
  manifest: hermes-roam 0.4.0 (standalone)
  WARN: declared python_dependencies not installed: pyyaml>=6.0,<7,
boto3>=1.34,<2 — install and enable the plugin with hermes plugins. For an
enabled plugin, run hermes pm repair, then restart Hermes.
  OK: runtime discovery, manifest parsing, import, and registration passed
  registrations: 3 tool(s), 3 hook(s)
```

The WARN is a host-environment artifact, not a packaging defect — the check
inspects the HOST data root (`<host-data-root>`), where the plugin is
deliberately not installed (this campaign never modifies `<host-data-root>`);
it appeared after the doctor's own Hermes source-update pass rebuilt the host
dependency environment. The packaging is proven where it matters:
`validate --install-deps` resolves and installs all 3 requirements above, and
every real install into a campaign or demo home carries pyyaml and boto3
(the 0.2.0 fix holds). Full analysis in `docs/DISCLOSURES.md` §11.

## 2. Live timing on the realistic dummy home (review-r4-real)

Home: `<work>/roam-homes/review-r4-real` — a real 2.4 GB Hermes home
(the adversarial review's perf fixture), with the merged 0.4.0 plugin
reinstalled from the checkout (`hermes plugins list` → `hermes-roam enabled
0.4.0`), its sync state rebuilt from current content via `roam init
--create-remote`. Every number is a real wall-clock measurement of the real
CLI (cwd `<work>`), run once, sequentially, on an otherwise idle host.
Script: `<work>/roam-homes/r4-timing.sh`; transcript:
`docs/timing-r4-final.log`.

Local backend (store on the same disk):

| command | wall time | exit |
|---|---|---|
| `hermes roam status` | **1.8 s** | 0 |
| `hermes roam push --dry-run` | **1.8 s** | 0 |
| `hermes roam doctor` | **1.8 s** | 0 |

Blackholed git remote (`10.255.255.1` — non-routable; nothing ever answers):

| git_remote | command | wall time | exit |
|---|---|---|---|
| `ssh://git@10.255.255.1/x.git` | `hermes roam status` | **12.0 s** | 1 |
| `ssh://git@10.255.255.1/x.git` | `hermes roam push` | **11.8 s** | 1 |
| `https://10.255.255.1/x.git` | `hermes roam status` | **16.8 s** | 1 |
| `https://10.255.255.1/x.git` | `hermes roam push` | **16.7 s** | 1 |

Every number is far under the 30 s budget: the local commands stay under 2 s
(precompiled scope classifier + directory pruning), ssh dies on the pinned
10 s connect timeout (`BatchMode=yes`), https on the 20 s network-call cap —
both fail closed with a named, redacted error (exit 1, never a hang, never a
claim that the remote is ahead). The 0.4 history walk adds no measurable
cost to these commands. The home's configuration was restored afterwards.

## 3. Full two-home demo — `scripts/demo-two-homes.sh` (exit 0)

Run: `cd <work> && bash scripts/demo-two-homes.sh <tmp>/roam-demo-r4`,
**DEMO_EXIT=0**, transcript archived verbatim in `docs/demo-run-round4.log`.
The script drives the real Hermes CLI through both backends with in-script
assertions at every step (`set -euo pipefail`): shared content lands, host
keys and `.env` stay local, `model.api_key` never crosses in either
direction, a real conflict is kept local and listed, `--overwrite-conflicts`
replaces it with a backup, a deletion propagates with the removed content
preserved in the peer's `roam/backups/`, a shared config key propagates while
both devices keep their own secrets, and `roam/rules.yaml` never travels.

Stage 3 (new in 0.4.0) proves the recovery surface live, verbatim:

```
=== stage 3 — history: the remote store keeps every pushed generation ===
roam: 2 recoverable version(s) in the remote store (newest first)
gen 2 beta 2026-11-07 7772721a2130 64B present SOUL.md
gen 1 alpha 2026-11-07 8abcffcf4c34 29B present SOUL.md
roam: recover one with `hermes roam restore <path> --generation N --apply`
roam: the remote store keeps every pushed generation; it is never pruned by roam
verified: history lists every pushed generation of SOUL.md, newest first

=== stage 3 — restore the older generation: plan first, then --apply ===
roam: plan only — generation 1 still holds SOUL.md (sha256 8abcffcf4c34, 29B)
roam: review it, then run the same command with --apply
roam: restored SOUL.md from generation 1 into the home
backup       <tmp>/roam-demo-r4/alpha/roam/backups/20261107-030434
roam: the restored version is a local change; the next `hermes roam push` publishes it
verified: generation 1 restored verbatim; the replaced version is in roam/backups

=== stage 3 — roll forward to the newest generation ===
roam: restored SOUL.md from generation 2 into the home
backup       <tmp>/roam-demo-r4/alpha/roam/backups/20261107-030636
roam: the restored version is a local change; the next `hermes roam push` publishes it
local dirty  0 shared files changed — nothing to push
fleet        2 devices
verified: the newest generation is back and status is settled
```

The in-script assertions additionally proved: the plan-only restore changed
nothing; the `--apply` restore wrote the exact generation-1 bytes back
(hash-verified by content grep); the replaced version landed in
`roam/backups/`; and after rolling forward to the newest generation,
`roam status` settles clean (`local dirty 0`) — restore is a journaled,
reversible operation, not a state corruption.

## 4. Randomized property campaign + upgraded verifier (both backends)

The round-3 randomized harness (`tests/review2_s3_harness.py`, seed
**20261106**) was rerun end to end on FRESH homes with the merged 0.4.0
plugin, under `ROAM_CAMPAIGN_BASE=<work>/roam-homes/r4-final`, first on
the local backend (10 random rounds + 4 scenarios) then on the git backend
(same again), 92 minutes wall clock. Full console log:
`docs/campaign-r4-final.log`.

Verdict (verbatim, from the log):

```
P1 violations: 17; P2 convergence failures: 0
```

- **P2 (convergence): 0 failures on BOTH backends.** Every random round
  ended with the homes converged and clean, including the git backend whose
  alpha device previously pinned `local_dirty=True` forever — the round-4 h2
  root-cause fix (status no longer counts carry-forward fleet entries a
  device cannot see as local dirt) holds under the full randomized
  adversary. The rules.yaml scenario asserts rules.yaml does NOT travel and
  passed on both backends.
- **P1 (no data loss): 17 violations in the raw walk** — exactly the
  versions whose local `backup_keep=5` windows had rotated. This is the
  designed retention boundary (a local crash window, not an archive), and
  0.4 makes the store the archive: the upgraded verifier must recover every
  one of them.

The upgraded verifier (`tests/review2_s3_verify.py`) ran against the same
stores and homes (`ROAM_CAMPAIGN_BASE=.../r4-final`, campaign log passed via
`ROAM_CAMPAIGN_LOG`). **Exit 0.** Verbatim:

```
versions ever pushed (bundle manifests): 25
paths proven user-destroyed (peer refusal + harness rewrite in transcript): 1
versions missing from both homes and both backup windows: 17
versions recovered through Engine.history/restore into the
scratch home (hash-checked): 16
  + SOUL.md@36c1f5a2e92c — restored from the local store, generation 1
  + SOUL.md@c30d907b20a6 — restored from the local store, generation 2
  + SOUL.md@e8415e2b26b4 — restored from the local store, generation 6
  + memories/MEMORY.md@140cef2e6557 — restored from the local store, generation 1
  + memories/MEMORY.md@8195db204c50 — restored from the local store, generation 2
  + memories/MEMORY.md@ebbdf667d450 — restored from the local store, generation 5
  + profiles/p/SOUL.md@9793d703e6ba — restored from the local store, generation 6
  + profiles/p/SOUL.md@aea705b7cbd3 — restored from the local store, generation 8
  + profiles/p/SOUL.md@f03aeb6c4297 — restored from the local store, generation 2
  + profiles/p/skills/pskill/SKILL.md@7a166ecf06ef — restored from the local store, generation 5
  + skills/deep/nested/x/SKILL.md@64a0f00abcdb — restored from the local store, generation 10
  + skills/deep/nested/x/SKILL.md@f25bddbdfecc — restored from the local store, generation 12
  + skills/skill-a/SKILL.md@009a7aff3b37 — restored from the local store, generation 3
  + skills/skill-b/SKILL.md@42a5ca7343d3 — restored from the local store, generation 6
  + skills/skill-b/SKILL.md@b42fca03775a — restored from the local store, generation 9
  + skins/theme-a/skin.css@87ffda9efcad — restored from the local store, generation 9
P1 HOLDS: every version ever pushed that the sync system touched is still
present in a home, in a roam/backups directory, or was restored from the
remote store through the plugin's own recovery surface and hash-checked
```

The 17th missing version is the one path the harness itself destroyed (the
peer refused every version of it and the harness rewrote it in place — a
user rewrite, out of scope, proven from the transcript). Every other version
ever pushed was either present in a home, present in a backup window, or
restored through `Engine.history` + `Engine.restore --apply` into a scratch
home and hash-matched. Transcript: `docs/verifier-r4-final.log`.

## 5. Suite

```
.venv/bin/python -m pytest -q    → 536 passed, 1 warning — three consecutive
                                 full runs (119.7 s / 120.5 s / 120.4 s), 0 failed
.venv/bin/ruff check roam tests → All checks passed!
```

The single warning is a stdlib `zipfile` UserWarning from
`test_archive.py::test_duplicate_member_rejected`, which deliberately builds
a duplicate-name archive to assert the engine rejects it. The round-4 lanes
added 15 history/restore engine tests, 11 CLI/slash surface tests, the
list_blobs backend contract tests, and 2 status carry-forward regression
tests; all are in the 536.

## 6. Retention model (0.4.0)

`backup_keep` (default 5) is a local crash window only. The remote store
keeps every generation ever pushed and is never pruned by roam; `hermes roam
history` enumerates it and `hermes roam restore --apply` recovers any
generation. Pruning the archive is a manual operator decision (README
documents how). The campaign above is the proof that this model loses
nothing: the versions that fell out of every local backup window were all
still in the store and all recovered through the plugin's own surface.

## 7. Commits on main for this proof

```
4a6466c (fix4-h1) engine+backends: signed head chaining (parent_bundle_key) and list_blobs on local/s3/git
20ff960 (fix4-h1) engine: Engine.history (verified chain walk) and Engine.restore (journaled recovery)
f0633b0 (fix4-h1) cli+slash: hermes roam history / restore; /roam history read-only
2a19b99 (fix4-h1) verifier P1 upgrade + README retention model
0cd1ac2 (fix4-h2) engine: status must not count carry-forward fleet entries as local dirt
c790867 (fix4-h2) harness: P2 convergence only on paths shared under every device's rules
5afaa8d (fix4-h2) docs: round-4 h2 campaign rerun proof (P2 converges on both backends)
4d7da0a integrate r4: merge fix4-h1 + fix4-h2; verifier/harness env overrides; demo stage 3; suite x3 green (536)
d901a7c proof: final campaign (P2 0 both backends) + verifier exit 0 + demo exit 0; transcripts archived
deb6bf9 release 0.4.0: version bump, CHANGELOG 0.4.0, list_blobs disclosures
(final)   REAL_HERMES_PROOF 0.4.0 rewrite; gate WARN disclosure; scratch cleanup
```
