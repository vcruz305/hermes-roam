#!/usr/bin/env python3
"""Review lane s3 — randomized multi-round two-device no-data-loss property test.

Drives the REAL `hermes roam` CLI (live Hermes 0.22.5, cwd /root/work) against
two scratch homes over the local backend and the git backend (local bare repo),
~10 seeded random rounds per backend: create/edit/delete shared skills,
memories, SOUL and profile content on both devices with random sync
interleavings, then a convergence cycle.

After every round it asserts:
  P1 (no data loss): every (path, sha) version that was ever pushed to the
     remote or ever present on BOTH devices is still present in at least one
     of {home alpha, home beta, alpha roam/backups, beta roam/backups}.
  P2 (convergence): after the convergence cycle both homes agree on the
     content of every tracked path that is shared under BOTH devices' own
     rules, and neither reports the remote ahead or local dirt. A path one
     device keeps local (its rules.yaml narrowing) legitimately differs per
     device and is excluded from the comparison.

Dedicated scenarios after the random rounds: sessions opt-in on ONE device,
profile secret stripping, rules.yaml narrowing (local + shared + host_keys).

Run:  /root/work/hermes-roam/.venv/bin/python tests/review2_s3_harness.py \
          <log.md> [seed]
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

WORK = Path("/root/work")
HERMES = "hermes"
# The campaign homes/stores and the checkout under test are overridable so a
# rerun never touches the archived evidence homes (round-4 h2).
BASE = Path(os.environ.get("ROAM_CAMPAIGN_BASE", "/root/work/roam-homes"))
CHECKOUT = Path(os.environ.get("ROAM_CHECKOUT", str(WORK / "hermes-roam")))
HOMES = {
    "alpha": BASE / "review2-s3-a",
    "beta": BASE / "review2-s3-b",
}
STORE_LOCAL = BASE / "review2-s3-remote"
STORE_GIT = BASE / "review2-s3-git-remote.git"
KEYFILE = Path(
    os.environ.get(
        "ROAM_TEST_KEY", "/root/.hermes/cache/scratch/s3-test-key.txt"
    )
)

TRACKED = [
    "SOUL.md",
    "memories/MEMORY.md",
    "skills/skill-a/SKILL.md",
    "skills/skill-b/SKILL.md",
    "skills/deep/nested/x/SKILL.md",
    "skins/theme-a/skin.css",
    "profiles/p/SOUL.md",
    "profiles/p/memories/PMEM.md",
    "profiles/p/skills/pskill/SKILL.md",
    "profiles/p/skins/pskin.css",
]

log_lines: list[str] = []


def log(text: str) -> None:
    log_lines.append(text)
    print(text, flush=True)


def cli(device: str, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(HOMES[device])
    proc = subprocess.Popen(
        [HERMES, "roam", *args],
        cwd=str(WORK), env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,  # the whole call tree is killable on timeout
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(
            proc.args, proc.returncode, stdout, stderr,
        )
    except subprocess.TimeoutExpired:
        # kill the entire session (hermes spawns helper subprocesses that
        # inherit the pipes; killing only the direct child would leave the
        # pipe open and the wait would spin forever)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        stdout, stderr = proc.communicate()
        return subprocess.CompletedProcess(
            proc.args, proc.returncode, stdout, stderr,
        )


def out(proc: subprocess.CompletedProcess) -> str:
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-4:])
    return tail


# ------------------------------------------------------------------ snapshots

def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def home_map(device: str) -> dict[str, str]:
    """sha of every file under the shared/opt-in surfaces of the home — the
    full walk, not a hardcoded list, so the invariant checker sees every
    path a bundle could ever have carried."""
    home = HOMES[device]
    result = {}
    for path in sorted(home.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(home).as_posix()
        if rel.startswith(("roam/", "cache/", "audio_cache/", "image_cache/",
                           "logs/", "installs/", "plugins/", "pairing/",
                           "node/", "tools/", "lsp/", "runtime/", "gateway",
                           "desktop", "bin/", "hermes-agent/", "__pycache__")):
            continue
        try:
            result[rel] = sha_bytes(path.read_bytes())
        except OSError:
            result[rel] = "unreadable"
    return result


def backup_shas(device: str) -> set[str]:
    """Every content sha sitting in the device's roam/backups tree."""
    backups = HOMES[device] / "roam" / "backups"
    shas: set[str] = set()
    if not backups.is_dir():
        return shas
    for path in backups.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                shas.add(sha_bytes(path.read_bytes()))
            except OSError:
                pass
    return shas


