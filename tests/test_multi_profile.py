"""Multi-profile PoC suite (0.4.2 lane): profiles and bots travel as working
Hermes profiles, roam always operates on the ROOT home, the session archive
covers every profile, and per-profile exclusion/deletion follow the
carry-forward contract.

Written RED against aca19a4: the profile-metadata, root-resolution,
per-profile session-archive, and profile-state-disclosure tests fail there;
the exclusion/deletion tests pin the ruled behavior.
"""

import base64
import json
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import conftest  # noqa: E402
from fakes.fake_backend import FakeBackend  # noqa: E402

hermes_roam = conftest.load_plugin_package()  # the plugin package
from roam import head as head_mod  # noqa: E402
from roam import scope as scope_mod  # noqa: E402
from roam import session_archive as archive_mod  # noqa: E402
from roam.engine import Engine  # noqa: E402
from roam.model import RoamSettings  # noqa: E402

KEY = bytes(range(1, 33))
VERSION = "0.22.5"

PROFILE_NAMES = ("bot1", "bot2")


def write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, mode)


def mk_engine(home: Path, device: str, backend: FakeBackend, **kw) -> Engine:
    return Engine(
        home,
        RoamSettings(backend="fake", device_name=device, archive_key=KEY, **kw),
        backend,
        VERSION,
    )


def signed_files(backend: FakeBackend) -> dict:
    pair = backend.get_head()
    assert pair is not None
    return head_mod.verify_head(pair[0], KEY).files


def build_root_home(tmp_path: Path, name: str, profiles=PROFILE_NAMES) -> Path:
    """A ROOT home whose shared surface exists at the top level AND inside
    every named profile, with per-profile local state that must stay put."""
    home = tmp_path / name
    write(home / "config.yaml", b"model:\n  provider: p-x\n")
    write(home / "SOUL.md", f"{name} root soul".encode())
    write(home / ".env", b"ROOT_SECRET=1\n")
    for profile in profiles:
        base = home / "profiles" / profile
        write(base / "config.yaml", f"model:\n  provider: p-{profile}\n".encode())
        write(base / "SOUL.md", f"{profile} soul".encode())
        write(base / "profile.yaml", f"description: {profile} support bot\n".encode())
        write(base / "memories/MEMORY.md", f"{profile} memory".encode())
        write(base / f"skills/{profile}-skill/SKILL.md", f"{profile} skill".encode())
        write(base / ".env", f"{profile}_SECRET=1\n".encode())
        write(base / "state.db", b"sqlite-bytes")
    return home


def cli_args(plugin, *argv):
    return plugin.cli.make_parser().parse_args(list(argv))


def transcript(name: str, session_id: str, updated: str, body: bytes = b"") -> dict:
    return {
        "name": name,
        "data": (
            f"---\nsession_id: {session_id}\ntitle: 't {session_id}'\n"
            f"created_at: {updated}\nupdated_at: {updated}\nmessage_count: 2\n---\n"
        ).encode()
            + body,
        "header": {"session_id": session_id, "updated_at": updated},
    }


# ------------------------------------------------------------ (a) metadata


def test_profile_yaml_metadata_is_shared() -> None:
    """Hermes keeps per-profile metadata (description, display name, bot
    title) in profiles/<name>/profile.yaml and a pulled profile without an
    identity marker is not even listable: profile.yaml must be SHARED."""
    rules = scope_mod.Rules.default()
    for relpath in (
        "profiles/bot1/profile.yaml",
        "profiles/bot-2/profile.yaml",
    ):
        assert scope_mod.classify_path(relpath, rules) is scope_mod.Scope.SHARED, relpath


