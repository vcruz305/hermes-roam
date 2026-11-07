"""Scope rules: path classification, host-scoped config merging, plugin inventory.

Classification uses gitignore-style patterns (fnmatch per path segment,
``**`` spanning segments, trailing ``/`` marking directories). The ordered
rule list is first-match-wins; user rules from ``<home>/roam/rules.yaml``
precede the defaults. That file is per-device policy (R2): it never
travels in a bundle, so every machine's rules stay its own.

The defaults are an ALLOWLIST: only the explicitly shared paths (SOUL,
memories, skills, skins, config, hermes-pack, and the same inside
``profiles/*/``) ever travel. Any path no rule matches is LOCAL, so an
unknown file in a real home can never leak into a bundle.

The rule list is PRECOMPIDLED once per rule set (see ``_Classifier``):
anchored rules are dispatched by their first segment, per-segment and
per-directory results are memoized, and ``walk_home`` prunes whole
directory trees that no included-scope rule can ever match. The fnmatch
call count is linear in the number of distinct names and directories, not
paths x rules x segments.

config.yaml merging: host keys, home-directory paths, and secrets are
host-scoped. A mapping key whose name looks like a secret (key names are
normalised — lowercase, ``-`` -> ``_`` — before pattern matching, and any
name ending in ``_key`` is a secret unless allowlisted), or a string value
that looks like a known credential, never travels; a list that contains
any such value (or any home path) is host-scoped as a whole — the simpler
and safer rule — and the local list always wins on merge. These are the
only content inspections, and values are never logged.
"""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import ConfigError
from .model import Scope

# Ordered default rules: first match wins. This is an ALLOWLIST: any path no
# rule matches is LOCAL and never leaves the machine. User rules from
# <home>/roam/rules.yaml precede these — `shared:` entries extend the
# allowlist, `local:` / `ignored:` narrow it further.
DEFAULT_RULES: tuple[tuple[str, Scope], ...] = (
    # Per-machine state and secrets: always LOCAL, at any depth.
    ("auth.lock", Scope.LOCAL),
    ("*.lock", Scope.LOCAL),
    ("*.tmp", Scope.LOCAL),
    (".env", Scope.LOCAL),
    ("auth.json", Scope.LOCAL),
    ("auth.json.*", Scope.LOCAL),
    ("state.db", Scope.LOCAL),
    ("state.db-*", Scope.LOCAL),
    ("deliveries.db*", Scope.LOCAL),
    ("executions.db*", Scope.LOCAL),
    ("gateway/", Scope.LOCAL),
    ("gateway-service/", Scope.LOCAL),
    ("bin/", Scope.LOCAL),
    ("hermes-agent/", Scope.LOCAL),
    ("plugin-data/", Scope.LOCAL),
    ("desktop/", Scope.LOCAL),
    ("profiles/*/home/", Scope.LOCAL),
    # roam/rules.yaml is per-device scope policy (R2): it is loaded from this
    # machine's home only, never synced, and a user shared: entry cannot
    # untag it (the roam/ contract below pins the whole tree LOCAL).
    ("roam/rules.yaml", Scope.LOCAL),
    ("profiles/*/roam/rules.yaml", Scope.LOCAL),
    ("context_length_cache.yaml*", Scope.LOCAL),
    ("channel_directory.json", Scope.LOCAL),
    ("*.bak*", Scope.LOCAL),
    ("*.before-*", Scope.LOCAL),
    ("*.pid", Scope.LOCAL),
    ("node_modules/", Scope.LOCAL),
    # Hermes runtime tool downloads (Node, browsers, toolchains): machine-specific,
    # hundreds of MB, reproducible via `hermes update`/installers – never travels.
    ("installs/", Scope.LOCAL),
    # Hermes-managed state directly inside the shared skills/ tree — the hub's
    # own scanners exclude these names too; the lockfile tree must never move
    # between machines. Never even listed.
    ("skills/.*", Scope.IGNORED),
    # Caches and machine-local scratch: never even listed, at any depth.
    ("cache/", Scope.IGNORED),
    ("audio_cache/", Scope.IGNORED),
    ("image_cache/", Scope.IGNORED),
    ("logs/", Scope.IGNORED),
    ("backups/", Scope.IGNORED),
    ("bootstrap-cache/", Scope.IGNORED),
    ("checkpoints/", Scope.IGNORED),
    ("sandboxes/", Scope.IGNORED),
    ("state-snapshots/", Scope.IGNORED),
    ("tmp/", Scope.IGNORED),
    ("__pycache__/", Scope.IGNORED),
    (".git/", Scope.IGNORED),
    ("venv/", Scope.IGNORED),
    (".venv/", Scope.IGNORED),
    ("reports/", Scope.IGNORED),
    ("profile-archives/", Scope.IGNORED),
    # Plugin code is a git checkout whose provenance Hermes owns; it never
    # travels, at any depth. This rule must sit ABOVE the shared content
    # rules: they are unanchored, so a later rule would never win for e.g.
    # plugins/<slug>/skills/** and the plugin's own bundled skills would sync.
    # (The plugin's own .git/ dirs stay IGNORED via the rule above.) Only
    # roam/installed-plugins.json (names + pinned sha) is synced.
    ("plugins/**", Scope.LOCAL),
    # 0.4.1: the read-only session archive namespace. These paths exist
    # only inside bundles (a pushing device exports them with Hermes's own
    # `hermes sessions export`); no home path ever classifies here and the
    # receiver installs them read-only under <home>/roam/session-archive/
    # (LOCAL). Reading requires no opt-in; publishing does (sync_sessions).
    # This rule MUST sit above the raw sessions/ rule: `sessions/` is a
    # single-segment pattern that matches any `sessions` path segment, so a
    # lower position would swallow the archive namespace whole.
    ("archive/sessions/**", Scope.SESSIONS),
    # 0.4.1: the raw sessions folder is ALWAYS LOCAL. In Hermes 0.22.5 it is
    # agent.logs_dir — redacted request_dump_*.json debug files and rare
    # fallback jsonl shards — never real transcript history (that lives in
    # the LOCAL state.db). Request dumps must never travel; the opt-in
    # session archive is published from Hermes's own `hermes sessions
    # export` under the bundle-only archive/sessions/ namespace instead.
    ("sessions/", Scope.LOCAL),
    ("profiles/*/sessions/", Scope.LOCAL),
    # Opt-in cron: JOB DEFINITIONS only. In Hermes 0.22.5 <home>/cron/ holds
    # jobs.json (the definitions) plus run output under cron/output/, lock
    # files, and telemetry stamps — only jobs.json may ever travel; run
    # output and bookkeeping are machine-local.
    ("cron/jobs.json", Scope.CRON),
    ("profiles/*/cron/jobs.json", Scope.CRON),
    ("cron/output/", Scope.IGNORED),
    ("profiles/*/cron/output/", Scope.IGNORED),
    ("cron/**", Scope.LOCAL),
    ("profiles/*/cron/**", Scope.LOCAL),
    # The allowlist: only these paths ever travel, top level and per profile.
    # roam/rules.yaml is NOT here: it is per-device policy and always LOCAL
    # (R2) — a device's rules never move to another machine.
    # profile.yaml is Hermes's named-profile metadata (description, display
    # name, bot roster title, rename history): non-secret, and one of the
    # profile IDENTITY markers. A root home has none; the rule exists so the
    # re-application inside profiles/<name>/ classifies the real file SHARED
    # (see classify_path), because without it (and without any other marker)
    # a pulled profile is not even listable.
    ("config.yaml", Scope.SHARED),
    ("SOUL.md", Scope.SHARED),
    ("profile.yaml", Scope.SHARED),
    ("hermes-pack.yaml", Scope.SHARED),
    ("memories/", Scope.SHARED),
    ("skills/", Scope.SHARED),
    ("skins/", Scope.SHARED),
    ("profiles/*/config.yaml", Scope.SHARED),
    ("profiles/*/SOUL.md", Scope.SHARED),
    # 0.4.2: Hermes keeps per-profile metadata (description, display name,
    # bot roster title, rename history) in profiles/<name>/profile.yaml —
    # non-secret, and one of Hermes's profile IDENTITY markers: without it
    # (and without any other marker) a pulled profile is not even listable.
    ("profiles/*/profile.yaml", Scope.SHARED),
    ("profiles/*/hermes-pack.yaml", Scope.SHARED),
    ("profiles/*/memories/", Scope.SHARED),
    ("profiles/*/skills/", Scope.SHARED),
    ("profiles/*/skins/", Scope.SHARED),
)

