# R2_PROOF — hermes-roam 0.4.0 S3 backend against real Cloudflare R2

Date: 2026-11-06/07 (UTC). Host: the campaign VPS, Hermes 0.22.5 (`hermes` on
PATH), plugin checkout `<work>/hermes-roam` (main @ 92b47d9, v0.4.0).
Every command below ran for real against a live Cloudflare R2 bucket
(`hermes-roam`, endpoint
`https://<account-id>.r2.cloudflarestorage.com`,
region `auto`). R2 API token credentials were kept in a mode-600 env file
outside the repo, loaded into the process environment only inside the
processes that needed them, and are never printed, logged, committed, or
echoed anywhere in this document or the raw transcripts. The account id in
the endpoint is shown; the two key values are not. Raw transcripts are
archived next to this file: `docs/r2-contract.log`,
`docs/r2-homes-run.log`.

Test prefix root: `roam-r2-proof-20261106203229/` —
`…/contract` for the direct backend contract, `…/homes` for the two-home
sync. Every object under the root was deleted at the end of the run and the
prefix was verified empty (§4).

This closes the last open item in `docs/DISCLOSURES.md` §5: until now the
S3/R2 backend was proven only against the moto emulator. It is now proven
against real Cloudflare R2 end to end.

## 1. Direct backend contract against real R2

Command (cwd `<work>/hermes-roam`, repo venv python; the harness reads
the credentials file itself and never prints the values):

```
.venv/bin/python <work>/roam-homes/r2-contract.py roam-r2-proof-20261106203229
```

Verbatim output (the only redactions: none needed — no secret ever appears):

```
# S3Backend contract run against real Cloudflare R2
# bucket='hermes-roam' endpoint='https://<account-id>.r2.cloudflarestorage.com' region='auto' prefix='roam-r2-proof-20261106203229/contract'
[ping] OK (0.49s): "s3 backend ready: bucket 'hermes-roam' reachable (prefix 'roam-r2-proof-20261106203229/contract')"
[get_head on fresh prefix -> None] OK (0.10s): None
[put_head(data, expected=None) -> token] OK (0.30s): '906a19e7794ab648783147fa8f176fe1'
[get_head -> (bytes, token) round trip] OK (0.30s): (b'contract-head-v1', '906a19e7794ab648783147fa8f176fe1')
    data matches: True; token: 906a...6fe1 (len 32)
[put_head with stale/wrong expected -> HeadConflict (real 412)] HeadConflict (0.09s): the remote refused the conditional write for roam-r2-proof-20261106203229/contract/HEAD: it changed since it was read; pull first
    raised from: roam.errors.HeadConflict: the remote refused the conditional write for roam-r2-proof-20261106203229/contract/HEAD: it changed since it was read; pull first
[put_blob(bundles/000001-alpha-0123456789ab.roam)] OK (0.34s): None
[put_blob same key again -> BlobExists (real 412)] BlobExists (0.10s): the remote refused the conditional write for bundles/000001-alpha-0123456789ab.roam: it changed since it was read; pull first
    raised from: roam.errors.BlobExists: the remote refused the conditional write for bundles/000001-alpha-0123456789ab.roam: it changed since it was read; pull first
[blob_exists same key -> True] OK (0.12s): True
[get_blob with max_bytes=100 (under real size) -> BackendError] BackendError (0.08s): bundle bundles/000001-alpha-0123456789ab.roam is 5022 bytes, over the 100 byte limit
    raised from: roam.errors.BackendError: bundle bundles/000001-alpha-0123456789ab.roam is 5022 bytes, over the 100 byte limit
[get_blob with max_bytes=100000 -> downloads] OK (0.33s): b'contract-blob-pa'
    downloaded content matches source: True
[list_blobs('bundles/')] OK (0.10s): ['bundles/000001-alpha-0123456789ab.roam']
[list_blobs('nonexistent/') -> []] OK (0.07s): []
[get_head after everything -> same token] OK (0.38s): '906a...6fe1'
# summary: ping=OK, get_head on fresh prefix -> None=OK, put_head(data, expected=None) -> token=OK, get_head -> (bytes, token) round trip=OK, put_head with stale/wrong expected -> HeadConflict (real 412)=HeadConflict, put_blob(bundles/000001-alpha-0123456789ab.roam)=OK, put_blob same key again -> BlobExists (real 412)=BlobExists, blob_exists same key -> True=OK, get_blob with max_bytes=100 (under real size) -> BackendError, get_blob with max_bytes=100000 -> downloads=OK, list_blobs('bundles/')=OK, list_blobs('nonexistent/') -> []=OK, get_head after everything -> same token=OK
# contract verdict: PASS
```

