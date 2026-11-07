"""Tests run without Hermes. ``roam`` imports as a top-level package from the repo root;
the plugin surface (``__init__.py``, ``cli.py`` ...) is loaded as package ``hermes_roam`` via
``load_plugin_package()`` so relative imports inside it resolve."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HERMES_HOME", str(ROOT / ".test-home-never-used"))


def load_plugin_package(name: str = "hermes_roam"):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def plugin():
    """The plugin package (as `hermes_roam`) — shared by the surface tests."""
    return load_plugin_package()


@pytest.fixture()
def fake_engine(plugin, monkeypatch):
    """A FakeEngine wired into the plugin's settings.build_engine — shared
    by the surface tests (plugin surface and the review suites)."""
    tests_dir = Path(__file__).resolve().parent
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))
    import fake_engine as fake_engine_mod

    engine = fake_engine_mod.FakeEngine()
    monkeypatch.setattr(plugin.settings, "build_engine", lambda: engine)
    return engine