def test_profiles_arrive_working_local_state_stays(tmp_path: Path) -> None:
    """Push from a ROOT home with two profiles, pull on a fresh second home:
    every profile's shared surface arrives byte-identical (profile.yaml
    included) and no profile's .env/state.db ever travels."""
    backend = FakeBackend()
    home_a = build_root_home(tmp_path, "home-a")
    engine_a = mk_engine(home_a, "dev-a", backend)
    engine_a.init(create_remote=True)
    assert engine_a.push().generation == 1

    home_b = tmp_path / "home-b"
    write(home_b / "config.yaml", b"model:\n  provider: p-b\n")
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.init(create_remote=False)
    result = engine_b.pull(apply=True)
    assert result.applied
    for profile in PROFILE_NAMES:
        base = home_b / "profiles" / profile
        assert (base / "SOUL.md").read_bytes() == f"{profile} soul".encode()
        assert (base / "profile.yaml").is_file(), (
            f"profiles/{profile}/profile.yaml did not travel: the pulled "
            "profile would carry no Hermes profile metadata and, without any "
            "other identity marker, would not be listable at all"
        )
        assert (base / "memories/MEMORY.md").read_bytes() == f"{profile} memory".encode()
        assert (base / f"skills/{profile}-skill/SKILL.md").is_file()
        assert not (base / ".env").exists()
        assert not (base / "state.db").exists()
    assert not (home_b / ".env").exists()


# --------------------------------------------------- (b) root-home contract


def test_sync_root_resolution() -> None:
    """Path-structure-only ROOT resolution: a profile home maps to its root,
    everything else (including a bare 'profiles' sibling layout) maps to
    itself."""
    settings = hermes_roam.settings
    assert settings.sync_root(Path("/root/work/homes/mp-a")) == Path(
        "/root/work/homes/mp-a"
    )
    assert settings.sync_root(Path("/root/work/homes/mp-a/profiles/bot1")) == Path(
        "/root/work/homes/mp-a"
    )
    assert settings.sync_root(Path("/root/work/homes/mp-a/profiles/bot-2")) == Path(
        "/root/work/homes/mp-a"
    )
    # not a profile-shaped path: unchanged
    assert settings.sync_root(Path("/root/work/homes/profiles")) == Path(
        "/root/work/homes/profiles"
    )
    assert settings.active_profile_name(Path("/root/work/homes/mp-a")) == "default"
    assert (
        settings.active_profile_name(Path("/root/work/homes/mp-a/profiles/bot1"))
        == "bot1"
    )


def test_build_engine_targets_the_root_from_a_profile_context(
    tmp_path: Path, monkeypatch
) -> None:
    """`hermes -p NAME roam ...` must operate on the ROOT home — one store
    holding the whole tree with profile-relative paths — never on the
    profile dir (which would publish profile files as top-level paths and
    mix profiles)."""
    root = tmp_path / "mp-a"
    store = tmp_path / "store"
    profile_home = root / "profiles" / "bot1"
    write(profile_home / "SOUL.md", b"bot1")
    settings_obj = RoamSettings(
        backend="local",
        device_name="dev-a",
        local_dir=str(store),
        archive_key=KEY,
    )
    monkeypatch.setattr(hermes_roam.settings, "load_settings", lambda: settings_obj)
    monkeypatch.setattr(
        hermes_roam.settings, "hermes_home", lambda: profile_home
    )
    engine = hermes_roam.settings.build_engine()
    assert engine.home == root, (
        "roam inside a profile context operated on the profile dir instead "
        "of the ROOT home: pushes would publish profile files as top-level "
        "paths and mix profiles into one store"
    )
    # outside a profile context the active home IS the root: unchanged
    monkeypatch.setattr(hermes_roam.settings, "hermes_home", lambda: root)
    assert hermes_roam.settings.build_engine().home == root


def test_profile_context_settings_come_from_the_root_config(
    tmp_path: Path, monkeypatch
) -> None:
    """In a profile context the sync settings come from the ROOT home's
    config.yaml (the fleet store is configured once, at the root), never
    from the profile's own config."""

    class Ctx:
        def get_config(self, key, default=None):  # the PROFILE's config
            return "profile-store-dir" if key == "local_dir" else default

    profile_home = tmp_path / "profiles" / "bot1"
    write(profile_home / "SOUL.md", b"bot1")
    monkeypatch.setattr(hermes_roam.settings, "hermes_home", lambda: profile_home)
    hermes_roam.settings.set_context(Ctx())
    monkeypatch.setenv("ROAM_ARCHIVE_KEY", base64.b64encode(KEY).decode())
    root_config = tmp_path / "config.yaml"
    write(
        root_config,
        b"plugins:\n  entries:\n    hermes-roam:\n      settings:\n"
        b"        backend: local\n        local_dir: root-store-dir\n",
    )

    def read_root(path: Path) -> dict:
        assert path == root_config, "the profile-context settings read another file"
        return yaml.safe_load(root_config.read_text())

    monkeypatch.setattr(hermes_roam.settings, "_root_config_raw", read_root)
    loaded = hermes_roam.settings.load_settings()
    assert loaded.local_dir == "root-store-dir", (
        "the sync settings in a profile context came from somewhere other "
        "than the ROOT home's config.yaml"
    )