def store_pushed_versions(backend: str) -> set[tuple[str, str]]:
    """(relpath, sha) pairs of every version ever pushed, from the store's
    immutable bundles (decrypted with the test key)."""
    sys.path.insert(0, str(CHECKOUT))
    from roam import archive  # noqa: PLC0415

    key_text = KEYFILE.read_text().strip()
    raw = base64.urlsafe_b64decode(key_text + "=" * (-len(key_text) % 4))
    pairs: set[tuple[str, str]] = set()
    roots = [STORE_LOCAL / "bundles"] if backend == "local" else []
    if backend == "git":
        proc = subprocess.run(
            ["git", "-C", str(STORE_GIT), "cat-file", "--batch-all-objects", "--batch-check"],
            capture_output=True, text=True, timeout=300,
        )
        blobs = [line.split()[0] for line in proc.stdout.splitlines()
                 if line.split()[2:3] == ["blob"]]
        for blob in blobs:
            data = subprocess.run(
                ["git", "-C", str(STORE_GIT), "cat-file", "blob", blob],
                capture_output=True, timeout=300,
            ).stdout
            if not data.startswith(b"ROAM1"):
                continue
            tmp = Path("/root/.hermes/cache/scratch/s3-blob.roam")
            tmp.write_bytes(data)
            try:
                dest = Path("/root/.hermes/cache/scratch/s3-blob-x")
                if dest.exists():
                    shutil.rmtree(dest)
                limits = archive.ArchiveLimits(100000, 2**31, 2**31)
                manifest = archive.open_bundle(tmp, key=raw, dest_dir=dest, limits=limits)
                pairs.update((k, v.get("sha256")) for k, v in manifest.items()
                             if isinstance(v, dict))
            except Exception:  # noqa: BLE001 - non-bundle blobs are skipped
                continue
        return pairs
    for bundle in sorted(roots[0].glob("*.roam")):
        dest = Path("/root/.hermes/cache/scratch/s3-blob-x")
        if dest.exists():
            shutil.rmtree(dest)
        try:
            limits = archive.ArchiveLimits(100000, 2**31, 2**31)
            manifest = archive.open_bundle(bundle, key=raw, dest_dir=dest, limits=limits)
            pairs.update((k, v.get("sha256")) for k, v in manifest.items()
                         if isinstance(v, dict))
        except Exception as exc:  # noqa: BLE001
            log(f"NOTE: store bundle {bundle.name} could not be opened: {exc}")
    return pairs


# ------------------------------------------------------------------ bookkeeping

observed: set[tuple[str, str]] = set()      # every version ever seen in a home
seen_on: dict[tuple[str, str], set[str]] = {}  # version -> devices it lived on
violations: list[str] = []
convergence_failures: list[str] = []


def observe(device: str, mapping: dict[str, str]) -> None:
    for rel, sha in mapping.items():
        version = (rel, sha)
        observed.add(version)
        seen_on.setdefault(version, set()).add(device)


def protected_versions(backend: str) -> set[tuple[str, str]]:
    pushed = store_pushed_versions(backend)
    both = {v for v, devices in seen_on.items() if len(devices) >= 2}
    return pushed | both


def check_invariants(backend: str, round_no: int) -> None:
    protected = protected_versions(backend)
    present: dict[str, str] = {}
    for rel, sha in protected:
        # config.yaml files travel stripped and are merged key-by-key on pull,
        # so their verbatim push-time digest never exists on any device; the
        # merge surface is verified separately (scenario_profile_secrets).
        if rel == "config.yaml" or (
            rel.startswith("profiles/") and rel.endswith("/config.yaml")
        ):
            continue
        where = []
        for device in HOMES:
            if home_map(device).get(rel) == sha:
                where.append(f"home:{device}")
            elif sha in backup_shas(device):
                where.append(f"backups:{device}")
        present[f"{rel}@{sha[:12]}"] = where or ["NOWHERE"]
    lost = {k: v for k, v in present.items() if v == ["NOWHERE"]}
    if lost:
        violations.append(f"[{backend} r{round_no}] lost: " + json.dumps(lost))
        log(f"P1 VIOLATION ({backend} round {round_no}): {json.dumps(lost)}")
    else:
        log(f"P1 ok ({backend} round {round_no}): {len(protected)} protected versions all "
            f"present in homes or backups")


