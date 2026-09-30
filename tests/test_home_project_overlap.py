from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from loopx.bootstrap import bootstrap_project
from loopx.bootstrap_command_pack import inspect_bootstrap_connection
from loopx.control_plane.projects.registry import register_project_goal
from loopx.control_plane.testing.canary_harness import default_state_file
from loopx.project_prompt import build_new_project_prompt
from loopx.project_uninstall import uninstall_project


GOAL_ID = "home-project-goal"


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _goal(home: Path, registry_path: Path, state_file: str) -> dict[str, object]:
    return {
        "id": GOAL_ID,
        "objective": "Keep project state separate from runtime state.",
        "domain": "project-goal-control-plane",
        "repo": str(home),
        "state_file": state_file,
        "status": "connected",
        "adapter": {"kind": "generic_project_goal_v0", "status": "connected"},
        "source_registry": str(registry_path),
    }


def test_bootstrap_separates_home_project_state_from_runtime_goals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LOOPX_RUNTIME_ROOT", raising=False)
    runtime_root = home / ".loopx"
    registry_path = runtime_root / "registry.json"

    result = bootstrap_project(
        project=home,
        registry_path=registry_path,
        runtime_root=None,
        goal_id=GOAL_ID,
        objective="Keep project state separate from runtime state.",
        domain="project-goal-control-plane",
        role="primary",
        parent_goal_id=None,
        state_file=None,
        goal_doc=None,
        adapter_kind="generic_project_goal_v0",
        adapter_status="connected",
        next_probe=None,
        spawn_allowed=False,
        max_children=0,
        allowed_domains=[],
        write_scope=[],
        force=False,
        dry_run=False,
        sync_global=False,
    )

    state_file = home / ".loopx" / "project-goals" / GOAL_ID / "ACTIVE_GOAL_STATE.md"
    assert result["state_file"] == str(state_file)
    assert state_file.is_file()
    assert not (runtime_root / "goals" / GOAL_ID).exists()
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert registry["goals"][0]["state_file"] == (
        f".loopx/project-goals/{GOAL_ID}/ACTIVE_GOAL_STATE.md"
    )


def test_explicit_collocated_runtime_is_used_by_project_registration_and_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "project"
    runtime_root = project / ".loopx"
    registry_path = runtime_root / "registry.json"

    preview = inspect_bootstrap_connection(
        project,
        goal_id=GOAL_ID,
        runtime_root_arg=str(runtime_root),
    )
    expected_state = (
        project / ".loopx" / "project-goals" / GOAL_ID / "ACTIVE_GOAL_STATE.md"
    )
    assert preview["state_file"] == str(expected_state)

    result = register_project_goal(
        registry_path=registry_path,
        runtime_root=runtime_root,
        project_id="home-project",
        project_kind="personal",
        knowledge_root=project,
        goal_id=GOAL_ID,
        objective="Keep project state separate from runtime state.",
        non_goals=[],
        acceptance=["Runtime receipts remain runtime-owned."],
        unknowns=[],
        next_effect="Inspect the project.",
        stop_condition="Stop after registration.",
        repository_bindings=[],
        external_locator_bindings=[],
    )

    assert result["state_file"] == str(expected_state)
    assert expected_state.is_file()
    assert not (runtime_root / "goals" / GOAL_ID).exists()


def test_relative_state_path_rendering_does_not_depend_on_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(home)

    prompt = build_new_project_prompt(
        project=project,
        goal_doc=project / "GOAL.md",
        goal_id=GOAL_ID,
        objective="Keep project state separate from runtime state.",
        domain="project-goal-control-plane",
        adapter_kind="generic_project_goal_v0",
        adapter_status="connected",
        next_probe=None,
        spawn_allowed=False,
        allowed_domains=[],
        write_scope=[],
    )["prompt"]

    expected = f".loopx/goals/{GOAL_ID}/ACTIVE_GOAL_STATE.md"
    assert expected in prompt
    assert default_state_file(GOAL_ID) == expected


