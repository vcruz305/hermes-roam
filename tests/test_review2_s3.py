"""Adversarial review round 2, lane s3 — live end-to-end, docs truth, surface honesty.

Every test here documents a defect found by following the documented behaviour
(the README / docs/DESIGN.md claims) and asserting it, so each test FAILS
against the current production code (main @ 5f09276). The tests drive the REAL
roam pipeline (real scope / inventory / archive / restore modules, real
AES-GCM bundles and real signed heads) against the in-memory FakeBackend —
exactly the production wiring minus the network — and the real plugin surface
(tools) loaded the same way test_plugin_surface.py loads it.

Findings (test -> finding):
  s3-f1  a pull --apply on an already-converged fleet rewrites every shared
         file and takes a fresh backup of it: the engine never compares the
         staged content with the local file, only local-vs-base for the
         conflict check. `hermes roam diff` says "nothing would change" while
         the pull plan lists every manifest file as "would update" and the
         apply reports "N file(s) written" — the two surfaces contradict
         each other and the pull creates a new full backup directory per run.
  s3-f2  the FIRST pull into a fresh device never lists
         `roam/installed-plugins.json` as "kept local by your scope rules",
         although the file is in the signed head manifest and is silently
         kept local: the zip bundle manifest (what the pull plan iterates)
         is built from the walk entries only and excludes the extras, and
         the kept-local listing only fires from the second pull on, after
         the first apply seeded state.base_files from the head manifest.
         README ("pull --apply lists every path it kept local ... together
         with the paths your local scope rules keep out") and DESIGN.md
         ("pull --apply prints every path it kept local") are false for the
         first pull — the only pull a new device ever runs per the quick
         start.
  s3-f3  the roam_push TOOL does not refuse before `hermes roam init`: the
         init gate exists only in the CLI and the /roam slash command, so
         the model can push a real generation (moving the signed remote
         head) on a device that was never initialised. README ("roam_push
         ... refuses before init") and DESIGN ("push refuses with RoamError
         when <home>/roam/state.json is absent") are false for the tool
         surface.
  s3-f4  `push --dry-run` reports `bundle_size` 0: the README and the tool
         schema promise the dry run lists the bundle's total size together
         with the file list; the engine returns before estimating the
         bundle, so the size is never computed and every surface prints
         "0 bytes".
  s3-f5  plugin.yaml's python_dependencies omits pyyaml while pyproject.toml
         (the file `hermes plugins validate` actually resolves) declares
         cryptography, pyyaml and boto3; the manifest and the real
         dependency list disagree.
"""

import json
import sys
import tomllib
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import load_plugin_package
from fakes.fake_backend import FakeBackend

from roam import head as head_mod
from roam.engine import Engine
from roam.model import RoamSettings

ROOT = Path(__file__).resolve().parent.parent
KEY = bytes(range(1, 33))  # real entropy: weak keys are rejected
VERSION = "0.22.5"


# ------------------------------------------------------------------ helpers


def mk_settings(device: str, **kw) -> RoamSettings:
    return RoamSettings(backend="fake", device_name=device, archive_key=KEY, **kw)


def mk_engine(home: Path, device: str, backend: FakeBackend, **kw) -> Engine:
    return Engine(home, mk_settings(device, **kw), backend, VERSION)


def write(path: Path, data: bytes, mode: int = 0o644) -> None:
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, mode)


def make_home(tmp_path: Path, name: str, files: dict[str, bytes]) -> Path:
    home = tmp_path / name
    for rel, data in files.items():
        write(home / rel, data)
    return home


SOUL = b"# soul v1\nshared values\n"