def convergence_paths() -> list[str]:
    """TRACKED paths that are SHARED under BOTH devices' current rules.

    Only these can converge fleet-wide. A path one device keeps local
    (its own rules.yaml narrowing) legitimately differs per device and must
    never be part of the equality comparison — comparing it made rounds fail
    after the rules scenario even though roam behaved as designed (round-4
    h2). rules.yaml itself is per-device policy and never travels."""
    sys.path.insert(0, str(CHECKOUT))
    from roam import scope  # noqa: PLC0415
    from roam.model import Scope  # noqa: PLC0415

    out = []
    for rel in TRACKED:
        shared_everywhere = True
        for device in HOMES:
            rules = scope.load_rules(HOMES[device])
            if scope.classify_path(rel, rules) is not Scope.SHARED:
                shared_everywhere = False
                break
        if shared_everywhere:
            out.append(rel)
    return out


_last_excluded: list[str] | None = None


def shared_map(device: str, paths: list[str]) -> dict[str, str]:
    """sha of the comparable SHARED-class paths only — used for the fleet
    convergence comparison (local files legitimately differ per device)."""
    home = HOMES[device]
    result = {}
    for rel in paths:
        path = home / rel
        try:
            if path.is_file() and not path.is_symlink():
                result[rel] = sha_bytes(path.read_bytes())
        except OSError:
            pass
    return result


def check_convergence(round_no: int, backend: str, record: bool = False) -> bool:
    global _last_excluded  # noqa: PLW0603 - module-run harness state
    paths = convergence_paths()
    excluded = sorted(set(TRACKED) - set(paths))
    if excluded and excluded != _last_excluded:
        _last_excluded = excluded
        log(f"  P2 comparison excludes {len(excluded)} TRACKED path(s) that are not "
            f"shared under every device's rules (per-device local by design): "
            f"{', '.join(excluded)}")
    maps = {d: shared_map(d, paths) for d in HOMES}
    equal = maps["alpha"] == maps["beta"]
    ahead = {}
    for device in HOMES:
        proc = cli(device, "status", "--json")
        try:
            data = json.loads(proc.stdout)
            ahead[device] = (
                bool(data.get("remote_ahead")),
                int(data.get("dirty_count", -1)) == 0,
            )
        except (ValueError, TypeError, KeyError):
            ahead[device] = ("status-parse-failed", False)
    clean = all(v == (False, True) for v in ahead.values())
    if equal and clean:
        log(f"P2 ok ({backend} round {round_no}): homes converged, both clean ({ahead})")
        return True
    if record:
        convergence_failures.append(f"[{backend} r{round_no}] equal={equal} status={ahead}")
    log(f"P2 not yet converged ({backend} round {round_no}): equal={equal} status={ahead}")
    return False


# ------------------------------------------------------------------ operations

def user_op(device: str, rel: str, action: str) -> None:
    path = HOMES[device] / rel
    before = sha_bytes(path.read_bytes()) if path.is_file() else None
    if action == "delete":
        if path.exists():
            path.unlink()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        if action == "create" or not path.exists():
            body = f"# s3 harness {time.time()} {os.urandom(8).hex()}\n" + "x" * 64 + "\n"
            path.write_text(body)
        elif action == "edit":
            with path.open("a") as handle:
                handle.write(f"edit {time.time()} {os.urandom(8).hex()}\n")
    after = sha_bytes(path.read_bytes()) if path.is_file() else None
    log(f"  user op: {device} {action} {rel} ({(before or 'none')[:8]} -> {(after or 'gone')[:8]})")


def push(device: str, force: bool = False) -> subprocess.CompletedProcess:
    args = ["push"] + (["--force"] if force else [])
    return cli(device, *args)