# Dotted config.yaml key paths that are per-device. A non-wildcard key covers
# its whole subtree; a key with a ``*`` segment covers exactly that depth.
HOST_CONFIG_KEYS: tuple[str, ...] = (
    "terminal",
    "terminal.*",
    "paths",
    "paths.*",
    "display.interface",
    "browser.executable",
    "mcp_servers.*.command",
    "plugins.entries.hermes-roam",
)

# Mapping key names (any depth) that always hold a secret. Key names are
# NORMALISED before matching (lowercase, '-' -> '_'), so dash forms like
# `api-key` match their underscore twins (R12). A ``*_``-prefixed entry
# matches any key ending with the suffix; every other entry matches exactly.
# Keys that end in exactly '_key' are secrets unless allowlisted below.
# Keys that match never travel and always stay local.
DEFAULT_SECRET_KEY_PATTERNS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "*_api_key",
    "token",
    "*_token",
    "access_token",
    "refresh_token",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
    "secret_access_key",
    "access_key_id",
    "secret",
    "*_secret",
    "client_secret",
    "client_id_secret",
    "credentials",
    "*_credentials",
    "password",
    "*_password",
    "passwd",
    "private_key",
    "bearer",
    "authorization",
    "cookie",
)

# Key names that end in exactly '_key' but are known non-secret settings and
# must keep syncing (R12 allowlist). Checked on the normalised name.
NON_SECRET_KEY_NAMES: frozenset[str] = frozenset(
    {
        "cache_key",
        "sort_key",
        "primary_key",
        "key_strategy",
        "cache_key_strategy",
        "hotkey",
        "keybinding",
    }
)

# String values that look like known credentials, wherever they appear
# (lists included). Anything matching is stripped on push and kept local.
_SECRET_VALUE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"^ghp_[A-Za-z0-9]{8,}"),
    re.compile(r"^github_pat_[A-Za-z0-9_]{8,}"),
    re.compile(r"^xox[bp]-[A-Za-z0-9-]{8,}"),
    re.compile(r"^AKIA[0-9A-Z]{16}$"),
    re.compile(r"^hf_[A-Za-z0-9]{30,}"),
    re.compile(r"^Bearer\s+\S"),
)

_RULE_KEYS: dict[str, Scope] = {
    "shared": Scope.SHARED,
    "local": Scope.LOCAL,
    "ignored": Scope.IGNORED,
}

# Absolute paths under a user home directory — the directory itself
# (/root, /home/u, /Users/u, C:\Users\u) or anything below it. Such values
# are per-device and never travel in a bundle.
_HOME_PATH_RE = re.compile(
    r"^(?:/root|/home/[^/]+|/Users/[^/]+|[A-Za-z]:[\\/](?:Users|home)[\\/][^\\/]+)"
    r"(?:[/\\]|$)"
)

_MISSING = object()


@dataclass(frozen=True, slots=True)
class Rules:
    """Ordered (pattern, scope) rules plus the merged host key list.

    ``device_rules`` are the GENERATED local rules (hub-lock and pristine
    bundled-skill trees): they are derived from this device's own Hermes
    state, not written by the user, and surfaces must label them as such.
    """

    rules: tuple[tuple[str, Scope], ...] = DEFAULT_RULES
    host_keys: tuple[str, ...] = HOST_CONFIG_KEYS
    device_rules: tuple[tuple[str, Scope], ...] = ()

    @classmethod
    def default(cls) -> Rules:
        return cls()