def fleet(tmp_path, beta_files: dict[str, bytes] | None = None) -> tuple:
    """alpha pushes one shared file; returns (a, b, backend, home_b)."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "alpha", {"SOUL.md": SOUL})
    a = mk_engine(home_a, "alpha", backend)
    a.init(create_remote=True)
    a.push()
    home_b = make_home(tmp_path, "beta", beta_files or {})
    b = mk_engine(home_b, "beta", backend)
    return a, b, backend, home_b


def plan_paths(result) -> list[str]:
    plan = result.plan.to_dict()
    return sorted(
        path
        for group in ("added", "changed", "removed")
        for paths in plan[group].values()
        for path in paths
    )


# ----------------------------------------------------------------- s3-f1


def test_f1_pull_apply_on_converged_fleet_is_a_noop(tmp_path) -> None:
    """After beta installed alpha's generation, the fleet is converged:
    diff() is empty. A second pull --apply must change nothing: the plan
    must be empty and no backup directory may be created. Today the pull
    rewrites every manifest file and takes a fresh backup of it."""
    _, b, _, _ = fleet(tmp_path)
    first = b.pull(apply=True)
    assert "SOUL.md" in plan_paths(first)  # the first install really installs
    assert (b.home / "SOUL.md").read_bytes() == SOUL
    assert b.diff().is_empty()  # the documented pull preview: nothing to change

    second = b.pull(apply=True)
    assert plan_paths(second) == [], (
        "a pull against a converged fleet must plan nothing; the pull plan "
        "must agree with `hermes roam diff`, which reports nothing to change"
    )
    assert second.backup_dir == "", (
        "no file was replaced, so no backup directory may be created; today "
        "every pull --apply backs up and rewrites every shared file"
    )


# ----------------------------------------------------------------- s3-f2


def test_f2_first_pull_lists_installed_plugins_json_as_kept_local(tmp_path) -> None:
    """roam/installed-plugins.json is in the signed head manifest and is
    silently kept local by the pull (never written). README and DESIGN say
    the pull lists every path the scope rules keep local — including on
    the first pull a new device ever runs."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "alpha", {
        "SOUL.md": SOUL,
        "plugins/demo/plugin.yaml": b"name: demo\nversion: 1.0.0\n",
    })
    a = mk_engine(home_a, "alpha", backend)
    a.init(create_remote=True)
    a.push()
    pair = backend.get_head()
    head = head_mod.verify_head(pair[0], KEY)
    assert "roam/installed-plugins.json" in head.files  # the head carries it

    home_b = make_home(tmp_path, "beta", {})
    b = mk_engine(home_b, "beta", backend)
    first = b.pull(apply=False)
    assert "roam/installed-plugins.json" in first.skipped_local, (
        "the bundle manifest is built from the walk entries only, so the "
        "first pull into a fresh device never lists the scope-kept "
        "roam/installed-plugins.json; the listing only appears from the "
        "second pull on, after the first apply seeded state.base_files "
        "from the head manifest — an inconsistent, dishonest first run"
    )


# ----------------------------------------------------------------- s3-f3


def test_f3_roam_push_tool_refuses_before_init(tmp_path, monkeypatch) -> None:
    """README: 'roam_push ... refuses before init'. The CLI and the /roam
    slash command check <home>/roam/state.json; the roam_pull tool's
    allow_agent_apply gate was added in 0.2.0 but the push tool has no
    init gate at all, so the model can push a real generation on a device
    that never ran `hermes roam init`."""
    plugin = load_plugin_package()
    backend = FakeBackend()
    home_alpha = make_home(tmp_path, "alpha", {"SOUL.md": SOUL})
    mk_engine(home_alpha, "alpha", backend).init(create_remote=True)

    home_beta = make_home(tmp_path, "beta", {"SOUL.md": b"# beta soul\n"})
    engine = mk_engine(home_beta, "beta", backend)
    monkeypatch.setattr(plugin.settings, "build_engine", lambda: engine)

    out = json.loads(plugin.tools.roam_push({}))
    assert out.get("error") and "init" in str(out.get("error", "")).lower(), (
        "the roam_push tool must refuse on a home without roam state, like "
        "the CLI does ('no local roam state — run hermes roam init first'); "
        f"today it returns {out}"
    )
    pair = backend.get_head()
    head = head_mod.verify_head(pair[0], KEY)
    assert head.generation == 0, "the tool must not move the remote head"


# ----------------------------------------------------------------- s3-f4