def test_uninstall_archives_only_the_state_file_from_legacy_runtime_overlap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LOOPX_RUNTIME_ROOT", raising=False)
    runtime_root = home / ".loopx"
    registry_path = runtime_root / "registry.json"
    global_path = runtime_root / "registry.global.json"
    state_dir = runtime_root / "goals" / GOAL_ID
    state_file = state_dir / "ACTIVE_GOAL_STATE.md"
    runtime_receipt = state_dir / "runtime-receipt.json"
    state_dir.mkdir(parents=True)
    state_file.write_text("# state\n", encoding="utf-8")
    runtime_receipt.write_text("{}\n", encoding="utf-8")
    goal = _goal(
        home,
        registry_path,
        f".loopx/goals/{GOAL_ID}/ACTIVE_GOAL_STATE.md",
    )
    registry = {
        "schema_version": "0.1",
        "registry_role": "project-local",
        "common_runtime_root": str(runtime_root),
        "goals": [goal],
    }
    _write_json(registry_path, registry)
    _write_json(
        global_path,
        {
            "schema_version": "0.1",
            "registry_role": "global-local",
            "common_runtime_root": str(runtime_root),
            "goals": [goal],
        },
    )

    reconnected = bootstrap_project(
        project=home,
        registry_path=registry_path,
        runtime_root=None,
        goal_id=GOAL_ID,
        objective="Keep project state separate from runtime state.",
        domain="project-goal-control-plane",
        role="primary",
        parent_goal_id=None,
        state_file=None,
        goal_doc=None,
        adapter_kind="generic_project_goal_v0",
        adapter_status="connected",
        next_probe=None,
        spawn_allowed=False,
        max_children=0,
        allowed_domains=[],
        write_scope=[],
        force=False,
        dry_run=False,
        sync_global=False,
    )
    assert reconnected["state_file"] == str(state_file)
    assert not (runtime_root / "project-goals" / GOAL_ID).exists()
    state_before_archive = state_file.read_text(encoding="utf-8")

    preview = uninstall_project(
        registry_path=registry_path,
        runtime_root_override=None,
        goal_ids=[GOAL_ID],
        archive_state=True,
        remove_empty_registry=False,
        execute=False,
    )

    assert preview["state_actions"][0]["action"] == "would-move-state-file"
    assert state_file.is_file()
    assert runtime_receipt.is_file()

    result = uninstall_project(
        registry_path=registry_path,
        runtime_root_override=None,
        goal_ids=[GOAL_ID],
        archive_state=True,
        remove_empty_registry=False,
        execute=True,
    )

    action = result["state_actions"][0]
    assert action["action"] == "moved-state-file"
    assert runtime_receipt.is_file(), "runtime-owned state must stay in place"
    assert state_dir.is_dir(), "uninstall must not move the runtime goal directory"
    assert not state_file.exists()
    archived_state = Path(action["archive_path"]) / "ACTIVE_GOAL_STATE.md"
    assert archived_state.read_text(encoding="utf-8") == state_before_archive


def test_uninstall_reports_missing_legacy_state_file_without_moving_runtime_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LOOPX_RUNTIME_ROOT", raising=False)
    runtime_root = home / ".loopx"
    registry_path = runtime_root / "registry.json"
    global_path = runtime_root / "registry.global.json"
    state_dir = runtime_root / "goals" / GOAL_ID
    runtime_receipt = state_dir / "runtime-receipt.json"
    state_dir.mkdir(parents=True)
    runtime_receipt.write_text("{}\n", encoding="utf-8")
    goal = _goal(
        home,
        registry_path,
        f".loopx/goals/{GOAL_ID}/ACTIVE_GOAL_STATE.md",
    )
    registry = {
        "schema_version": "0.1",
        "registry_role": "project-local",
        "common_runtime_root": str(runtime_root),
        "goals": [goal],
    }
    _write_json(registry_path, registry)
    _write_json(
        global_path,
        {
            "schema_version": "0.1",
            "registry_role": "global-local",
            "common_runtime_root": str(runtime_root),
            "goals": [goal],
        },
    )

    preview = uninstall_project(
        registry_path=registry_path,
        runtime_root_override=None,
        goal_ids=[GOAL_ID],
        archive_state=True,
        remove_empty_registry=False,
        execute=False,
    )
    result = uninstall_project(
        registry_path=registry_path,
        runtime_root_override=None,
        goal_ids=[GOAL_ID],
        archive_state=True,
        remove_empty_registry=False,
        execute=True,
    )

    assert preview["state_actions"][0]["action"] == "state-file-missing"
    assert result["state_actions"][0]["action"] == "state-file-missing"
    assert runtime_receipt.is_file()
    assert state_dir.is_dir()
    assert not (runtime_root / "archive").exists()