def _hub_lock_rules(home: Path) -> tuple[tuple[str, Scope], ...]:
    """LOCAL rules for skill trees the Hermes hub owns.

    Hermes records hub installs in ``<home>/skills/.hub/lock.json`` — the same
    marker its own code reads — with one ``install_path`` per install. Hub
    installs are reproducible from the hub, so their trees never travel; a
    missing or unreadable lock contributes nothing (the static ``skills/.*``
    rule already keeps the lock tree itself out of every bundle), and a
    traversal-shaped entry is ignored, never turned into a rule. Per-profile
    hub trees are covered by re-applying these rules inside ``profiles/<name>/``.
    """
    lock = Path(home) / "skills" / ".hub" / "lock.json"
    try:
        data = json.loads(lock.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ()
    installed = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(installed, dict):
        return ()
    rules: list[tuple[str, Scope]] = []
    for entry in installed.values():
        rel = entry.get("install_path") if isinstance(entry, dict) else None
        if not isinstance(rel, str) or not rel:
            continue
        parts = rel.split("/")
        if rel.startswith("/") or "\\" in rel or ".." in parts or any(not p for p in parts):
            continue
        rules.append((f"skills/{rel}/", Scope.LOCAL))
    return tuple(rules)


# ------------------------------------------------- bundled-skill provenance

# Everything in this block replicates Hermes's own bundled-skill bookkeeping
# from the READ-ONLY Hermes source (never imported — see docs/DESIGN.md):
# the v2 manifest format is tools/skills_sync.py:113-128 (_read_manifest,
# ``name:hash`` lines), the nested category layout is tools/skills_sync.py:
# 170-184 (_discover_bundled_skills + _compute_relative_dest:
# bundled/<category>/<skill> -> skills/<category>/<skill>), the origin
# hash is tools/skills_sync.py:187-199 (dir_hash: MD5 over the sorted
# package — relative POSIX path then file bytes — with generated runtime
# caches excluded), the runtime-cache set is tools/skills_sync_optional.py:
# 43-58 (_RUNTIME_CACHE_DIRS / _is_runtime_cache), the scanner exclusions
# are agent/skill_utils.py:24-52 (EXCLUDED_SKILL_DIRS /
# SKILL_SUPPORT_DIRS), and the skill name is tools/skill_usage.py:246-257
# (read_skill_name: frontmatter ``name:`` first, directory name fallback).

_BUNDLED_MANIFEST_NAME = ".bundled_manifest"
_BUNDLED_DESCRIPTION_NAME = "DESCRIPTION.md"

# agent/skill_utils.py:24-32 — dirs no skill scanner ever enters.
_SCANNER_EXCLUDED_DIRS = frozenset((
    ".git", ".github", ".hub", ".archive", ".curator_backups", ".locks",
    ".venv", "venv", "node_modules", "site-packages", "__pycache__",
    ".tox", ".nox", ".pytest_cache", ".mypy_cache", ".ruff_cache",
))
# agent/skill_utils.py:32 — support dirs inside a skill package.
_SKILL_SUPPORT_DIRS = frozenset(("references", "templates", "assets", "scripts"))
# tools/skills_sync_optional.py:43 — generated runtime state only.
_BUNDLED_CACHE_DIRS = frozenset((
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
))


def _bundled_is_runtime_cache(path: Path, relative: Path) -> bool:
    """tools/skills_sync_optional.py:46-58 _is_runtime_cache, verbatim."""
    parts = relative.parts
    if any(part in _BUNDLED_CACHE_DIRS for part in parts[:-1]):
        return True
    if path.name in _BUNDLED_CACHE_DIRS and path.is_dir():
        return True
    return path.suffix in {".pyc", ".pyo"} and path.with_suffix(".py").is_file()


def _bundled_dir_hash(directory: Path) -> str:
    """tools/skills_sync.py:187-199 dir_hash: MD5 of the package's sorted
    file paths and contents, runtime caches excluded. os.walk with
    followlinks=False matches rglob's never-follow-symlinked-dirs behavior,
    and sorting the absolute paths matches sorted(rglob(...))'s ordering."""
    paths: list[Path] = []
    for root, _dirs, files in os.walk(directory, topdown=True, followlinks=False):
        paths.extend(Path(root) / name for name in files)
    hasher = hashlib.md5()
    for fpath in sorted(paths, key=str):
        relative = fpath.relative_to(directory)
        if _bundled_is_runtime_cache(fpath, relative):
            continue
        try:
            if not fpath.is_file():
                continue
            hasher.update(str(relative).encode("utf-8"))
            hasher.update(fpath.read_bytes())
        except OSError:
            continue  # tools/skills_sync.py:194 suppress(OSError) semantics
    return hasher.hexdigest()


def _skill_md_name(skill_md: Path, fallback: str) -> str:
    """tools/skill_usage.py:246-257 read_skill_name: the frontmatter
    ``name:`` field of a SKILL.md (first 4000 chars), else the fallback."""
    try:
        lines = [
            line.strip()
            for line in skill_md.read_text(encoding="utf-8-sig", errors="replace")[:4000].split("\n")
        ]
    except OSError:
        return fallback
    if "---" not in lines:
        return fallback
    block = lines[lines.index("---") + 1:]
    block = block[:block.index("---")] if "---" in block else block
    for line in block:
        if line.startswith("name:"):
            value = line.split(":", 1)[1].strip().strip("\"'")
            if value:
                return value
    return fallback


def _read_bundled_manifest(skills_dir: Path) -> dict[str, str]:
    """tools/skills_sync.py:113-128 _read_manifest: ``{name: origin_hash}``;
    v1 plain-name lines carry an empty hash (nothing can be proven)."""
    try:
        result: dict[str, str] = {}
        for line in (skills_dir / _BUNDLED_MANIFEST_NAME).read_text(
            encoding="utf-8-sig"
        ).splitlines():
            line = line.strip()
            if not line:
                continue
            if ":" in line:
                name, _, hash_value = line.partition(":")
                result[name.strip()] = hash_value.strip()
            else:
                result[line] = ""
        return result
    except OSError:
        return {}


def _iter_bundled_skill_mds(skills_dir: Path):
    """Every candidate skill root under one skills/ tree, applying Hermes's
    own scanner exclusions (agent/skill_utils.py:34-52): excluded dirs are
    pruned, support packages inside a skill root are skipped, and symlinks
    are never followed."""
    for root, dirs, files in os.walk(skills_dir, topdown=True, followlinks=False):
        root_path = Path(root)
        dirs[:] = sorted(
            d for d in dirs
            if d not in _SCANNER_EXCLUDED_DIRS and not (root_path / d).is_symlink()
        )
        if "SKILL.md" not in files:
            continue
        skill_md = root_path / "SKILL.md"
        rel = skill_md.relative_to(skills_dir)
        if any(part in _SCANNER_EXCLUDED_DIRS for part in rel.parts):
            continue
        # support packages (references/…, scripts/…) inside a skill root
        # are never standalone skills: agent/skill_utils.py:41-52.
        excluded = False
        for index, part in enumerate(rel.parts[:-1]):
            if index > 0 and part in _SKILL_SUPPORT_DIRS and (
                skills_dir.joinpath(*rel.parts[:index], "SKILL.md").is_file()
            ):
                excluded = True
                break
        if not excluded:
            yield skill_md


def _bundled_skill_rules(home: Path) -> tuple[tuple[str, Scope], ...]:
    """LOCAL rules for the pristine Hermes bundled-skill trees of this
    device (root home and every profiles/<name>/ alike).

    Hermes seeds its bundled skills into every home's skills/ tree —
    nested category paths included — and records each skill's origin hash
    in skills/.bundled_manifest. A tree whose current content hash still
    equals that origin hash is an UNMODIFIED Hermes copy: each device's
    own Hermes ships (and updates) it, so it must never travel — a
    foreign copy would overwrite the receiver's and skills_sync would
    then see a hash mismatch and freeze the receiver's bundled updates
    forever. A tree whose hash DIFFERS is the user's own work on top of a
    bundled skill: no rule is generated and it syncs like any user skill
    (the receiving Hermes treats it as user-modified there, which is
    exactly the right verdict). A manifest entry without a hash (v1 line),
    a missing manifest, or a tree the user deleted contributes nothing;
    a user shared: rule in roam/rules.yaml still wins over these rules.

    The category DESCRIPTION.md files Hermes seeds beside its bundled
    skills are Hermes-shipped too and never travel.
    """
    home = Path(home)
    trees = ["skills"]
    profiles = home / "profiles"
    if profiles.is_dir():
        trees.extend(
            f"profiles/{child.name}/skills"
            for child in sorted(profiles.iterdir(), key=lambda item: item.name)
            if child.is_dir() and child.name not in ("", ".", "..")
        )
    rules: list[tuple[str, Scope]] = []
    for prefix in trees:
        skills_dir = home / prefix
        manifest = _read_bundled_manifest(skills_dir)
        if not manifest:
            continue
        bundled_categories: set[str] = set()
        for skill_md in _iter_bundled_skill_mds(skills_dir):
            skill_dir = skill_md.parent
            names = {skill_dir.name, _skill_md_name(skill_md, skill_dir.name)}
            origin = next((manifest[name] for name in names if name in manifest), None)
            if origin is None:
                continue  # not Hermes-shipped: the default rules apply
            rel = skill_dir.relative_to(home).as_posix()
            if len(rel.split("/")) < 2 or rel.startswith("../"):
                continue  # never a blanket skills/ rule
            bundled_categories.add(skill_dir.parent.relative_to(home).as_posix())
            if origin and _bundled_dir_hash(skill_dir) == origin:
                rules.append((f"{rel}/", Scope.LOCAL))
        for category_rel in sorted(bundled_categories):
            if (home / category_rel / _BUNDLED_DESCRIPTION_NAME).is_file():
                rules.append((f"{category_rel}/{_BUNDLED_DESCRIPTION_NAME}", Scope.LOCAL))
    return tuple(rules)


def load_rules(home: Path) -> Rules:
    """Load rules from {home}/roam/rules.yaml; user rules take precedence.

    The file is per-device policy (R2): it is read from this machine's home
    only and never travels in a bundle, so each device's rules stay their
    own.

    The effective list is user rules (they extend the allowlist), then the
    hub-lock rules for skills Hermes installed from the hub, then the
    bundled-provenance rules for this device's pristine Hermes bundled
    skills, then the defaults. The generated (hub + bundled) rules are
    also returned as ``Rules.device_rules`` so surfaces can label them as
    device-generated instead of user policy.
    """
    hub = _hub_lock_rules(home)
    bundled = _bundled_skill_rules(home)
    generated = hub + bundled
    path = Path(home) / "roam" / "rules.yaml"
    if not path.is_file():
        return Rules(rules=generated + DEFAULT_RULES, device_rules=generated)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"roam/rules.yaml is not readable YAML: {exc}") from exc
    if data is None:
        return Rules(rules=generated + DEFAULT_RULES, device_rules=generated)
    if not isinstance(data, dict):
        raise ConfigError("roam/rules.yaml must be a mapping of shared/local/ignored/host_keys")
    user: list[tuple[str, Scope]] = []
    for key, scope_value in _RULE_KEYS.items():
        patterns = data.get(key, [])
        if not isinstance(patterns, list) or not all(
            isinstance(pattern, str) and pattern for pattern in patterns
        ):
            raise ConfigError(f"roam/rules.yaml: '{key}' must be a list of pattern strings")
        user.extend((pattern, scope_value) for pattern in patterns)
    host_keys_raw = data.get("host_keys", [])
    if not isinstance(host_keys_raw, list) or not all(
        isinstance(host_key, str) and host_key for host_key in host_keys_raw
    ):
        raise ConfigError("roam/rules.yaml: 'host_keys' must be a list of dotted key strings")
    unknown = sorted(set(data) - set(_RULE_KEYS) - {"host_keys"})
    if unknown:
        raise ConfigError(f"roam/rules.yaml: unknown key '{unknown[0]}'")
    host_keys = tuple(dict.fromkeys((*host_keys_raw, *HOST_CONFIG_KEYS)))
    return Rules(
        rules=tuple(user) + generated + DEFAULT_RULES,
        host_keys=host_keys,
        device_rules=generated,
    )