def test_f4_push_dry_run_lists_the_total_size(tmp_path) -> None:
    """README: 'push --dry-run ... lists the bundle's file paths ... together
    with the total size, without uploading anything'; the roam_push schema
    says the same. The engine returns bundle_size 0 for dry runs, so every
    surface prints '0 bytes'."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "alpha", {"SOUL.md": SOUL})
    a = mk_engine(home_a, "alpha", backend)
    a.init(create_remote=True)
    a.push()
    write(home_a / "SOUL.md", b"# soul v2\nmore shared values\n")

    result = a.push(dry_run=True)
    assert result.files, "the file list is reported (this part works)"
    assert result.bundle_size > 0, (
        "the dry run must report the real total size of the bundle it "
        "describes; today the engine returns 0 and the CLI prints "
        "'would push N files, 0 bytes'"
    )


# ----------------------------------------------------------------- s3-f5


def test_f5_manifest_declares_every_real_dependency() -> None:
    """`hermes plugins validate --install-deps` resolves pyproject.toml (3
    requirements: cryptography, pyyaml, boto3) and ignores the manifest's
    python_dependencies when a pyproject exists. The manifest must not
    disagree with the real dependency list: a catalog reviewer reading only
    plugin.yaml sees 2 of the 3 runtime dependencies."""
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    real = pyproject["project"]["dependencies"]
    declared = manifest.get("python_dependencies", [])

    def norm(spec: str) -> str:
        return spec.split(">")[0].split("=")[0].split("<")[0].strip().lower()

    assert {norm(spec) for spec in declared} == {norm(spec) for spec in real}, (
        f"plugin.yaml declares {declared} but the package really installs "
        f"{real}"
    )


# ----------------------------------------------------------------- s3-f6


def test_f6_pull_plan_lists_the_config_merge(tmp_path) -> None:
    """README: 'hermes roam diff — pull preview: the added / changed /
    removed paths a pull would install' and 'A pull only previews until you
    pass --apply'. The pull plan the user is told to review must therefore
    include the config.yaml merge the apply will perform. Today the plan
    hides it: config files are routed to plan.configs and never appear in
    the printed plan, while `hermes roam diff` lists config.yaml as
    changed — the user reviews a plan that says one file and approves an
    install that also rewrites their model/provider configuration."""
    backend = FakeBackend()
    home_a = make_home(tmp_path, "alpha", {
        "SOUL.md": SOUL,
        "config.yaml": b"model: gpt-x\nterminal:\n  theme: dark-a\n",
    })
    a = mk_engine(home_a, "alpha", backend)
    a.init(create_remote=True)
    a.push()
    home_b = make_home(tmp_path, "beta", {
        "config.yaml": b"model: gpt-y\nterminal:\n  theme: light-b\n",
    })
    b = mk_engine(home_b, "beta", backend)

    diff = b.diff()
    assert "config.yaml" in diff.to_dict()["changed"]["shared"], (
        "the documented pull preview does flag the config change"
    )
    plan = b.pull(apply=False)
    listed = {
        path
        for group in ("added", "changed", "removed")
        for paths in plan.plan.to_dict()[group].values()
        for path in paths
    }
    assert "config.yaml" in listed or any(
        "config" in note for note in plan.notes
    ), (
        "the pull plan must tell the user that the apply will merge "
        "config.yaml; today the plan lists only the non-config files and "
        f"the merge happens silently on --apply (listed={sorted(listed)}, "
        f"notes={plan.notes})"
    )

def test_f8_disclosures_list_every_command_and_file() -> None:
    """s3-f8: the disclosure section must list every git command the backend
    runs (including `git config --get core.sshCommand`, which the old list
    omitted), every file written under <home>/roam/ (last.roam, floor.json,
    rules.yaml, lock), and the private core import must be referenced by
    function name, not by a line number that rots."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for command in ("init", "remote get-url/add/set-url",
                    "config --get core.sshCommand", "fetch", "rev-parse",
                    "cat-file", "hash-object", "read-tree", "update-index",
                    "write-tree", "commit-tree", "push", "show", "ls-remote"):
        assert command in readme, f"the git command list must include {command!r}"
    for path in ("roam/last.roam", "roam/floor.json", "roam/rules.yaml",
                 "roam/lock"):
        assert path in readme, f"the files-written list must include {path!r}"
    disclosures = (ROOT / "docs/DISCLOSURES.md").read_text(encoding="utf-8")
    assert "settings.py:170" not in disclosures
    assert "hermes_version()" in disclosures