# ------------------------------------------- (c) session archive per profile


def test_session_archive_exports_every_profile(tmp_path: Path, monkeypatch) -> None:
    """With sync_sessions on, the push exports the ROOT home's sessions
    (profile 'default') AND every profile that has sessions, into
    archive/sessions/<device>/<profile>/; the listing groups by device and
    profile."""
    backend = FakeBackend()
    home_a = build_root_home(tmp_path, "home-a")
    engine_a = mk_engine(home_a, "dev-a", backend, sync_sessions=True)

    exports = {
        home_a: [transcript("root-1.md", "root-1", "2026-11-07")],
        home_a / "profiles" / "bot1": [
            transcript("bot-1.md", "bot-1", "2026-11-07")
        ],
    }

    def fake_export(home, scratch, exe, timeout):
        return exports.get(Path(home), []), ""

    monkeypatch.setattr(archive_mod, "find_hermes", lambda: "hermes")
    monkeypatch.setattr(archive_mod, "collect_export", fake_export)
    engine_a.init(create_remote=True)
    result = engine_a.push()
    assert result.generation == 1
    files = signed_files(backend)
    assert "archive/sessions/dev-a/default/root-1.md" in files, (
        "the ROOT home's sessions were not published under the 'default' "
        f"profile namespace: {sorted(files)}"
    )
    assert "archive/sessions/dev-a/bot1/bot-1.md" in files, (
        "profile bot1's sessions were not exported: the archive covers only "
        "one profile's state.db"
    )
    assert not [p for p in files if p == "archive/sessions/dev-a/bot-1.md"]

    home_b = tmp_path / "home-b"
    write(home_b / "config.yaml", b"model:\n  provider: p-b\n")
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.init(create_remote=False)
    engine_b.pull(apply=True)
    assert (home_b / "roam/session-archive/dev-a/default/root-1.md").is_file()
    assert (home_b / "roam/session-archive/dev-a/bot1/bot-1.md").is_file()
    listing = archive_mod.list_local_archive(home_b)
    by_device = {d["device"]: d for d in listing}
    profiles = {p["profile"]: p for p in by_device["dev-a"]["profiles"]}
    assert set(profiles) == {"default", "bot1"}, (
        "the pulled archive listing is not grouped by device AND profile"
    )
    assert [s["session_id"] for s in profiles["bot1"]["sessions"]] == ["bot-1"]


def test_session_archive_cap_is_shared_across_profiles(
    tmp_path: Path, monkeypatch
) -> None:
    """sessions_max_mb is ONE shared cap across every profile's export,
    newest transcripts first regardless of which profile they came from."""
    backend = FakeBackend()
    home_a = build_root_home(tmp_path, "home-a")
    engine_a = mk_engine(
        home_a, "dev-a", backend, sync_sessions=True, sessions_max_mb=1
    )
    exports = {
        home_a: [transcript("old-root.md", "old-root", "2026-11-01", b"o" * 800_000)],
        home_a / "profiles" / "bot1": [
            transcript("new-bot.md", "new-bot", "2026-11-07", b"n" * 10),
            transcript("mid-bot.md", "mid-bot", "2026-11-05", b"m" * 400_000),
        ],
    }

    def fake_export(home, scratch, exe, timeout):
        return exports.get(Path(home), []), ""

    monkeypatch.setattr(archive_mod, "find_hermes", lambda: "hermes")
    monkeypatch.setattr(archive_mod, "collect_export", fake_export)
    engine_a.init(create_remote=True)
    result = engine_a.push()
    files = signed_files(backend)
    assert "archive/sessions/dev-a/bot1/new-bot.md" in files
    assert "archive/sessions/dev-a/bot1/mid-bot.md" in files
    assert "archive/sessions/dev-a/default/old-root.md" not in files, (
        "the size cap must be shared across profiles and keep the NEWEST "
        "transcripts first, not each profile's oldest"
    )
    assert any("dropped 1 older session(s)" in note for note in result.notes)