# ------------------------------------------------------------------ classifier


def _match_segments(pattern: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    """gitignore-style match where '**' spans segments and '*' stays in one."""
    if not pattern:
        return not parts
    head = pattern[0]
    if head == "**":
        return any(_match_segments(pattern[1:], parts[index:]) for index in range(len(parts) + 1))
    if not parts:
        return False
    return fnmatch.fnmatchcase(parts[0], head) and _match_segments(pattern[1:], parts[1:])


_WILDCARD_CHARS = frozenset("*?[")
_MEMO_CAP = 200_000
_CLASSIFIER_CAP = 32


def _is_wildcard(segment: str) -> bool:
    return any(char in _WILDCARD_CHARS for char in segment)


@dataclass(frozen=True, slots=True)
class _Rule:
    index: int
    segments: tuple[str, ...]
    dir_only: bool
    anchored: bool
    scope: Scope


class _Classifier:
    """Precompiled rule list with exact first-match-wins semantics.

    Shortcuts, all proven equal to matching every rule against every path
    prefix (see ``_pattern_hits`` in git history for the reference shape):

    - an anchored rule (contains ``/``) can only match a prefix of the path,
      so a literal first segment that differs from the path's first segment
      rules the rule out with no fnmatch call (bucket dispatch); anchored
      rules whose first segment contains wildcards stay per-path candidates;
    - a non-anchored rule is a single-segment pattern, so it matches a path
      iff it matches one of the path's segments — checked per segment, with
      the per-segment result memoized by name;
    - matches on proper prefixes (ancestor directories) depend only on the
      directory, so they are memoized per directory and shared by every path
      below it;
    - full-path matches are memoized per (directory, name).

    The memos make the fnmatch call count linear in the number of distinct
    names and directories walked; nothing about the verdicts changes.
    """

    __slots__ = (
        "entries",
        "_rules",
        "_na",
        "_na_always",
        "_na_pref",
        "_a_always",
        "_a_buckets",
        "_matchall",
        "_na_memo",
        "_cand_memo",
        "_dir_memo",
        "_full_memo",
        "_prune_memo",
    )

    def __init__(self, entries: tuple[tuple[str, Scope], ...]) -> None:
        self.entries = entries
        rules: list[_Rule] = []
        for index, (pattern, scope_value) in enumerate(entries):
            stripped = pattern.rstrip("/")
            dir_only = stripped != pattern
            segments = tuple(stripped.split("/")) if stripped else ()
            rules.append(_Rule(index, segments, dir_only, "/" in stripped, scope_value))
        self._rules = tuple(rules)
        self._na = tuple(rule for rule in rules if not rule.anchored and rule.segments != ("**",))
        # Non-anchored patterns whose first characters are literal can only
        # ever match names starting with that literal prefix, so they are
        # bucketed by prefix and only fnmatch-tested when the prefix matches;
        # the rest (leading wildcard) must always be tested. The bucketed
        # test set is the complete one: a pattern whose prefix does not
        # match cannot match the name either.
        always: list[_Rule] = []
        pref: dict[str, list[_Rule]] = {}
        for rule in self._na:
            prefix = rule.segments[0].split("*")[0].split("?")[0].split("[")[0]
            if prefix:
                pref.setdefault(prefix, []).append(rule)
            else:
                always.append(rule)
        self._na_always = tuple(always)
        self._na_pref = {key: tuple(value) for key, value in pref.items()}
        self._a_always = tuple(
            rule for rule in rules if rule.anchored and _is_wildcard(rule.segments[0])
        )
        buckets: dict[str, list[_Rule]] = {}
        for rule in rules:
            if rule.anchored and not _is_wildcard(rule.segments[0]):
                buckets.setdefault(rule.segments[0], []).append(rule)
        self._a_buckets = {key: tuple(value) for key, value in buckets.items()}
        # A bare '**' segment spans segments, so it matches every nonempty
        # path (as a dir when dir-only); it must never be dropped.
        self._matchall = tuple(rule for rule in rules if rule.segments == ("**",))
        self._na_memo: dict[tuple[str, bool], frozenset[int]] = {}
        self._cand_memo: dict[str, tuple[_Rule, ...]] = {}
        self._dir_memo: dict[tuple[str, ...], frozenset[int]] = {}
        self._full_memo: dict[tuple, frozenset[int]] = {}
        self._prune_memo: dict[tuple, bool] = {}

    # -- hit computation -----------------------------------------------------

    def _na_hits(self, name: str, is_dir: bool) -> frozenset[int]:
        key = (name, is_dir)
        memo = self._na_memo
        if key in memo:
            return memo[key]
        hits = set()
        for rule in self._na_always:
            if (not rule.dir_only or is_dir) and fnmatch.fnmatchcase(name, rule.segments[0]):
                hits.add(rule.index)
        for prefix, rules in self._na_pref.items():
            if not name.startswith(prefix):
                continue
            for rule in rules:
                if (not rule.dir_only or is_dir) and fnmatch.fnmatchcase(name, rule.segments[0]):
                    hits.add(rule.index)
        result = frozenset(hits)
        if len(memo) < _MEMO_CAP:
            memo[key] = result
        return result

    def _candidates(self, first: str) -> tuple[_Rule, ...]:
        memo = self._cand_memo
        if first in memo:
            return memo[first]
        candidates = self._a_always + self._a_buckets.get(first, ())
        if len(memo) < _MEMO_CAP:
            memo[first] = candidates
        return candidates

    def _full_hits(self, parts: tuple[str, ...], is_dir: bool) -> frozenset[int]:
        key = (*parts[:-1], parts[-1], is_dir)
        memo = self._full_memo
        if key in memo:
            return memo[key]
        hits = set(self._na_hits(parts[-1], is_dir))
        for rule in self._matchall:
            if not rule.dir_only or is_dir:
                hits.add(rule.index)
        for rule in self._candidates(parts[0]):
            if rule.dir_only and not is_dir:
                continue
            if _match_segments(rule.segments, parts):
                hits.add(rule.index)
        result = frozenset(hits)
        if len(memo) < _MEMO_CAP:
            memo[key] = result
        return result

    def _prefix_hits(self, prefix: tuple[str, ...]) -> frozenset[int]:
        if not prefix:
            return frozenset()
        memo = self._dir_memo
        if prefix in memo:
            return memo[prefix]
        hits = self._prefix_hits(prefix[:-1]) | self._full_hits(prefix, True)
        if len(memo) < _MEMO_CAP:
            memo[prefix] = hits
        return hits

    def hits(self, parts: tuple[str, ...], is_dir: bool) -> frozenset[int]:
        if not parts:
            return frozenset()
        return self._prefix_hits(parts[:-1]) | self._full_hits(parts, is_dir)

    def scope_for(self, parts: tuple[str, ...], is_dir: bool) -> Scope | None:
        """Scope of the first matching rule, or None when nothing matches."""
        hit = self.hits(parts, is_dir)
        if not hit:
            return None
        return self.entries[min(hit)][1]

    # -- subtree pruning ----------------------------------------------------

    def prunable(self, parts: tuple[str, ...], include: frozenset) -> bool:
        """Whether NO path below the directory ``parts`` can ever be listed
        under ``include``, so a walk may skip the whole subtree.

        Exactness argument: let W be the first rule matching the directory
        itself (the roam contract counts as a virtual rule ordered before
        every other). When W matches the directory, it matches every strict
        descendant too (its match is a path prefix, and prefixes of the
        directory are ancestor directories of anything below it), so every
        descendant's first match is W or something ordered before it. The
        subtree is safe to skip exactly when W's scope is not included and
        no included-scope rule ordered before W can reach a descendant:

        - a non-anchored rule is a single-segment pattern, so it matches a
          descendant name wherever it appears — it always reaches;
        - an anchored rule reaches a descendant only if its first
          min(len(segments), len(parts)) segments match the directory that
          far (a shorter match would have matched the directory itself and
          W would not be first);
        - when nothing matches the directory at all, a descendant can still
          match an included rule (e.g. a file named config.yaml), so only an
          empty include makes such a subtree safe.
        """
        if not parts:
            return False  # the home itself is never pruned
        key = (include, parts)
        memo = self._prune_memo
        if key in memo:
            return memo[key]
        result = self._prune(parts, include)
        if len(memo) < _MEMO_CAP:
            memo[key] = result
        return result

    def _prune(self, parts: tuple[str, ...], include: frozenset) -> bool:
        # The roam contract is ordered before every rule and its scope is
        # LOCAL for the whole tree (R2): no path under roam/ — including
        # roam/rules.yaml — can ever be included, so the entire tree is
        # pruned whenever LOCAL is not part of the include set.
        if parts and parts[0] == "roam":
            return Scope.LOCAL not in include
        if not include:
            return True
        # The walk never traverses a directory whose own classification is
        # IGNORED, whatever the include set is (caches are not even entered).
        if self.scope_for(parts, True) is Scope.IGNORED:
            return True
        # Only ANCESTOR matches decide the remaining verdict: when some rule matches a
        # prefix of the directory, it matches every strict descendant too,
        # so every descendant's first match is that rule or something ordered
        # before it — and whether anything ordered before it can reach a
        # descendant never depends on the directory's own name (non-anchored
        # rules match any name; anchored reach is the depth check below).
        # When no ancestor matches, the subtree is kept regardless of its
        # own name, which is never more permissive than the exact verdict.
        hit = self._prefix_hits(parts[:-1])
        if not hit:
            return False  # nothing matches: descendants may still match
        first = min(hit)
        first_scope = self.entries[first][1]
        if first_scope is Scope.IGNORED:
            return True  # IGNORED trees are never traversed at any include
        if first_scope in include:
            return False
        for rule in self._rules:
            if rule.index >= first or rule.scope not in include:
                continue
            if not rule.anchored or rule.segments == ("**",):
                return False
            depth = min(len(rule.segments), len(parts))
            if all(
                fnmatch.fnmatchcase(parts[j], rule.segments[j]) for j in range(depth)
            ):
                return False
        return True


_CLASSIFIER_LOCK = threading.Lock()
_CLASSIFIERS: dict[tuple[tuple[str, Scope], ...], _Classifier] = {}


def _classifier(rules: Rules) -> _Classifier:
    key = rules.rules
    with _CLASSIFIER_LOCK:
        classifier = _CLASSIFIERS.get(key)
        if classifier is None:
            if len(_CLASSIFIERS) >= _CLASSIFIER_CAP:
                _CLASSIFIERS.clear()
            classifier = _Classifier(key)
            _CLASSIFIERS[key] = classifier
    return classifier


def _decompose(relpath: str) -> tuple[tuple[str, ...], bool]:
    is_dir = relpath.endswith("/")
    parts = tuple(part for part in relpath.split("/") if part)
    return parts, is_dir


def _scope_for_parts(parts: tuple[str, ...], is_dir: bool, rules: Rules) -> Scope:
    # Contract: everything under roam/ is LOCAL — it is per-device state
    # (sync state, backups, journals, mirrors) and, since R2, the per-device
    # scope policy roam/rules.yaml itself never travels either.
    if parts and parts[0] == "roam":
        return Scope.LOCAL
    matched = _classifier(rules).scope_for(parts, is_dir)
    # Allowlist: unknown paths are LOCAL, never SHARED.
    return matched if matched is not None else Scope.LOCAL


def _merge_scopes(outer: Scope, inner: Scope) -> Scope:
    for candidate in (outer, inner):
        if candidate == Scope.IGNORED:
            return Scope.IGNORED
    for candidate in (outer, inner):
        if candidate == Scope.LOCAL:
            return Scope.LOCAL
    for candidate in (outer, inner):
        if candidate in (Scope.SESSIONS, Scope.CRON):
            return candidate
    return Scope.SHARED


def classify_path(relpath: str, rules: Rules) -> Scope:
    """Scope for one POSIX relpath; a trailing '/' marks a directory.

    The same rules are re-applied inside each profiles/<name>/ subtree, so a
    profile's .env is LOCAL and its cache is IGNORED just like at the top.
    """
    parts, is_dir = _decompose(relpath)
    scope_value = _scope_for_parts(parts, is_dir, rules)
    if len(parts) > 2 and parts[0] == "profiles":
        inner = "/".join(parts[2:]) + ("/" if is_dir else "")
        inner_scope = classify_path(inner, rules)
        scope_value = _merge_scopes(scope_value, inner_scope)
    return scope_value


def dir_prunable(relpath: str, rules: Rules, include: set[Scope]) -> bool:
    """Whether the directory at ``relpath`` (trailing '/') heads a tree in
    which no path can ever be listed under ``include``, so a walk may skip it.

    Trees whose first matching rule is LOCAL or IGNORED (and nothing ordered
    before it can reach below with an included scope) are pruned at the
    directory level — never traversed, listed, or opened; directories that
    can contain included paths (e.g. skills/ or profiles/<name>/) are
    always entered. The verdict is exact: a pruned tree is precisely one
    whose every path classifies out of ``include``.
    """
    parts, _ = _decompose(relpath)
    if not parts:
        return False  # the home itself is never pruned
    return _classifier(rules).prunable(parts, frozenset(include))


def is_config_path(relpath: str) -> bool:
    """Whether the relpath is a config.yaml the engine must strip on push and
    merge on pull: the top-level config.yaml and every profiles/<name>/config.yaml."""
    if relpath == "config.yaml":
        return True
    parts = relpath.split("/")
    return (
        len(parts) == 3
        and parts[0] == "profiles"
        and parts[2] == "config.yaml"
        and parts[1] not in ("", ".", "..")
    )


# ------------------------------------------------------------------ config merge


def _parse_config(text: str, source: str) -> dict:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source} config.yaml is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{source} config.yaml must be a YAML mapping")
    return data


