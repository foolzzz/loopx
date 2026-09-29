"""The test session refuses access to the real user LoopX runtime root.

These tests exercise the guard against a temporary stand-in for the real root,
so a broken guard can never touch the owner's live runtime state.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import real_runtime_root_guard as guard

from loopx.paths import DEFAULT_RUNTIME_ROOT

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_session_protects_the_default_runtime_root_whatever_home_a_test_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert os.path.normpath(DEFAULT_RUNTIME_ROOT) in guard.protected_roots()

    monkeypatch.setenv("HOME", str(tmp_path))

    assert os.path.normpath(DEFAULT_RUNTIME_ROOT) in guard.protected_roots()
    assert not any(root.startswith(str(tmp_path)) for root in guard.protected_roots())


def test_a_test_owned_home_keeps_a_usable_runtime_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    goal_dir = Path.home() / ".codex" / "loopx" / "goals" / "guard-probe"

    goal_dir.mkdir(parents=True)
    (goal_dir / "state.json").write_text("{}", encoding="utf-8")

    assert [path.name for path in goal_dir.parent.iterdir()] == ["guard-probe"]
    assert guard.take_violations() == []


def test_in_process_access_to_the_protected_root_is_refused_and_named(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    stand_in = tmp_path / "real-home" / ".codex" / "loopx"
    (stand_in / "goals").mkdir(parents=True)
    (stand_in / "registry.global.json").write_text("{}", encoding="utf-8")

    with guard.protecting(stand_in, tmp_path / "report.jsonl"):
        with pytest.raises(PermissionError, match="real LoopX runtime root"):
            (stand_in / "goals" / "probe").mkdir()
        with pytest.raises(PermissionError):
            (stand_in / "goals" / "probe.json").write_text("{}", encoding="utf-8")
        with pytest.raises(PermissionError):
            (stand_in / "registry.global.json").read_text(encoding="utf-8")
        with pytest.raises(PermissionError):
            list((stand_in / "goals").iterdir())
        violations = guard.take_violations()

    events = [violation["event"] for violation in violations]
    assert events[:3] == ["os.mkdir", "open", "open"]
    assert events[3:] in (["os.listdir"], ["os.scandir"])  # Path.iterdir differs by Python version
    assert list((stand_in / "goals").iterdir()) == []
    message = guard.describe(request.node.nodeid, violations)
    assert message.startswith(f"{request.node.nodeid} used the real LoopX runtime root")
    assert f"os.mkdir {stand_in / 'goals' / 'probe'} (this test process)" in message
    assert guard.take_violations() == []


def test_a_cli_subprocess_resolving_the_default_root_is_refused_and_reported(
    tmp_path: Path,
) -> None:
    real_home = tmp_path / "real-home"
    stand_in = real_home / ".codex" / "loopx"
    probe = (
        "from loopx.paths import DEFAULT_RUNTIME_ROOT; "
        "(DEFAULT_RUNTIME_ROOT / 'goals' / 'guard-probe').mkdir(parents=True)"
    )

    with guard.protecting(stand_in, tmp_path / "report.jsonl"):
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=REPO_ROOT,
            env={**os.environ, "HOME": str(real_home)},
            capture_output=True,
            text=True,
            check=False,
        )
        violations = guard.take_violations()

    assert completed.returncode != 0
    assert "test guard refused os.mkdir" in completed.stderr
    assert not stand_in.exists()
    assert [(violation["event"], violation["path"]) for violation in violations] == [
        ("os.mkdir", str(stand_in / "goals" / "guard-probe"))
    ]
    assert violations[0]["pid"] != os.getpid()
