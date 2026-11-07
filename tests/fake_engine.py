"""Fake roam.engine.Engine for plugin-surface tests (Lane f4).

Returns result mirrors that carry the pre-agreed 0.2.0 fields
(``conflicts`` / ``skipped_local`` / ``recovered`` on pull and status,
``files`` / ``recovered`` on push, ``overwrite_conflicts`` on pull) so the
surface can be exercised before the lane-f2 engine change lands.
``calls`` records every invocation with the exact kwargs passed.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import roam.errors as errors
import roam.model as model


def _local_state() -> model.LocalState:
    return model.LocalState(
        last_pushed_generation=2,
        last_pulled_generation=2,
        last_seen_generation=2,
        last_push_at=1.0,
        last_pull_at=1.0,
    )


def _devices() -> list[model.DeviceInfo]:
    return [
        model.DeviceInfo(
            name="desktop",
            last_push_generation=2,
            last_pull_generation=2,
            hermes_version="0.22.5",
            platform="linux",
            seen_at=1.0,
        ),
        model.DeviceInfo(
            name="laptop",
            last_push_generation=5,
            last_pull_generation=0,
            hermes_version="0.22.5",
            platform="linux",
            seen_at=2.0,
        ),
    ]


@dataclass(frozen=True)
class FakeStatus:
    """Status mirror plus the pre-agreed 0.2.0 ``recovered`` field."""

    device: str
    backend: str
    local: Any
    remote_generation: int | None
    remote_device: str
    remote_created_at: float
    remote_ahead: bool
    local_dirty: bool
    dirty_count: int
    devices: list
    error: str = ""
    recovered: str | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def status_with(**overrides: Any) -> FakeStatus:
    """The default fake Status with overrides applied (tests tweak one field)."""
    values: dict[str, Any] = dict(
        device="desktop",
        backend="local",
        local=_local_state(),
        remote_generation=5,
        remote_device="laptop",
        remote_created_at=2.0,
        remote_ahead=True,
        local_dirty=True,
        dirty_count=3,
        devices=_devices(),
        error="",
        recovered=None,
    )
    values.update(overrides)
    return FakeStatus(**values)


def _diff() -> model.Diff:
    return model.Diff(
        added={model.Scope.SHARED: ["skills/new/SKILL.md"]},
        changed={model.Scope.SHARED: ["SOUL.md"]},
        removed={model.Scope.SESSIONS: ["sessions/old.json"]},
        remote_generation=5,
    )


@dataclass(frozen=True)
class FakePlan:
    """The pre-agreed R11 plan shape (round 3): the 0.2.0 groups plus the
    configs / unchanged / config_conflicts fields the engine will report,
    so the surfaces can be exercised before the engine lane lands."""

    base: Any
    configs: list = field(default_factory=list)
    unchanged: list = field(default_factory=list)
    config_conflicts: list = field(default_factory=list)

    @property
    def added(self):
        return self.base.added

    @property
    def changed(self):
        return self.base.changed

    @property
    def removed(self):
        return self.base.removed

    @property
    def remote_generation(self):
        return self.base.remote_generation

    def to_dict(self) -> dict:
        return {
            **self.base.to_dict(),
            "configs": list(self.configs),
            "unchanged": list(self.unchanged),
            "config_conflicts": list(self.config_conflicts),
        }


@dataclass(frozen=True)
class FakePullResult:
    """PullResult mirror plus the pre-agreed 0.2.0 fields."""

    generation: int
    applied: bool
    plan: Any
    backup_dir: str = ""
    config_merged: bool = False
    plugins_to_install: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    skipped_local: list = field(default_factory=list)
    recovered: str | None = None

    def to_dict(self) -> dict:
        return {
            "generation": self.generation,
            "applied": self.applied,
            "plan": self.plan.to_dict(),
            "backup_dir": self.backup_dir,
            "config_merged": self.config_merged,
            "plugins_to_install": list(self.plugins_to_install),
            "notes": list(self.notes),
            "conflicts": list(self.conflicts),
            "skipped_local": list(self.skipped_local),
            "recovered": self.recovered,
        }


@dataclass(frozen=True)
class FakePushResult:
    """PushResult mirror plus the pre-agreed 0.2.0 fields."""

    generation: int
    bundle_key: str
    bundle_size: int
    file_count: int
    dry_run: bool
    skipped_reason: str = ""
    files: list = field(default_factory=list)
    recovered: str | None = None

    def to_dict(self) -> dict:
        return {
            "generation": self.generation,
            "bundle_key": self.bundle_key,
            "bundle_size": self.bundle_size,
            "file_count": self.file_count,
            "dry_run": self.dry_run,
            "skipped_reason": self.skipped_reason,
            "files": list(self.files),
            "recovered": self.recovered,
        }


class FakeEngine:
    """Stand-in for roam.engine.Engine; raises ``error`` when set."""

    home = Path(__file__).resolve().parent / "fakes" / "fake-home"

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.error = error
        # A result to return instead of the default status (e.g. an
        # unreachable backend) — surface-level tests.
        self.status_result: FakeStatus | None = None
        # Pre-agreed 0.2.0 knobs the surface must handle honestly.
        self.status_recovered: str | None = None
        self.push_files: list[str] = []
        self.push_recovered: str | None = None
        self.push_skipped_reason: str = ""
        self.pull_conflicts: list[str] = []
        self.pull_skipped_local: list[str] = []
        self.pull_recovered: str | None = None
        # R11 surface knobs: the pre-agreed plan fields the surfaces must
        # display (config merges, unchanged files, config conflicts).
        self.pull_configs: list[str] = []
        self.pull_unchanged: list[str] = []
        self.pull_config_conflicts: list[str] = []
        # R9: what lock_holder() reports (None = the lock is free); the
        # auto-push hook skips instead of waiting when this is set.
        self.lock_holder_info: dict | None = None

    def _run(self, name: str, **kwargs):
        self.calls.append((name, kwargs))
        if self.error is not None:
            raise self.error
        return getattr(self, f"_{name}")(**kwargs)

    # ---------------------------------------------------------------- results

    def _status(self, **kwargs) -> FakeStatus:
        if self.status_result is not None:
            return self.status_result
        return status_with(recovered=self.status_recovered)

    def _diff(self, **kwargs) -> model.Diff:
        return _diff()

    def _push(self, *, force: bool = False, dry_run: bool = False, **kwargs) -> FakePushResult:
        if not (self.home / "roam" / "state.json").is_file():
            # The pre-agreed 0.2.0 engine behaviour: push refuses outright.
            raise errors.RoamError("no local roam state — run `hermes roam init` first")
        return FakePushResult(
            generation=6,
            bundle_key="bundles/6-desktop-abc.roam",
            bundle_size=4096,
            file_count=2,
            dry_run=dry_run,
            skipped_reason=self.push_skipped_reason,
            files=list(self.push_files),
            recovered=self.push_recovered,
        )

    def _pull(self, *, apply: bool = False, classes=None, dry_run: bool = False,
              include_prefixes=None, overwrite_conflicts: bool = False,
              **kwargs) -> FakePullResult:
        plan: Any = _diff()
        if self.pull_configs or self.pull_unchanged or self.pull_config_conflicts:
            plan = FakePlan(
                base=_diff(),
                configs=list(self.pull_configs),
                unchanged=list(self.pull_unchanged),
                config_conflicts=list(self.pull_config_conflicts),
            )
        return FakePullResult(
            generation=5,
            applied=apply,
            plan=plan,
            backup_dir="",
            config_merged=False,
            plugins_to_install=["some-plugin"],
            notes=[],
            conflicts=list(self.pull_conflicts),
            skipped_local=list(self.pull_skipped_local),
            recovered=self.pull_recovered,
        )

    def _doctor(self, **kwargs) -> list:
        return [
            model.Check(name="archive key", ok=True, detail="decodes to 32 bytes"),
            model.Check(name="backend", ok=False, detail="no credentials"),
        ]

    def _init(self, *, create_remote: bool = False, **kwargs) -> str:
        return "roam initialized"

    # ------------------------------------------------------------------ api

    def status(self):
        return self._run("status")

    def diff(self):
        return self._run("diff")

    def push(self, *, force=False, dry_run=False):
        return self._run("push", force=force, dry_run=dry_run)

    def pull(self, *, apply=False, classes=None, dry_run=False, include_prefixes=None,
             overwrite_conflicts=False):
        return self._run(
            "pull", apply=apply, classes=classes, dry_run=dry_run,
            include_prefixes=include_prefixes, overwrite_conflicts=overwrite_conflicts,
        )

    def doctor(self):
        return self._run("doctor")

    def init(self, *, create_remote=False):
        return self._run("init", create_remote=create_remote)

    def lock_holder(self):
        """R9 pre-agreed engine API: the current home-lock holder, or None."""
        return self.lock_holder_info


CONFLICT = errors.RemoteAhead("remote head moved to generation 5; pull first")

# A fake home that looks initialised (<home>/roam/state.json present), so the
# push paths that must proceed can be exercised; tests that prove the init
# refusal point .home at a directory without it.
FAKE_HOME = Path(__file__).resolve().parent / "fakes" / "fake-home"