def _get_path(data: dict, path: tuple[str, ...]):
    node: dict = data
    for segment in path[:-1]:
        child = node.get(segment)
        if not isinstance(child, dict):
            return _MISSING
        node = child
    if path[-1] not in node:
        return _MISSING
    return node[path[-1]]


def _set_path(data: dict, path: tuple[str, ...], value: object) -> None:
    node: dict = data
    for segment in path[:-1]:
        child = node.get(segment)
        if not isinstance(child, dict):
            child = {}
            node[segment] = child
        node = child
    node[path[-1]] = value


def _remove_path(data: dict, path: tuple[str, ...]) -> None:
    node: dict = data
    for segment in path[:-1]:
        child = node.get(segment)
        if not isinstance(child, dict):
            return
        node = child
    node.pop(path[-1], None)


def _host_key_covers(host_key: str, path: tuple[str, ...]) -> bool:
    segments = tuple(host_key.split("."))
    if any(segment == "*" for segment in segments):
        if len(path) != len(segments):
            return False
        return all(fnmatch.fnmatchcase(part, segment) for part, segment in zip(path, segments, strict=True))
    return len(path) >= len(segments) and tuple(path[: len(segments)]) == segments


def _normalised_key_name(key: str) -> str:
    """R12: lowercase, '-' -> '_', before any pattern matching."""
    return key.lower().replace("-", "_")