def pull_apply(device: str, overwrite: bool = False) -> subprocess.CompletedProcess:
    args = ["pull", "--apply"] + (["--overwrite-conflicts"] if overwrite else [])
    return cli(device, *args)


def sync_until_converged(backend: str, round_no: int, max_iters: int = 8) -> None:
    for iteration in range(max_iters):
        for device in ("alpha", "beta"):
            proc = pull_apply(device)
            if proc.returncode != 0:
                log(f"  sync {device} pull: rc={proc.returncode} {out(proc)}")
            proc = push(device)
            if proc.returncode == 2:
                # refused: pull first, then retry the push once
                pull_apply(device)
                proc = push(device)
            if proc.returncode not in (0,):
                log(f"  sync {device} push: rc={proc.returncode} {out(proc)}")
        if check_convergence(round_no, backend,
                             record=iteration == max_iters - 1):
            return
    log(f"  convergence NOT reached in {max_iters} iterations ({backend} r{round_no})")


def random_round(backend: str, round_no: int, rng: random.Random) -> None:
    log(f"=== [{backend}] random round {round_no} ===")
    n_ops = rng.randint(1, 3)
    for _ in range(n_ops):
        device = rng.choice(["alpha", "beta"])
        rel = rng.choice(TRACKED)
        path = HOMES[device] / rel
        if not path.exists():
            action = "create"
        else:
            action = rng.choice(["edit", "edit", "delete"])
        user_op(device, rel, action)
        for d in HOMES:
            observe(d, home_map(d))
    # random interleave of the basic sync moves (seeded shuffle)
    moves = [("alpha", "push"), ("beta", "pull_apply"),
             ("beta", "push"), ("alpha", "pull_apply")]
    rng.shuffle(moves)
    for device, move in moves:
        if move == "push":
            proc = push(device)
            if proc.returncode == 2:
                log(f"  interleave: {device} push refused (as designed): {out(proc)}")
                pull_apply(device)
                proc = push(device)
                log(f"  interleave: {device} push retry rc={proc.returncode}: {out(proc)}")
            else:
                log(f"  interleave: {device} push rc={proc.returncode}: {out(proc)}")
        else:
            overwrite = rng.random() < 0.12
            proc = pull_apply(device, overwrite=overwrite)
            log(f"  interleave: {device} pull_apply(overwrite={overwrite}) "
                f"rc={proc.returncode}: {out(proc)}")
        for d in HOMES:
            observe(d, home_map(d))
    sync_until_converged(backend, round_no)
    check_invariants(backend, round_no)


# ------------------------------------------------------------------ scenarios

def scenario_sessions(backend: str) -> None:
    log(f"=== [{backend}] scenario: sessions opt-in on ONE device only ===")
    # enable on alpha only
    proc = subprocess.run(
        [HERMES, "config", "set",
         "plugins.entries.hermes-roam.settings.sync_sessions", "true"],
        cwd=str(WORK), env={**os.environ, "HERMES_HOME": str(HOMES["alpha"])},
        capture_output=True, text=True, timeout=120,
    )
    log(f"  alpha config set sync_sessions: rc={proc.returncode} {out(proc)}")
    for device, name in (("alpha", "s-alpha"), ("beta", "s-beta")):
        path = HOMES[device] / "sessions" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"device": name, "secret": os.urandom(8).hex()}))
        log(f"  user op: {device} create sessions/{name}.json")
    observe("alpha", home_map("alpha"))
    observe("beta", home_map("beta"))
    proc = push("alpha")
    log(f"  alpha push rc={proc.returncode}: {out(proc)}")
    proc = pull_apply("beta")
    log(f"  beta pull --apply rc={proc.returncode}: {out(proc)}")
    beta_got = (HOMES["beta"] / "sessions" / "s-alpha.json").exists()
    beta_kept = (HOMES["beta"] / "sessions" / "s-beta.json").exists()
    log(f"  beta received alpha's session: {beta_got} (expect False); "
        f"beta kept its own session: {beta_kept} (expect True)")
    proc = push("beta")
    log(f"  beta push rc={proc.returncode}: {out(proc)}")
    proc = pull_apply("alpha")
    log(f"  alpha pull --apply rc={proc.returncode}: {out(proc)}")
    alpha_kept = (HOMES["alpha"] / "sessions" / "s-alpha.json").exists()
    log(f"  alpha kept its own session after pull: {alpha_kept} (expect True)")
    if beta_got or not beta_kept or not alpha_kept:
        violations.append(f"[{backend} sessions] beta_got={beta_got} "
                         f"beta_kept={beta_kept} alpha_kept={alpha_kept}")
    # disable again for the remaining rounds
    proc = subprocess.run(
        [HERMES, "config", "set", "plugins.entries.hermes-roam.settings.sync_sessions", "false"],
        cwd=str(WORK), env={**os.environ, "HERMES_HOME": str(HOMES["alpha"])},
        capture_output=True, text=True, timeout=120,
    )
    log(f"  alpha sync_sessions back to false: rc={proc.returncode}")