# ----------------------------------------------------------------- s3-f9


def test_f9_quick_start_is_actionable() -> None:
    """s3-f9: the quick start must name {home}/.env explicitly (not an
    unnamed 'profile' .env), configure a real backend inline, and be honest
    about installation: the catalog install works once admitted, and the
    local-path install is shown meanwhile."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    quick = readme.split("## Quick start")[1].split("## What syncs")[0]
    assert "{home}/.env" in quick
    assert "s3_bucket" in quick  # a real backend is configured inline
    assert "admitted to the Hermes catalog" in quick
    assert "hermes plugins install file:///path/to/hermes-roam" in quick


# ----------------------------------------------------------------- s3-f10


def test_f10_skill_keeps_destructive_commands_with_the_user() -> None:
    """s3-f10: the bundled skill must tell the agent to have the USER run
    init --create-remote, push and pull --apply, and never to read .env or
    auth.json."""
    skill = (ROOT / "skills/roam-setup/SKILL.md").read_text(encoding="utf-8")
    assert "Have the USER run" in skill
    for command in ("init --create-remote", "hermes roam push",
                    "hermes roam pull --apply"):
        assert command in skill
    assert "Never read `.env`, `auth.json`" in skill
    assert "never open, print, or summarize" in skill



# ------------------------------------------------------------------ s3 f4_surf (surface half, g4)

def test_f4_surfaces_relay_the_dry_run_estimate(plugin, fake_engine) -> None:
    """# RULING (round 3, R11) — surface half of s3-f4: computing the dry-run
    estimate (estimate_bundle_bytes) is the engine lane's work; this half
    asserts the surfaces. The README and the roam_push schema promise the
    dry run lists the bundle's file paths together with the total size, so
    every surface must print the estimate the engine reports — never a
    hardcoded '0 bytes'."""
    import contextlib
    import io

    fake_engine.push_files = ["skills/a/SKILL.md", "memories/m.md"]
    payload = json.loads(plugin.tools.roam_push({"dry_run": True}))
    assert payload["push"]["bundle_size"] > 0, (
        "the tool JSON must carry the engine's dry-run size estimate"
    )
    assert payload["push"]["files"], "the tool JSON must carry the file list"
    slash = plugin.commands.handle("push dry-run")
    assert "bytes" in slash and "0 bytes" not in slash, (
        "the slash surface must print the reported estimate, not hide it"
    )
    argv = plugin.cli.make_parser().parse_args(["push", "--dry-run"])
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        plugin.cli.handler(argv)
    out = buffer.getvalue()
    assert "0 bytes" not in out, "the CLI must print the reported estimate"
    assert "bytes" in out


# ----------------------------------------------------------------- s3-f5




# ------------------------------------------------------------------ s3 f6_surf (surface half, g4)

def test_f6_surfaces_name_the_config_merge(plugin, fake_engine, capsys) -> None:
    """# RULING (round 3, R11) — surface half of s3-f6: routing config.yaml
    into InstallPlan.configs is the engine lane's work; this half asserts
    the surfaces. The pull plan the user is told to review must include the
    config.yaml merge the apply will perform, so once the engine reports a
    config merge every surface prints 'would merge config.yaml (host-scoped
    keys stay local)' instead of hiding it. 0.4.2: a first_pull mode merge
    prints the union rules instead."""
    fake_engine.pull_configs = [("config.yaml", "merge")]
    slash = plugin.commands.handle("pull")
    assert "would merge config.yaml (host-scoped keys stay local)" in slash, (
        "the slash plan must name the config merge the apply performs"
    )
    argv = plugin.cli.make_parser().parse_args(["pull"])
    code = plugin.cli.handler(argv)
    out = capsys.readouterr().out
    assert code == 0
    assert "would merge config.yaml (host-scoped keys stay local)" in out, (
        "the CLI plan must name the config merge the apply performs"
    )
    payload = json.loads(plugin.tools.roam_pull({}))
    assert ["config.yaml", "merge"] in payload["pull"]["plan"]["configs"], (
        "the tool JSON plan must carry the config merge"
    )


# ----------------------------------------------------------------- s3-f8