def _secret_key_name(key: object) -> bool:
    if not isinstance(key, str):
        return False
    name = _normalised_key_name(key)
    if name in NON_SECRET_KEY_NAMES:
        return False  # allowlisted non-secret *_key settings keep syncing
    for pattern in DEFAULT_SECRET_KEY_PATTERNS:
        if pattern.startswith("*_"):
            if name.endswith(pattern[1:]):
                return True
        elif name == pattern:
            return True
    return name.endswith("_key")


def _credential_value(value: object) -> bool:
    return isinstance(value, str) and any(rx.match(value) for rx in _SECRET_VALUE_RES)


def secret_like(key: object, value: object) -> bool:
    """Whether the mapping entry is a secret: the key name matches
    DEFAULT_SECRET_KEY_PATTERNS or the value looks like a known credential.

    Together with the home-path check this is the only content inspection
    roam ever performs on config values; values are never logged.
    """
    return _secret_key_name(key) or _credential_value(value)


def _is_home_path(value: object) -> bool:
    return isinstance(value, str) and _HOME_PATH_RE.match(value) is not None


def _protected_scalar(value: object) -> bool:
    return _is_home_path(value) or _credential_value(value)


def _prune_hollow(data: dict, removed: set[tuple[str, ...]]) -> None:
    """Drop mappings that became empty because their covered children were removed."""
    for path in sorted(removed, key=len, reverse=True):
        for depth in range(len(path) - 1, 0, -1):
            ancestor = path[:depth]
            node = _get_path(data, ancestor)
            if isinstance(node, dict) and not node:
                _remove_path(data, ancestor)