def scenario_profile_secrets(backend: str) -> None:
    log(f"=== [{backend}] scenario: profile secrets stay local ===")
    for device in ("alpha", "beta"):
        envfile = HOMES[device] / "profiles" / "p" / ".env"
        envfile.parent.mkdir(parents=True, exist_ok=True)
        envfile.write_text(f"PROFILE_TOKEN={os.urandom(8).hex()}\n")
        cfg = HOMES[device] / "profiles" / "p" / "config.yaml"
        cfg.write_text(
            f"profile: {device}-p\nmodel: gpt-test\napi_key: sk-{os.urandom(12).hex()}\n"
            f"notes: shared-note-{device}\n"
        )
    proc = push("alpha")
    log(f"  alpha push rc={proc.returncode}: {out(proc)}")
    proc = pull_apply("beta")
    log(f"  beta pull --apply rc={proc.returncode}: {out(proc)}")
    beta_cfg = (HOMES["beta"] / "profiles" / "p" / "config.yaml").read_text()
    beta_env = (HOMES["beta"] / "profiles" / "p" / ".env").read_text()
    alpha_key = next(
        line for line in (HOMES["alpha"] / "profiles" / "p" / "config.yaml")
        .read_text().splitlines() if line.startswith("api_key:")
    ).split("api_key:", 1)[1].strip()
    alpha_token = (HOMES["alpha"] / "profiles" / "p" / ".env").read_text()
    ok_shared = "shared-note-alpha" in beta_cfg
    ok_nokey = alpha_key not in beta_cfg
    ok_env = alpha_token not in beta_env
    log(f"  shared profile note arrived: {ok_shared}; alpha api_key absent: {ok_nokey}; "
        f"alpha profile .env value absent: {ok_env}")
    if not (ok_shared and ok_nokey and ok_env):
        violations.append(f"[{backend} profile] shared={ok_shared} nokey={ok_nokey} env={ok_env}")


def scenario_rules(backend: str) -> None:
    log(f"=== [{backend}] scenario: rules.yaml (local narrowing, shared extension) ===")
    # beta narrows skills/skill-b to local and extends with notebooks/.
    # rules.yaml is per-device policy and NEVER travels in a bundle (R2):
    # alpha must not receive it, and the narrowed path stays pinned in the
    # signed head as carry-forward fleet state (round-4 h2).
    rules = HOMES["beta"] / "roam" / "rules.yaml"
    rules.parent.mkdir(parents=True, exist_ok=True)
    rules.write_text("local:\n  - skills/skill-b/**\nshared:\n  - notebooks/**\n")
    proc = push("beta")
    log(f"  beta push (rules.yaml stays local; the narrowed path is carried "
         f"forward) rc={proc.returncode}: {out(proc)}")
    proc = pull_apply("alpha")
    log(f"  alpha pull --apply rc={proc.returncode}: {out(proc)}")
    # alpha did NOT receive beta's rules.yaml; its own rules are unchanged
    alpha_rules = HOMES["alpha"] / "roam" / "rules.yaml"
    log(f"  alpha received beta's rules.yaml: {alpha_rules.exists()} (expect False)")
    proc = push("alpha")
    log(f"  alpha push rc={proc.returncode}: {out(proc)}")
    proc = pull_apply("beta")
    log(f"  beta pull --apply rc={proc.returncode}: {out(proc)}")
    a_skill_b = (HOMES["alpha"] / "skills" / "skill-b" / "SKILL.md")
    b_skill_b = (HOMES["beta"] / "skills" / "skill-b" / "SKILL.md")
    log(f"  alpha skills/skill-b kept: {a_skill_b.exists()} (expect True: the last "
         f"synced fleet version); beta skills/skill-b kept local: {b_skill_b.exists()} "
         f"(expect True: narrowed by beta's own rules)")
    if alpha_rules.exists():
        violations.append(f"[{backend} rules] beta's rules.yaml travelled to alpha")
    if not (a_skill_b.exists() and b_skill_b.exists()):
        violations.append(f"[{backend} rules] skill-b survived={a_skill_b.exists()}/"
                         f"{b_skill_b.exists()}")
    for d in HOMES:
        observe(d, home_map(d))
    check_invariants(backend, 99)