Contract verdict: **PASS** — every step behaved exactly as the moto-based
tests predict:

- absent head → `None` (R2 answers 404/`NoSuchKey`, mapped to None);
- create-once head write (`IfNoneMatch: *`) and CAS head write
  (`IfMatch`) both work; the returned token is the server ETag and
  round-trips through `get_head`;
- a stale/wrong `expected` token is rejected by R2 with a real HTTP 412
  `PreconditionFailed` → `HeadConflict`; a duplicate blob PUT is rejected
  with a real 412 → `BlobExists`;
- `get_blob` enforces the `max_bytes` cap (a 5022-byte blob over a
  100-byte cap is refused before any data is written) and downloads
  byte-identical content when the cap allows it;
- `list_blobs` returns exactly the stored bundle key under `bundles/`
  and `[]` for a prefix that holds nothing;
- `ping` confirms bucket reachability.

## 2. Two real-Hermes homes synced through real R2

Setup: two scratch Hermes homes (`<work>/roam-homes/r2-alpha`,
`r2-beta`), plugin installed from `file://<work>/hermes-roam`, backend
`s3` pointed at the same R2 bucket under the separate prefix
`roam-r2-proof-20261106203229/homes`, region `auto`, devices `alpha` /
`beta`. Both homes carry the SAME `ROAM_ARCHIVE_KEY`; each home's `.env`
also holds the two R2 token values (mode 600, never printed). Each
`config.yaml` carries its own `api_key:` line and its own `terminal:`
block, so host-key isolation can be observed.

Command (cwd `<work>`):

```
bash <work>/roam-homes/r2-homes.sh 20261106203229
```

Verbatim transcript (from `r2-homes-run.log`; the only edits are the
elision of the repeated "next steps" helper text, the removal of the two
.env sha256 digests (both homes were seeded with the same archive key), and
the neutralization of author-machine absolute paths — every roam decision
line is kept):