def _protected_paths(data: dict, host_keys: list[str]) -> set[tuple[str, ...]]:
    """Mapping-key paths that are host-scoped and never travel.

    An entry is protected when its dotted path is covered by a host key,
    when its key name looks like a secret, or when its value contains (at
    any depth, lists included) a home path or a credential-looking string.
    A list that contains any such value is protected as a whole: roam never
    tries to edit list elements in place, so the entire list stays local.
    """
    paths: set[tuple[str, ...]] = set()

    def walk(node: object, path: tuple[str, ...], addressable: bool) -> bool:
        if isinstance(node, dict):
            found = False
            for key, value in node.items():
                child = (*path, key)
                if _host_key_covers_any(host_keys, child) or _secret_key_name(key):
                    if addressable:
                        paths.add(child)
                    found = True
                    continue
                if walk(value, child, addressable):
                    found = True
                    if not isinstance(value, dict) and addressable:
                        # a list (or scalar) hosting protected content is
                        # host-scoped as a whole — nothing inside is editable
                        paths.add(child)
            return found
        if isinstance(node, list):
            return any(walk(item, path, False) for item in node)
        return _protected_scalar(node)

    walk(data, (), True)
    return paths


def _host_key_covers_any(host_keys: list[str], path: tuple[str, ...]) -> bool:
    return any(_host_key_covers(host_key, path) for host_key in host_keys)


def _covered_by(path: tuple[str, ...], protected: set[tuple[str, ...]]) -> bool:
    """Whether the path or any of its ancestors is a protected entry (a
    protected key covers its whole subtree, so descendants are covered too)."""
    return any(path[:depth] in protected for depth in range(1, len(path) + 1))


def _union_missing_shared(local: dict, remote: object, path: tuple[str, ...],
                          protected: set[tuple[str, ...]]) -> int:
    """Add the remote's shared settings the local tree lacks, in place.

    Only paths that are NOT protected travel; every local value that is
    already set wins and is never replaced. Returns the number of remote
    settings added.
    """
    added = 0
    if _covered_by(path, protected):
        return 0
    if isinstance(remote, dict):
        for key, value in remote.items():
            child = (*path, key)
            local_value = local.get(key, _MISSING)
            if _covered_by(child, protected):
                continue
            if isinstance(value, dict):
                if isinstance(local_value, dict):
                    added += _union_missing_shared(local_value, value, child, protected)
                elif local_value is _MISSING:
                    # the whole remote group is absent here: bring over the
                    # shared leaves it still carries, never verbatim
                    holder: dict = {}
                    if _union_missing_shared(holder, value, child, protected):
                        local[key] = holder
                        added += 1
                # a local scalar/list set on the same key wins: nothing is added
                continue
            if local_value is _MISSING:
                local[key] = copy.deepcopy(value)
                added += 1
            # a local value set on the same key wins: never replaced
        return added
    # a remote non-dict at the root cannot be merged into a mapping: nothing to do
    return 0