def test_sessions_show_needs_device_profile_and_session(plugin) -> None:
    """`hermes roam sessions show` addresses one transcript by device,
    profile and session id (the archive tree is grouped by both)."""
    args = cli_args(plugin, "sessions", "show", "dev-a", "default", "sess-1")
    assert (args.sessions_action, args.device, args.profile, args.session_id) == (
        "show",
        "dev-a",
        "default",
        "sess-1",
    )


def test_slash_sessions_groups_by_device_and_profile(
    tmp_path: Path, monkeypatch
) -> None:
    """/roam sessions lists the pulled archive grouped by device and
    profile and never dumps a transcript."""
    root = tmp_path / "root"
    archive = root / "roam" / "session-archive" / "dev-a"
    write(archive / "default" / "sess-1.md", b"---\nsession_id: sess-1\n---\n")
    write(archive / "bot1" / "sess-2.md", b"---\nsession_id: sess-2\n---\n")
    monkeypatch.setattr(hermes_roam.settings, "hermes_home", lambda: root)
    text = hermes_roam.commands.handle("sessions")
    assert "dev-a" in text
    assert "default" in text and "bot1" in text, (
        f"/roam sessions does not group the archive by profile: {text}"
    )
    assert "sess-1 --" not in text  # titles/ids may appear; bodies never


# ------------------------------------------------ (d) per-profile exclusion


def test_rules_can_exclude_one_profile(tmp_path: Path) -> None:
    """A local: rule for profiles/NAME/ opts one profile out of sync on this
    device: its files are neither pulled nor deleted, the other profiles
    sync, and this device's push carries the excluded profile's fleet state
    forward byte-identical (only a device that can see it can delete it)."""
    backend = FakeBackend()
    home_a = build_root_home(tmp_path, "home-a")
    engine_a = mk_engine(home_a, "dev-a", backend)
    engine_a.init(create_remote=True)
    engine_a.push()

    home_b = build_root_home(tmp_path, "home-b", profiles=("bot1",))
    write(home_b / "roam/rules.yaml", b"local:\n  - profiles/bot2/\n")
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.init(create_remote=False)
    result = engine_b.pull(apply=True)
    assert (home_b / "profiles/bot1/SOUL.md").read_bytes() == b"bot1 soul"
    assert not (home_b / "profiles/bot2/SOUL.md").exists(), (
        "the excluded profile's files were pulled anyway"
    )
    assert any(
        p.startswith("profiles/bot2/") for p in result.skipped_local
    ), "the excluded profile was not reported as kept local"

    # the excluded profile's fleet state survives this device's pushes
    write(home_b / "skills/own/SKILL.md", b"b's own")
    engine_b.push()
    files = signed_files(backend)
    assert "profiles/bot2/SOUL.md" in files, (
        "the excluded profile's entries were dropped from the signed head by a "
        "device that cannot see them"
    )


def test_status_and_rules_show_profile_sync_state(tmp_path: Path, monkeypatch) -> None:
    """`hermes roam status` and `hermes roam rules` name the ROOT home they
    cover and list every profile with its synced/excluded state."""
    root = tmp_path / "mp-a"
    write(root / "profiles/bot1/SOUL.md", b"bot1")
    write(root / "profiles/bot2/SOUL.md", b"bot2")
    write(root / "roam/rules.yaml", b"local:\n  - profiles/bot2/\n")
    monkeypatch.setattr(hermes_roam.settings, "hermes_home", lambda: root)
    state = hermes_roam.settings.profile_sync_state()
    by_name = {entry["profile"]: entry for entry in state}
    assert set(by_name) == {"default", "bot1", "bot2"}
    assert by_name["default"]["synced"] is True
    assert by_name["bot1"]["synced"] is True
    assert by_name["bot2"]["synced"] is False, (
        "the rules.yaml exclusion of profiles/bot2/ is not reflected in the "
        "profile sync state"
    )