def scenario_pull_idempotence(backend: str) -> None:
    log(f"=== [{backend}] scenario: pull --apply on an already-converged fleet ===")
    proc = cli("beta", "diff")
    log(f"  beta diff: {out(proc)}")
    proc = pull_apply("beta")
    log(f"  beta pull --apply rc={proc.returncode}: {out(proc)}")
    backups = HOMES["beta"] / "roam" / "backups"
    n = len(list(backups.iterdir())) if backups.is_dir() else 0
    log(f"  beta backup dirs after the no-op pull: {n}")


# ------------------------------------------------------------------ backends

def setup_git_backend() -> None:
    log("=== switching both homes to the git backend (local bare repo) ===")
    if not STORE_GIT.exists():
        subprocess.run(["git", "init", "--bare", "-q", str(STORE_GIT)], check=True,
                       capture_output=True, timeout=120)
        log(f"  created bare repo {STORE_GIT}")
    for device in HOMES:
        state = HOMES[device] / "roam" / "state.json"
        if state.exists():
            state.unlink()
        shutil.rmtree(HOMES[device] / "roam" / "git-mirror", ignore_errors=True)
        env = {**os.environ, "HERMES_HOME": str(HOMES[device])}
        for args in (
            ["config", "set", "plugins.entries.hermes-roam.settings.backend", "git"],
            ["config", "set", "plugins.entries.hermes-roam.settings.git_remote", str(STORE_GIT)],
            ["config", "set", "plugins.entries.hermes-roam.settings.git_branch", "roam"],
        ):
            proc = subprocess.run([HERMES, *args], cwd=str(WORK), env=env,
                                 capture_output=True, text=True, timeout=120)
            log(f"  {device} {' '.join(args[3:])}: rc={proc.returncode}")
    proc = cli("alpha", "init", "--create-remote")
    log(f"  alpha init --create-remote rc={proc.returncode}: {out(proc)}")
    proc = cli("beta", "init")
    log(f"  beta init rc={proc.returncode}: {out(proc)}")


def main() -> None:
    log_path = Path(sys.argv[1] if len(sys.argv) > 1 else "review2_s3_log.md")
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 20261106
    rng = random.Random(seed)
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    log(f"# review2 s3 property harness — seed {seed} — started {started}")
    for backend in ("local", "git"):
        if backend == "git":
            setup_git_backend()
        for round_no in range(1, 11):
            random_round(backend, round_no, rng)
        scenario_sessions(backend)
        scenario_profile_secrets(backend)
        scenario_rules(backend)
        scenario_pull_idempotence(backend)
    check_convergence(99, "git", record=True)
    log("")
    log(f"## verdict (seed {seed})")
    log(f"P1 violations: {len(violations)}")
    for v in violations:
        log(f"- {v}")
    log(f"P2 convergence failures: {len(convergence_failures)}")
    for c in convergence_failures:
        log(f"- {c}")
    log(f"finished {time.strftime('%Y-%m-%d %H:%M:%S')} (started {started})")
    log_path.write_text("\n".join(log_lines) + "\n")
    print(f"\nlog written to {log_path}")
    print(f"P1 violations: {len(violations)}; P2 convergence failures: "
          f"{len(convergence_failures)}")


if __name__ == "__main__":
    main()