def _conflicting_shared_keys(local: object, remote: object, path: tuple[str, ...],
                             protected: set[tuple[str, ...]]) -> list[tuple[str, ...]]:
    """Shared setting paths set on BOTH sides with different values.

    Protected paths never count (they are always local by definition); the
    first-pull merge keeps every local value for the paths listed here.
    """
    out: list[tuple[str, ...]] = []
    if _covered_by(path, protected):
        return out
    if isinstance(remote, dict) and isinstance(local, dict):
        for key in sorted(set(remote) | set(local)):
            child = (*path, key)
            if _covered_by(child, protected):
                continue
            remote_value = remote.get(key, _MISSING)
            local_value = local.get(key, _MISSING)
            if isinstance(remote_value, dict) and isinstance(local_value, dict):
                out.extend(_conflicting_shared_keys(local_value, remote_value, child, protected))
            elif remote_value is not _MISSING and local_value is not _MISSING \
                    and remote_value != local_value:
                out.append(child)
        return out
    if local is not _MISSING and remote is not _MISSING and local != remote:
        out.append(path)
    return out


def first_pull_conflict_keys(remote_text: str, local_text: str,
                             host_keys: list[str]) -> list[str]:
    """Dotted shared key paths a first pull keeps local: set on both sides
    with different values and not host-scoped/secret. The first-pull merge
    never replaces these local values; the pull lists them so the user
    knows what stayed and how to publish or overwrite it."""
    remote = _parse_config(remote_text, "remote")
    local = _parse_config(local_text, "local")
    protected = _protected_paths(remote, list(host_keys)) | _protected_paths(local, list(host_keys))
    paths = _conflicting_shared_keys(local, remote, (), protected)
    return [".".join(path) for path in sorted(paths)]


def merge_config(remote_text: str, local_text: str, host_keys: list[str],
                 *, first_pull: bool = False) -> str:
    """Merge a remote config.yaml with this device's host-scoped values.

    Host-scoped paths are those covered by a host key, key names that look
    like secrets, and values that are home paths or credentials (a list
    holding any of these is host-scoped as a whole). Result = remote with
    every host-scoped path replaced by the local value (or removed when
    local lacks it) and every local-only host-scoped value kept, so a
    device's secrets, credentials, paths and host settings never move.

    With ``first_pull`` (a device that never accepted a shared projection
    for this config) the merge is a union with local precedence instead:
    every remote shared setting the local file LACKS is added, and every
    local value set on a key both sides touch wins — a brand-new device
    receives the fleet's shared settings without surrendering anything
    it set itself. Host-scoped and secret paths stay local either way.
    """
    remote = _parse_config(remote_text, "remote")
    local = _parse_config(local_text, "local")
    keys = list(host_keys)
    protected = _protected_paths(remote, keys) | _protected_paths(local, keys)
    if first_pull:
        result = copy.deepcopy(local)
        _union_missing_shared(result, remote, (), protected)
        if not result:
            return ""
        return yaml.safe_dump(result, sort_keys=False)
    result = copy.deepcopy(remote)
    removed: set[tuple[str, ...]] = set()
    for path in protected:
        local_value = _get_path(local, path)
        if local_value is _MISSING:
            _remove_path(result, path)
            removed.add(path)
        else:
            _set_path(result, path, copy.deepcopy(local_value))
    _prune_hollow(result, removed)
    if not result:
        return ""
    return yaml.safe_dump(result, sort_keys=False)


def strip_host_keys(config_text: str, host_keys: list[str]) -> str:
    """Remove host-scoped values for the pushed config.yaml: host keys,
    home paths, secret key names, and credential-looking values — wherever
    they appear, lists included (a list holding any is removed whole)."""
    data = _parse_config(config_text, "local")
    removed: set[tuple[str, ...]] = set()
    for path in _protected_paths(data, list(host_keys)):
        _remove_path(data, path)
        removed.add(path)
    _prune_hollow(data, removed)
    if not data:
        return ""
    return yaml.safe_dump(data, sort_keys=False)


def shared_projection_digest(config_text: str, host_keys: list[str]) -> str:
    """sha256 of the canonical SHARED projection of a config.yaml: the
    strip_host_keys tree serialized canonically (sorted keys, so the digest
    is ORDER-INDEPENDENT — a device whose config carries the same shared
    values in a different key order is the same projection, never a phantom
    change). Stripping an already stripped projection yields the same tree,
    so three-way local vs base vs remote comparisons are exact, and a push
    records the projection it published through this same function.
    """
    stripped = strip_host_keys(config_text, host_keys)
    try:
        tree = yaml.safe_load(stripped) if stripped else {}
    except yaml.YAMLError:
        tree = {}
    if not isinstance(tree, dict):
        tree = {}

    def canonical(value: object) -> object:
        if isinstance(value, dict):
            return {
                str(key): canonical(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, list):
            return [canonical(item) for item in value]
        return value

    return hashlib.sha256(
        json.dumps(
            canonical(tree), sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, default=str,
        ).encode("utf-8")
    ).hexdigest()


# ------------------------------------------------------------------ plugin inventory


def _read_text(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


_HEX40_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _git_head_sha(plugin_dir: Path) -> str:
    """Read the checked-out commit sha from .git files; never run git."""
    git_dir = plugin_dir / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        return ""
    head = _read_text(git_dir / "HEAD").strip()
    if not head:
        return ""
    if head.startswith("ref:"):
        ref = head[4:].strip()
        sha = _read_text(git_dir / ref).strip()
        if _HEX40_RE.fullmatch(sha):
            return sha.lower()
        for line in _read_text(git_dir / "packed-refs").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref and _HEX40_RE.fullmatch(parts[0]):
                return parts[0].lower()
        return ""
    return head.lower() if _HEX40_RE.fullmatch(head) else ""


def _plugin_identity(plugin_dir: Path) -> tuple[str, str]:
    name, version = plugin_dir.name, ""
    manifest = plugin_dir / "plugin.yaml"
    if manifest.is_file():
        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            data = None
        if isinstance(data, dict):
            manifest_name = data.get("name")
            if isinstance(manifest_name, str) and manifest_name:
                name = manifest_name
            if data.get("version") is not None:
                version = str(data["version"])
    return name, version


def generate_installed_plugins(home: Path) -> dict:
    """Map plugin name -> {version, sha} from plugins/*/plugin.yaml and .git files."""
    root = Path(home) / "plugins"
    if not root.is_dir():
        return {}
    result: dict[str, dict[str, str]] = {}
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if child.is_symlink() or not child.is_dir():
            continue
        name, version = _plugin_identity(child)
        result[name] = {"version": version, "sha": _git_head_sha(child)}
    return result
