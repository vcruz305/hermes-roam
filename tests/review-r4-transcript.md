# r4 live-behavior transcript — real Hermes 0.22.5, realistic dummy home

Environment: Hermes Agent v0.22.5 (bf0203a794a0) on PATH; every command run
from cwd /root/work with HERMES_HOME=/root/work/roam-homes/review-r4-real.
The dummy home replicates the NAME structure of /root/.hermes (depth<=3, minus
hermes-agent/, node/, tools/, cache dirs; per-dir entries capped at 12, top
level complete) with every file containing only `dummy <relpath>`; no secret
content was ever copied. Remote store: local backend at
/root/work/roam-homes/review-r4-remote. Worktree: review-r4 @ fe2558a.

## 1. Install + init + push on the realistic home

```
$ hermes plugins install file:///root/work/hermes-roam --yes-deps --enable
  hermes-roam declares Python dependencies:
    - cryptography>=42,<51
    - pyyaml>=6.0,<7          # <- pyproject main deps; plugin.yaml's boto3 is NOT installed
  ✓ Installed / ✓ Resolving / ✓ Installing Python dependencies

$ hermes roam init --create-remote
roam: created the generation-0 remote head on the local backend and registered device 'r4-real'

$ hermes roam push --dry-run --json
{"generation": 1, "bundle_key": "", "bundle_size": 0, "file_count": 531, "dry_run": true, "skipped_reason": ""}

$ time hermes roam push
roam: pushed generation 1 (531 files, 163886 bytes)
real  0m24.693s   user 0m24.183s

$ time hermes roam status     # real 0m26.324s
$ time hermes roam doctor     # real 0m24.753s -> 7/7 checks passed
```

What the 531 bundled files include (classified with the repo's own
`roam.inventory.walk_home` against the dummy home): besides skills/memories/
SOUL.md/config.yaml, the SHARED catch-all picks up `hf_token`,
`.antigravity_oauth.json`, `shared/nous_auth.json`, `kanban.db*`,
`notepad.db`, `projects.db`, `shared-state.db`, `verification_evidence.db`,
`gateway.sock`, `gateway_state.json`, `install_id`, `.hermes_history`,
`.curator_backups/blobs/*`, 196 files under `outputs/`, `.npm_lock_hash_*`,
`.provider_models_cache_*.tmp` — i.e. secrets-looking and junk top-level
files. (This is the KNOWN denylist/SHARED-catch-all issue already owned by the
fix lane; recorded here only as measured behaviour.)

## 2. CPU blowup: every command costs ~25 s of pure classifier time

cProfile on `roam.inventory.walk_home` over the dummy home (1135 files):

```
31742 classify_path calls -> 743722 _pattern_hits -> 39,204,300 fnmatchcase
  calls; 59.6 s under cProfile, ~24 s without it (matches the wall times above)
```

The dummy home is SMALLER than a real /root/.hermes (real: 4846 entries at
depth<=3 alone, 3676 blobs, 187 sessions...), so on a real home every
status/push/pull/doctor costs minutes. `hermes roam status` alone (24 s+)
already exceeds the on_session_start watchdog cap (15 s) on any realistic
home, so the startup check always overruns and its "remote is ahead" notice
never lands.

## 3. Unreachable git backend (blackholed 10.255.255.1): 2m37s, then a raw TypeError

```
$ hermes config set ... backend git; git_remote ssh://git@10.255.255.1/srv/roam.git
$ time hermes roam status
device       r4-real
backend      git
pushed       generation 1
pulled       generation 0
remote       unreachable or empty
roam: unsupported operand type(s) for -: 'NoneType' and 'int'      # exit code 1
real  2m36.786s
```

The 156 s = git mirror fetch timeout (GIT_TIMEOUT=120) + ls-remote ping
(PING_TIMEOUT=20) + the ~16 s classifier walk. This is the exact crash
reproduced deterministically in tests/test_review_r4.py (engine.status()
sets remote_ahead=True with remote_generation=None; cli.py:_cmd_status then
subtracts None - int).

## 4. Unreachable S3 backend timings (repo venv, boto3 present)

```
S3Backend(bucket r4-bucket, endpoint http://10.255.255.1:9) get_head:
  failed after 43.5 s: Connect timeout (3 attempts x ~14.5 s)
S3Backend.ping(): failed after 87.9 s: Connect timeout
```

So with a working boto3 install, status on a realistic home = walk (>=24 s) +
43.5 s, and doctor = walk + 43.5 s + 87.9 s (>2.5 min). Both exceed the ~30 s
budget; the hook watchdog caps are 15 s / 120 s.

## 5. S3 backend is dead on real installs (boto3 is never provisioned)

```
$ hermes plugins doctor hermes-roam
  WARN: declared python_dependencies not installed: boto3>=1.34,<2 — ...
  OK: runtime discovery ... registrations: 3 tool(s), 3 hook(s)

$ hermes pm repair
hermes: dependency environment repaired

$ hermes roam doctor
  [FAIL] backend reachability — boto3 is not installed; install it with: pip install 'boto3>=1.34,<2'
  [FAIL] remote head signature — boto3 is not installed; ...
  roam: 5/7 checks passed    # exit 1
```

`hermes plugins validate --install-deps` confirms the loader takes
pyproject.toml ("python dependencies — 2 requirements from pyproject"):
cryptography + pyyaml. plugin.yaml's python_dependencies (cryptography,
boto3) is never installed, and `hermes pm repair` does NOT provision boto3 —
docs/DISCLOSURES.md item 1 claims pm repair "was verified to do exactly
that"; on Hermes 0.22.5 it does not. The README quick start (backend s3/R2)
cannot work on any real install.

## 6. plugins validate --install-deps (HERMES_HOME=dummy home, cwd /root/work)

```
✓ python dependencies — 2 requirements from pyproject
✓ capability probe — register() ran in isolation
✓ declared tools/hooks/middleware — matches registrations
✓ security scan — safe
✓ no core override — no runtime rebinds of Hermes core
⚠ private core imports (catalog rule 9): settings.py:170 hermes_cli.version_info.get_version_info
Validation passed.
```

The one caution is the rule-9 private import (disclosed in
docs/DISCLOSURES.md item 2, but unresolved).

`hermes plugins doctor` from cwd /root/work cannot complete on this VPS: the
plugin host's isolation step copies the entire cwd into scratch and hits
"No space left on device" (environment artifact, not a plugin defect);
`hermes plugins doctor hermes-roam` from a small cwd works and its full
output is in section 5 above.

## 7. CLI surface checks that PASSED

- every subcommand's --help renders (init/status/devices/diff/push/pull/doctor/rules);
- `roam {status,devices,diff,pull,doctor,push --dry-run} --json` all parse
  with `python -m json.tool`;
- exit codes observed: 0 success, 1 error (ConfigError/unreachable backend),
  2 conflict (RemoteAhead) — except the raw-TypeError status crash (section 3).

## 8. Hook cap overrun (deterministic proof in tests/test_review_r4.py)

hooks._spawn starts a daemon worker plus a watchdog that only joins with a
timeout and logs "abandoning it" — the worker keeps running (test
test_on_session_start_watchdog_does_not_bound_work: worker still alive well
past the cap). With the git backend the abandoned startup check keeps a
git/ssh subprocess chain alive for >=156 s inside the session; README's
"capped at 15 s" only describes the watchdog's patience, not a bound on the
work.