def test_cli_status_names_the_root_and_profile_state(
    plugin, fake_engine, tmp_path, monkeypatch, capsys
) -> None:
    root = tmp_path / "mp-a"
    write(root / "profiles/bot1/SOUL.md", b"bot1")
    write(root / "profiles/bot2/SOUL.md", b"bot2")
    write(root / "roam/rules.yaml", b"local:\n  - profiles/bot2/\n")
    monkeypatch.setattr(hermes_roam.settings, "hermes_home", lambda: root)
    code = plugin.cli.handler(cli_args(plugin, "status", "--json"))
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["covered_root"] == str(root), (
        "status does not name the ROOT home it covered"
    )
    by_name = {entry["profile"]: entry["synced"] for entry in payload["profiles"]}
    assert by_name == {"default": True, "bot1": True, "bot2": False}, (
        "status does not list every profile with its synced/excluded state"
    )
    code = plugin.cli.handler(cli_args(plugin, "status"))
    text = capsys.readouterr().out
    assert code == 0
    assert "bot1(synced)" in text and "bot2(excluded)" in text, (
        f"status does not show the per-profile sync state: {text}"
    )


# --------------------------------------------------- (e) profile lifecycle


def test_profile_deletion_follows_carry_forward(tmp_path: Path) -> None:
    """Deletion follows the carry-forward semantics: a device that EXCLUDED a
    profile can never delete it (its push carries the entries forward and
    the other devices keep the files), while a device that CAN see the
    profile deletes it everywhere once it pushes."""
    backend = FakeBackend()
    home_a = build_root_home(tmp_path, "home-a")
    engine_a = mk_engine(home_a, "dev-a", backend)
    engine_a.init(create_remote=True)
    engine_a.push()

    home_b = build_root_home(tmp_path, "home-b", profiles=())
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.init(create_remote=False)
    engine_b.pull(apply=True)
    assert (home_b / "profiles/bot1/SOUL.md").is_file()
    assert (home_b / "profiles/bot2/SOUL.md").is_file()

    # a device that excluded the profile can never delete it: its push
    # carries the profile's entries forward byte-identical
    home_c = build_root_home(tmp_path, "home-c", profiles=())
    write(home_c / "roam/rules.yaml", b"local:\n  - profiles/bot2/\n")
    engine_c = mk_engine(home_c, "dev-c", backend)
    engine_c.init(create_remote=False)
    engine_c.pull(apply=True)
    assert not (home_c / "profiles/bot2/SOUL.md").exists()
    write(home_c / "skills/own/SKILL.md", b"c's own")
    engine_c.push()
    files = signed_files(backend)
    assert "profiles/bot2/SOUL.md" in files, (
        "the excluded device's push dropped the profile entries it cannot see"
    )
    engine_b.pull(apply=True)
    assert (home_b / "profiles/bot2/SOUL.md").is_file(), (
        "the excluded device's push deleted the profile on the device that "
        "never excluded it"
    )

    # a device that CAN see the profile deletes it everywhere it is visible
    import shutil

    engine_a.pull(apply=True)  # accept the intermediate generation first
    shutil.rmtree(home_a / "profiles" / "bot2")
    engine_a.push()
    engine_b.pull(apply=True)
    assert not (home_b / "profiles/bot2/SOUL.md").exists(), (
        "the profile deleted on the pushing device survived on the device "
        "that can see it"
    )
    assert "profiles/bot2/SOUL.md" not in signed_files(backend)


def test_one_device_only_profile_arrives_on_the_other(tmp_path: Path) -> None:
    """A profile that exists only on the pushing device arrives on the
    pulling device through the normal push/pull round trip."""
    backend = FakeBackend()
    home_a = build_root_home(tmp_path, "home-a", profiles=("bot1",))
    write(home_a / "profiles/bot3/SOUL.md", b"bot3 soul")
    write(home_a / "profiles/bot3/profile.yaml", b"description: bot3\n")
    engine_a = mk_engine(home_a, "dev-a", backend)
    engine_a.init(create_remote=True)
    engine_a.push()

    home_b = tmp_path / "home-b"
    write(home_b / "config.yaml", b"model:\n  provider: p-b\n")
    engine_b = mk_engine(home_b, "dev-b", backend)
    engine_b.init(create_remote=False)
    engine_b.pull(apply=True)
    assert (home_b / "profiles/bot3/SOUL.md").read_bytes() == b"bot3 soul"
    assert (home_b / "profiles/bot3/profile.yaml").is_file()
