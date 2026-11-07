"""Bundled-skill provenance (0.5.1): Hermes ships ~58 bundled skills into
every home's skills/ tree (and every profile's), tracking each skill's
origin hash in skills/.bundled_manifest (v2 ``name:hash`` lines). Roam must
never carry a PRISTINE bundled skill: each device's own Hermes ships (and
updates) its copy, and a foreign copy would freeze the receiver's bundled
updates (skills_sync would see a hash mismatch and treat the skill as
user-modified forever). A bundled skill the user MODIFIED (current hash
differs from the manifest origin hash) is the user's work and syncs like
any other user skill; the receiving Hermes treats it as user-modified too,
which is exactly right.

The origin hash and the name->directory mapping are replicated from the
read-only Hermes source (no Hermes import):
- tools/skills_sync.py:113-128 _read_manifest (v2 ``name:hash`` lines),
  170-184 _discover_bundled_skills/_compute_relative_dest (nested category
  paths: bundled/<category>/<skill> -> skills/<category>/<skill>), and
  187-199 dir_hash (MD5 over the sorted package: relative POSIX path then
  file bytes, runtime caches excluded);
- tools/skills_sync_optional.py:43-58 _RUNTIME_CACHE_DIRS/_is_runtime_cache;
- tools/skill_usage.py:246-257 read_skill_name (frontmatter ``name:``
  first, directory name as fallback);
- agent/skill_utils.py:24-32 EXCLUDED_SKILL_DIRS (scanner exclusions).

Written RED against d6d0c2f (0.5.0): every classification assertion here
fails there (bundled skills classify SHARED) and the push test carries the
whole bundled tree.
"""

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import conftest  # noqa: E402
from fakes.fake_backend import FakeBackend  # noqa: E402

conftest.load_plugin_package()
from roam import scope as scope_mod  # noqa: E402
from roam.engine import Engine  # noqa: E402
from roam.model import RoamSettings, Scope  # noqa: E402

KEY = bytes(range(1, 33))
VERSION = "0.22.5"
CATEGORY = "creative"
PRISTINE = "art-skill"
HACKED = "hack-skill"
USER_SKILL = "my-skill"
PROFILE = "work"
PROFILE_SKILL = "work-tool"

# tools/skills_sync_optional.py:43 — the ONLY generated state dir_hash excludes.
RUNTIME_CACHE_DIRS = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)


def hermes_dir_hash(directory: Path) -> str:
    """Fixture-side replica of tools/skills_sync.py:187-199 dir_hash()."""
    hasher = hashlib.md5()

    def is_runtime_cache(path: Path) -> bool:
        relative = path.relative_to(directory)
        if any(part in RUNTIME_CACHE_DIRS for part in relative.parts[:-1]):
            return True
        if path.name in RUNTIME_CACHE_DIRS and path.is_dir():
            return True
        return path.suffix in {".pyc", ".pyo"} and path.with_suffix(".py").is_file()

    for fpath in sorted(directory.rglob("*")):
        if is_runtime_cache(fpath) or not fpath.is_file():
            continue
        hasher.update(str(fpath.relative_to(directory)).encode("utf-8"))
        hasher.update(fpath.read_bytes())
    return hasher.hexdigest()