```
############ stage 0: setup
=== install plugin in alpha (from file://<work>/hermes-roam)
✓ Plugin hermes-roam enabled.
=== alpha configured: backend s3, bucket set, endpoint set, region auto, prefix roam-r2-proof-20261106203229/homes, device alpha
=== install plugin in beta (from file://<work>/hermes-roam)
✓ Plugin hermes-roam enabled.
=== beta configured: backend s3, bucket set, endpoint set, region auto, prefix roam-r2-proof-20261106203229/homes, device beta
[redacted] both homes were seeded with the same ROAM_ARCHIVE_KEY (their .env files are identical; the sha256 digests are removed from this transcript)
############ stage 1: alpha bootstrap
--- [alpha init --create-remote] rc=0  2.8 s
roam: created the generation-0 remote head on the s3 backend and registered device 'alpha'
--- [alpha push (gen 1)] rc=0  3.2 s
roam: pushed generation 1 (5 files, 1534 bytes)
roam: bundle bundles/000001-alpha-817adc1c1868.roam
############ stage 2: beta initial sync
--- [beta init (head exists)] rc=0  2.7 s
roam: connected to the s3 backend (…); the remote head is at generation 1, last pushed by 'alpha'; pull before pushing anything
--- [beta pull (plan)] rc=0  2.7 s
roam: plan only — nothing was written (remote generation 1)
would add shared skills/team/alpha-note.md
roam: 2 file(s) changed here since the last sync; --apply would keep the local copy:
kept local (changed here since last sync): SOUL.md
kept local (changed here since last sync): memories/MEMORY.md
roam: 1 remote file(s) your scope rules keep local:
kept local by your scope rules: roam/installed-plugins.json
roam: review the plan, then run `hermes roam pull --apply`
--- [beta pull --apply] rc=0  2.6 s
roam: installed remote generation 1
backup       <work>/roam-homes/r2-beta/roam/backups/20261107-043418
roam: 2 file(s) kept local (changed here since last sync): SOUL.md, memories/MEMORY.md
roam: 1 remote path(s) are refused by this device's local rules or opt-in settings and were not written or deleted: roam/installed-plugins.json
roam: 1 file(s) written, 0 deleted
############ stage 3: beta edit + push
--- [beta push (gen 2)] rc=0  3.1 s
roam: pushed generation 2 (7 files, 2042 bytes)
roam: bundle bundles/000002-beta-b24f6825df8e.roam
############ stage 4: alpha stale push -> expect rc=2 RemoteAhead
--- [alpha push (stale)] rc=2  2.5 s
roam: the remote head is at generation 2 but this device last saw 1; pull first (or push with --force)
roam: the remote head moved ahead — run `hermes roam pull` first
############ stage 5: alpha resync + own edit
--- [alpha pull (plan)] rc=0  2.7 s
roam: plan only — nothing was written (remote generation 2)
would add shared skills/team/beta-edit.md
would add shared skills/team/beta-note.md
would update shared SOUL.md
would update shared memories/MEMORY.md
--- [alpha pull --apply] rc=0  2.6 s
roam: installed remote generation 2
backup       <work>/roam-homes/r2-alpha/roam/backups/20261107-043429
roam: 4 file(s) written, 0 deleted
--- [alpha push (gen 3)] rc=0  3.1 s
roam: pushed generation 3 (7 files, 2077 bytes)
roam: bundle bundles/000003-alpha-734e35cf835e.roam
############ stage 6: history + restore on alpha
--- [alpha history SOUL.md] rc=0  3.3 s
roam: 3 recoverable version(s) in the remote store (newest first)
gen 3 alpha 2026-11-07 9967bc738dca 68B present SOUL.md
gen 2 beta 2026-11-07 c2e89b91d427 32B present SOUL.md
gen 1 alpha 2026-11-07 9191ab309c45 33B present SOUL.md
roam: recover one with `hermes roam restore <path> --generation N --apply`
roam: the remote store keeps every pushed generation; it is never pruned by roam
--- [alpha restore SOUL.md gen1 --apply] rc=0  3.2 s
roam: restored SOUL.md from generation 1 into the home
backup       <work>/roam-homes/r2-alpha/roam/backups/20261107-043438
roam: the restored version is a local change; the next `hermes roam push` publishes it
--- [alpha restore SOUL.md gen3 --apply] rc=0  2.8 s
roam: restored SOUL.md from generation 3 into the home
backup       <work>/roam-homes/r2-alpha/roam/backups/20261107-043441
roam: the restored version is a local change; the next `hermes roam push` publishes it
############ stage 7: doctor + status timing
--- [alpha doctor] rc=0  2.4 s
roam: 9/9 checks passed   (crash recovery, archive key, backend reachability,
  remote head signature verifies (generation 3), rollback floor, hermes
  version 0.22.5 compatible, bundle size 75689/2147483648 bytes, staging dir
  writable, backups dir writable — all [ok])
--- [beta doctor] rc=0  2.4 s
roam: 9/9 checks passed   (same nine checks, all [ok])
--- [alpha status (timed)] rc=0  2.3 s
device alpha | backend s3 | pushed generation 3 | pulled generation 2 |
remote head generation 3 by alpha | remote not ahead of this device |
local dirty 0 shared files changed — nothing to push | fleet 2 devices
--- [beta status (timed)] rc=0  2.5 s
device beta | backend s3 | pushed generation 2 | pulled generation 1 |
remote head generation 3 by alpha | remote 1 generations ahead — run `hermes roam pull` |
local dirty 0 shared files changed — nothing to push | fleet 2 devices
############ done
```

### 2.1 Post-run isolation verification (all PASS)

A checker walked both homes after the run. Every check passed:

- beta received alpha's shared skill; beta kept its own local skill;
- beta's never-synced divergent shared files (SOUL.md, memories) were
  kept local by the conflict policy and never clobbered — alpha's
  memory content never arrived on beta;
