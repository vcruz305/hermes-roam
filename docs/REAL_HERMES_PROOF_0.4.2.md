# REAL HERMES PROOF — hermes-roam 0.4.2 first-pull config merge

Date: 2026-11-07 (PST). Host: the campaign VPS, Hermes 0.22.5 (`hermes` on
PATH). Plugin: hermes-roam **0.4.2**, checkout `<work>/hermes-roam`
(committed HEAD, `effc4d0`; installed into both scratch homes with
`hermes plugins install file://<work>/hermes-roam --yes --force`).
Scratch homes: `<work>/roam-homes/fp-a` (device `vps`) and
`<work>/roam-homes/fp-b` (device `beta`), remote: a local bare git
repository (`file://<work>/roam-homes/fp-remote.git`). Both homes share
one `ROAM_ARCHIVE_KEY` ([REDACTED]). Every command below ran from
`<work>` with `HERMES_HOME` pointed at the scratch home; scratch homes
were deleted after the proof.

## Setup

Device A (`fp-a`, `vps`): a real Hermes home seeded by `hermes` itself; set
`model.provider`, `model.default`, `model.api_key` ([REDACTED]), `terminal.cwd`,
and the roam plugin settings (git backend, the bare remote, device name).
`SOUL.md` gained a fleet ruling line. `hermes roam init --create-remote`,
then:

```
roam: pushed generation 1 (3 files, 1493 bytes)
roam: bundle bundles/000001-vps-bc871cc11efc.roam
```

Device B (`fp-b`, `beta`): a brand-new real Hermes home. Its `config.yaml`
exists ONLY because the user ran `hermes config set` for its own
`model.api_key` ([REDACTED]) and the roam plugin settings. `hermes roam init`.

## Step 1 — plan-only pull (the 0.4.1 poisoning step)

`hermes roam pull` printed the full plan and, new in 0.4.2, the notices the
0.4.1 surfaces hid:

```
roam: plan only — nothing was written (remote generation 1)
would update shared config.yaml
kept local (changed here since last sync): SOUL.md
kept local by your scope rules: roam/installed-plugins.json
roam: config.yaml: never synced here; the remote's shared keys are added; host-scoped and secret keys stay local
roam: would first-pull merge config.yaml (remote-only shared keys added, local values win where both are set, host-scoped and secret keys stay local)
roam: 1 path(s) changed locally since the last sync and remotely at the same time (conflicts) were kept local: SOUL.md; ...
roam: this device has never synced with this remote; if it is a brand-new device whose local files are only Hermes's fresh seed content, run `hermes roam pull --apply --overwrite-conflicts` to take the remote home (a backup of everything replaced is kept)
```

Nothing was written and no base was recorded — under 0.4.1 this very command
saved the downloaded bundle as the three-way config base, which made the
later apply see remote == base and keep the whole config local.

## Step 2 — first apply: the union

`hermes roam pull --apply` reported
`config.yaml first-pull merged: the remote's shared keys were added where
the local file lacked them, local values won for shared keys both sides set,
and host-scoped and secret keys stayed local`, kept `SOUL.md` local as a
listed conflict (with the brand-new-device guidance), and took a backup.
B's `config.yaml` afterwards:

```yaml
plugins:
  enabled: [hermes-roam]
  entries:
    hermes-roam:
      settings: {backend: git, git_remote: file:///.../fp-remote.git, device_name: beta}
model:
  api_key: [REDACTED]      # B's own key — kept
  provider: openrouter    # A's shared value — ARRIVED (0.4.1 kept it out)
  default: glm-5          # A's shared value — ARRIVED (0.4.1 kept it out)
```

B's own api key and roam settings stayed local; A's shared model settings
arrived. `SOUL.md` was still Hermes's seeded default (0 occurrences of the
fleet ruling).

## Step 3 — the overwrite pull

`hermes roam pull --apply --overwrite-conflicts` replaced `SOUL.md` with
A's (the fleet ruling is now present — under 0.4.1 this flag was
short-circuited for everything) and did NOT rewrite `config.yaml`: the
canonical shared-projection digest proved B's config already carries the
full remote projection, so it was reported as unchanged and excluded from
the backup. The pull's backup directory contains `SOUL.md` only.

## Step 4 — convergence

```
roam: 2 bundled path(s) are unchanged and were neither rewritten nor backed up
local dirty  0 shared files changed — nothing to push
roam: pull preview — nothing would change against remote generation 1
accepted config base recorded: ['config.yaml']
B api_key still local: True
```

`hermes roam devices` lists both `vps` (pushed 1) and `beta` (pulled 1).
Further pulls are no-ops; the accepted config base is recorded so future
remote config changes are real three-way comparisons; B's secret never
travelled.

## Gates

- `python -m pytest tests -q`: **558 passed, 1 skipped**; `ruff check .`: clean.
- `hermes plugins validate <work>/hermes-roam --install-deps`: **Validation passed.**
- `scripts/demo-two-homes.sh`: **exit 0** (both backends, real Hermes homes).
- The exact repro above is also pinned by `tests/test_first_pull_config.py`
  (real pipeline, real scope code, in-memory backend).

Commits: `9656ece` (union + accepted-base semantics + regression tests),
`e11cc47` (docs/version 0.4.2), `c701fba` (plan surfaces show first-pull
notices), `effc4d0` (canonical order-independent projection digest — found
live in this proof: the overwrite pull initially reordered a config the
device already carried).