def skill_md(name: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: bundled {name}\n---\n{body}"


def seed_bundled_tree(base: Path, *, modify: frozenset[str], variant: str = "") -> None:
    """Seed one skills/ tree the way Hermes's skills_sync does: nested
    category paths, a category DESCRIPTION.md, and the v2 origin manifest.
    ``variant`` simulates a different Hermes version's bundled content (a
    device running another release ships different bytes)."""
    skills = base / "skills" / CATEGORY
    skills.mkdir(parents=True, exist_ok=True)
    body = "bundled body\n" if not variant else f"bundled body ({variant})\n"
    (skills / "DESCRIPTION.md").write_text(
        f"Hermes category description {variant}\n".strip() + "\n", encoding="utf-8"
    )
    entries: dict[str, str] = {}
    for name in (PRISTINE, HACKED):
        tree = skills / name
        tree.mkdir(parents=True, exist_ok=True)
        (tree / "SKILL.md").write_text(skill_md(name, body), encoding="utf-8")
        (tree / "references").mkdir(exist_ok=True)
        (tree / "references" / "notes.md").write_text(
            f"{name} reference {variant}\n".strip() + "\n", encoding="utf-8"
        )
        entries[name] = hermes_dir_hash(tree)
    manifest = "".join(f"{name}:{hash_}\n" for name, hash_ in sorted(entries.items()))
    (base / "skills" / ".bundled_manifest").write_text(manifest, encoding="utf-8")
    for name in modify:
        # the user edited a bundled skill AFTER it was seeded: its current
        # hash no longer matches the manifest origin hash
        target = skills / name / "SKILL.md"
        target.write_text(
            skill_md(name, "bundled body\nUSER EDIT ON THIS DEVICE\n"), encoding="utf-8"
        )


def write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


def build_home(
    path: Path, *, modify: frozenset[str], bundled: bool, user: bool, variant: str = ""
) -> Path:
    write(path / "config.yaml", "model:\n  provider: openrouter\n  default: test\n")
    write(path / "SOUL.md", f"soul of {path.name}\n")
    if bundled:
        seed_bundled_tree(path, modify=modify, variant=variant)
    if user:
        write(path / "skills" / USER_SKILL / "SKILL.md", f"---\nname: {USER_SKILL}\n---\nuser skill\n")
    return path


def build_full_home(path: Path, *, modify: frozenset[str], variant: str = "") -> Path:
    """Root home + one profile, each with the bundled pair, a user skill,
    and a config (the profile tree mirrors the root tree)."""
    build_home(path, modify=modify, bundled=True, user=True, variant=variant)
    profile = path / "profiles" / PROFILE
    build_home(profile, modify=modify, bundled=True, user=False, variant=variant)
    write(
        profile / "skills" / PROFILE_SKILL / "SKILL.md",
        f"---\nname: {PROFILE_SKILL}\n---\nprofile user skill\n",
    )
    return path


def mk_engine(home: Path, device: str, backend: FakeBackend) -> Engine:
    return Engine(
        home,
        RoamSettings(backend="fake", device_name=device, archive_key=KEY),
        backend,
        VERSION,
    )


PRISTINE_REL = frozenset(
    {
        f"skills/{CATEGORY}/{PRISTINE}/SKILL.md",
        f"skills/{CATEGORY}/{PRISTINE}/references/notes.md",
        f"skills/{CATEGORY}/DESCRIPTION.md",
        f"profiles/{PROFILE}/skills/{CATEGORY}/{PRISTINE}/SKILL.md",
        f"profiles/{PROFILE}/skills/{CATEGORY}/{PRISTINE}/references/notes.md",
        f"profiles/{PROFILE}/skills/{CATEGORY}/DESCRIPTION.md",
    }
)
HACKED_REL = frozenset(
    {
        f"skills/{CATEGORY}/{HACKED}/SKILL.md",
        f"skills/{CATEGORY}/{HACKED}/references/notes.md",
        f"profiles/{PROFILE}/skills/{CATEGORY}/{HACKED}/SKILL.md",
        f"profiles/{PROFILE}/skills/{CATEGORY}/{HACKED}/references/notes.md",
    }
)
USER_REL = frozenset(
    {
        f"skills/{USER_SKILL}/SKILL.md",
        f"profiles/{PROFILE}/skills/{PROFILE_SKILL}/SKILL.md",
    }
)


# ------------------------------------------------------ (a) classification


def test_pristine_bundled_skills_are_local_top_level_and_profiles(tmp_path: Path) -> None:
    home = build_full_home(tmp_path / "home", modify=frozenset({HACKED}))
    rules = scope_mod.load_rules(home)
    for relpath in sorted(PRISTINE_REL):
        assert scope_mod.classify_path(relpath, rules) is Scope.LOCAL, relpath
    # the manifest itself is dot-state under skills/ and stays IGNORED
    assert scope_mod.classify_path("skills/.bundled_manifest", rules) is Scope.IGNORED
    assert scope_mod.classify_path(
        f"profiles/{PROFILE}/skills/.bundled_manifest", rules
    ) is Scope.IGNORED


def test_pristine_bundled_trees_are_pruned_from_the_walk(tmp_path: Path) -> None:
    """A pristine bundled tree is never even traversed (no sha256, no list)."""
    from roam import inventory

    home = build_full_home(tmp_path / "home", modify=frozenset({HACKED}))
    rules = scope_mod.load_rules(home)
    entries = inventory.walk_home(home, rules, include={Scope.SHARED})
    listed = {entry.relpath for entry in entries}
    assert not any(relpath in PRISTINE_REL for relpath in listed)


def test_modified_bundled_skill_stays_shared_everywhere(tmp_path: Path) -> None:
    home = build_full_home(tmp_path / "home", modify=frozenset({HACKED}))
    rules = scope_mod.load_rules(home)
    for relpath in sorted(HACKED_REL):
        assert scope_mod.classify_path(relpath, rules) is Scope.SHARED, relpath


def test_user_skills_still_sync(tmp_path: Path) -> None:
    home = build_full_home(tmp_path / "home", modify=frozenset({HACKED}))
    rules = scope_mod.load_rules(home)
    for relpath in sorted(USER_REL):
        assert scope_mod.classify_path(relpath, rules) is Scope.SHARED, relpath


def test_user_shared_rule_opts_a_bundled_skill_back_in(tmp_path: Path) -> None:
    """A user can explicitly share a bundled skill by naming it in
    roam/rules.yaml; the user rule precedes every generated rule."""
    home = build_full_home(tmp_path / "home", modify=frozenset({HACKED}))
    write(
        home / "roam" / "rules.yaml",
        "shared:\n  - skills/creative/art-skill/\n",
    )
    rules = scope_mod.load_rules(home)
    assert scope_mod.classify_path(
        f"skills/{CATEGORY}/{PRISTINE}/SKILL.md", rules
    ) is Scope.SHARED


def test_missing_or_hashless_manifest_adds_nothing(tmp_path: Path) -> None:
    """No manifest, or a v1 plain-name line (no origin hash): nothing can be
    proven, so no LOCAL rule is generated (default rules apply)."""
    home = build_full_home(tmp_path / "home", modify=frozenset({HACKED}))
    (home / "skills" / ".bundled_manifest").unlink()
    rules = scope_mod.load_rules(home)
    assert scope_mod.classify_path(
        f"skills/{CATEGORY}/{PRISTINE}/SKILL.md", rules
    ) is Scope.SHARED
    (home / "skills" / ".bundled_manifest").write_text(
        f"{PRISTINE}\n{HACKED}\n", encoding="utf-8"
    )
    rules = scope_mod.load_rules(home)
    assert scope_mod.classify_path(
        f"skills/{CATEGORY}/{PRISTINE}/SKILL.md", rules
    ) is Scope.SHARED


def test_manifest_only_names_what_exists_on_disk(tmp_path: Path) -> None:
    """A manifest entry whose skill directory the user deleted contributes
    no rule (nothing exists to protect) and never resurrects the tree."""
    home = build_full_home(tmp_path / "home", modify=frozenset({HACKED}))
    import shutil

    shutil.rmtree(home / "skills" / CATEGORY / PRISTINE)
    rules = scope_mod.load_rules(home)
    assert scope_mod.classify_path(
        f"skills/{CATEGORY}/{PRISTINE}/SKILL.md", rules
    ) is Scope.SHARED


# ------------------------------------------------------ (b) push


def test_push_carries_only_user_and_modified_bundled_skills(tmp_path: Path) -> None:
    backend = FakeBackend()
    home_a = build_full_home(tmp_path / "home-a", modify=frozenset({HACKED}))
    engine_a = mk_engine(home_a, "dev-a", backend)
    engine_a.init(create_remote=True)
    result = engine_a.push()
    assert result.generation == 1
    carried = set(result.files)
    # user skill + modified bundled skill, root and profile alike
    assert USER_REL <= carried
    assert HACKED_REL <= carried
    # every pristine bundled tree stayed on the device
    assert not (carried & PRISTINE_REL), sorted(carried & PRISTINE_REL)
    # and the pristine bytes are still on disk, untouched
    assert (home_a / "skills" / CATEGORY / PRISTINE / "SKILL.md").read_text(
        encoding="utf-8"
    ).endswith("bundled body\n")


# ------------------------------------------------------ (c) pull


def test_pull_never_overwrites_receiver_bundled_skills(tmp_path: Path) -> None:
    """Device B runs the same Hermes (its pristine copies hash-match its own
    manifest): a pull must leave every bundled byte untouched, deliver the
    user skills, and list the modified bundled skill as refused-by-policy."""
    backend = FakeBackend()
    home_a = build_full_home(tmp_path / "home-a", modify=frozenset({HACKED}))
    engine_a = mk_engine(home_a, "dev-a", backend)
    engine_a.init(create_remote=True)
    engine_a.push()

    home_b = build_full_home(tmp_path / "home-b", modify=frozenset())
    pristine_bytes = {
        relpath: (home_b / relpath).read_bytes() for relpath in sorted(PRISTINE_REL)
    }
    hacked_bytes = {
        relpath: (home_b / relpath).read_bytes()
        for relpath in sorted(HACKED_REL)
    }
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.init(create_remote=False)
    result = engine_b.pull(apply=True)
    assert result.applied
    # user content arrived
    for relpath in sorted(USER_REL):
        assert (home_b / relpath).is_file(), relpath
    # every pristine bundled byte is untouched (hash-checked)
    for relpath, payload in pristine_bytes.items():
        assert (home_b / relpath).read_bytes() == payload, relpath
    # the modified bundled skill was refused by B's own policy (B holds a
    # pristine copy), listed as skipped, and never overwritten
    for relpath in sorted(HACKED_REL):
        assert (home_b / relpath).read_bytes() == hacked_bytes[relpath], relpath
    assert f"skills/{CATEGORY}/{HACKED}/SKILL.md" in result.skipped_local
    assert f"profiles/{PROFILE}/skills/{CATEGORY}/{HACKED}/SKILL.md" in result.skipped_local


def test_carry_forward_never_deletes_or_overwrites_bundled_skills(tmp_path: Path) -> None:
    """B runs a DIFFERENT Hermes version (its pristine bundled bytes differ
    from A's). B pushes its own generation and A pulls it back: neither
    device's bundled trees may be overwritten or deleted by the other's
    manifest — each device keeps the copy its own Hermes ships."""
    backend = FakeBackend()
    home_a = build_full_home(tmp_path / "home-a", modify=frozenset({HACKED}))
    engine_a = mk_engine(home_a, "dev-a", backend)
    engine_a.init(create_remote=True)
    engine_a.push()

    home_b = build_full_home(tmp_path / "home-b", modify=frozenset(), variant="hermes-0.99")
    pristine_bytes = {
        (home, relpath): (home / relpath).read_bytes()
        for home in (home_a, home_b)
        for relpath in sorted(PRISTINE_REL)
    }
    hacked_a = (home_a / "skills" / CATEGORY / HACKED / "SKILL.md").read_bytes()
    hacked_b = (home_b / "skills" / CATEGORY / HACKED / "SKILL.md").read_bytes()
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.init(create_remote=False)
    assert engine_b.pull(apply=True).applied
    # B's own shared edit moves the generation ahead
    write(home_b / "memories" / "NOTE.md", "b shared note\n")
    assert engine_b.push().generation == 2

    result_a = engine_a.pull(apply=True)
    assert result_a.applied
    # every pristine bundled byte on BOTH devices is exactly what that
    # device's own Hermes shipped — never overwritten, never deleted
    for (home, relpath), payload in pristine_bytes.items():
        assert (home / relpath).is_file(), (home, relpath)
        assert (home / relpath).read_bytes() == payload, (home, relpath)
    # A's modified bundled skill still carries A's user edit; B's copy was
    # never touched either (it stays B's own pristine bytes)
    assert (
        home_a / "skills" / CATEGORY / HACKED / "SKILL.md"
    ).read_bytes() == hacked_a
    assert (
        home_b / "skills" / CATEGORY / HACKED / "SKILL.md"
    ).read_bytes() == hacked_b
    # the remote head carries no bundled-skill path at all
    signed_paths = set(result_a.plan.added.get(Scope.SHARED, [])) | set(
        result_a.plan.changed.get(Scope.SHARED, [])
    )
    assert not signed_paths & (PRISTINE_REL | HACKED_REL)


def test_modified_bundled_content_reaches_a_device_without_a_copy(tmp_path: Path) -> None:
    """Device C ships none of these skills (different Hermes version or a
    user-deleted tree): the genuinely modified bundled skill is user work
    and must reach C exactly like a user skill."""
    backend = FakeBackend()
    home_a = build_full_home(tmp_path / "home-a", modify=frozenset({HACKED}))
    engine_a = mk_engine(home_a, "dev-a", backend)
    engine_a.init(create_remote=True)
    engine_a.push()

    home_c = build_home(tmp_path / "home-c", modify=frozenset(), bundled=False, user=False)
    engine_c = mk_engine(home_c, "dev-c", backend)
    engine_c.init(create_remote=False)
    result = engine_c.pull(apply=True)
    assert result.applied
    assert (home_c / "skills" / CATEGORY / HACKED / "SKILL.md").read_text(
        encoding="utf-8"
    ).endswith("USER EDIT ON THIS DEVICE\n")
    assert (home_c / "skills" / USER_SKILL / "SKILL.md").is_file()
    assert not (home_c / "skills" / CATEGORY / PRISTINE / "SKILL.md").exists()