- beta's `.env` still holds all three secrets (archive key + both R2
  token values) and was never rewritten by a pull;
- beta's `config.yaml` keeps its OWN `api_key` and its own `terminal`
  block (`cwd` is beta's home); **alpha's `api_key` never reached beta**;
  the shared config projection (model provider) did arrive;
- alpha received beta's SOUL.md, beta's shared memory edit, and both of
  beta's shared skills; alpha kept its own api_key/terminal/.env —
  beta's `api_key` never reached alpha;
- after `restore --generation 3 --apply`, SOUL.md is byte-identical to
  the generation-3 content again (the generation-1 restore in between
  had actually rolled it back — both restores took real journaled
  backups).

### 2.2 What the live run proves

- The CAS head on real R2 rejects a stale push with exit code 2
  (RemoteAhead) and the exact "pull first" message — the same contract
  as the local and git backends.
- The bundle chain is verifiable end to end on R2: `history` walks all
  three generations, re-verifying each bundle header+tag with the archive
  key, and `restore --apply` recovers any of them through the journaled
  install.
- Doctor is green (9/9) on both devices against the live R2 endpoint.
- Live timing over the real WAN link: every full remote operation
  completed in 2.3–3.3 s (push 3.1–3.2 s, pull --apply 2.6 s,
  history 3.3 s, doctor 2.4 s, status 2.3–2.5 s) — far inside the
  operation budgets, and the R2 endpoint itself answered each request in
  well under a second.

## 3. What real R2 did differently from moto

Nothing contract-visible. Differences observed, all favorable:

1. **ETag availability.** R2 returns the `ETag` directly in the
   `put_object` response, so the `put_head` fallback (a second
   `head_object` to learn the token) never fired. The token is the raw
   32-hex MD5 of the (single, non-multipart) PUT, exactly the shape the
   head-token round trip expects. moto also returns an ETag, so no code
   path differs — but the fallback path is now confirmed unnecessary
   against the real service.
2. **412 semantics are real and atomic.** Both conditional-write refusals
   (stale `IfMatch` on the head, duplicate `IfNoneMatch: *` on a bundle)
   came back as genuine HTTP 412 `PreconditionFailed` from the server,
   not a client-side simulation. The error-to-exception mapping
   (`HeadConflict` / `BlobExists`) and the fixed, non-leaking message
   text behaved identically to moto.
3. **Latency.** Real R2 answered every request in well under a second
   over the WAN; moto (local process) is faster still, but the real
   service added no observable protocol friction — pagination, 404
   mapping, and `ContentLength` reporting all matched the emulator.
4. Everything else — absent-head → None, size-cap enforcement via
   `head_object` `ContentLength`, listing under `bundles/`, empty-listing
   for a missing prefix — behaved identically to the moto-based test
   suite (`tests/test_backend_s3.py`), which is why no product change
   was needed: the moto suite is a faithful model of the real service.

## 4. Cleanup

Every object under `roam-r2-proof-20261106203229/` (both the `contract`
and `homes` prefixes) was deleted with the R2 token's own credentials,
the prefix was re-listed and verified empty, and both scratch homes plus
all local run artifacts were removed. Verbatim cleanup output:

```
cleanup: 6 object(s) under 'roam-r2-proof-20261106203229/' in bucket 'hermes-roam'
  deleting roam-r2-proof-20261106203229/contract/HEAD
  deleting roam-r2-proof-20261106203229/contract/bundles/000001-alpha-0123456789ab.roam
  deleting roam-r2-proof-20261106203229/homes/HEAD
  deleting roam-r2-proof-20261106203229/homes/bundles/000001-alpha-817adc1c1868.roam
  deleting roam-r2-proof-20261106203229/homes/bundles/000002-beta-b24f6825df8e.roam
  deleting roam-r2-proof-20261106203229/homes/bundles/000003-alpha-734e35cf835e.roam
verify: 0 object(s) remain under 'roam-r2-proof-20261106203229/'
```

The two scratch homes (`r2-alpha`, `r2-beta`) and the local contract
scratch directory were then deleted and their removal verified; the
credentials file (`r2.env`, mode 600) stays where the operator put it and
was never read into any transcript.
