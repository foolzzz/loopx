#!/usr/bin/env python3
"""Smoke-test fail-fast behavior when the shared global registry is not writable."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from loopx.global_registry import sync_project_registry_to_global  # noqa: E402


GOAL_ID = "registry-write-denied-fixture"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_project_registry(path: Path, *, runtime: Path, repo: Path, agents: list[str] | None = None) -> None:
    goal = {
        "id": GOAL_ID,
        "domain": "registry-write-denied-smoke",
        "status": "active",
        "repo": str(repo),
        "state_file": ".loopx/goals/registry-write-denied-fixture/ACTIVE_GOAL_STATE.md",
        "adapter": {"kind": "generic_project_goal_v0", "status": "connected"},
        "coordination": {"registered_agents": agents or []},
    }
    write_json(
        path,
        {
            "schema_version": "0.1",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "common_runtime_root": str(runtime),
            "goals": [goal],
        },
    )


def only_goal(registry_path: Path) -> dict:
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    goals = payload["goals"]
    assert len(goals) == 1, goals
    return goals[0]


def run_cli(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "loopx.cli", "--format", "json", *args],
        cwd=REPO_ROOT,
        check=check,
        text=True,
        capture_output=True,
    )


def payload(result: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(result.stdout)


def make_unwritable(path: Path, *, lock_only: bool = False) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if lock_only:
        lock_file = path / "registry.global.json.lock"
        lock_file.write_text("", encoding="utf-8")
        lock_file.chmod(0o400)
    else:
        path.chmod(0o500)


def restore_writable(path: Path) -> None:
    try:
        path.chmod(0o700)
    except FileNotFoundError:
        pass


def assert_sync_reports_write_denied(root: Path, *, lock_only: bool = False) -> None:
    runtime = root / "runtime"
    project = root / "project"
    registry = project / ".loopx" / "registry.json"
    project.mkdir(parents=True)
    write_project_registry(registry, runtime=runtime, repo=project)
    source_before = registry.read_bytes()
    make_unwritable(runtime, lock_only=lock_only)
    try:
        result = sync_project_registry_to_global(
            registry_path=registry,
            runtime_root_override=None,
            goal_id=GOAL_ID,
            dry_run=False,
        )
    finally:
        restore_writable(runtime)

    assert result["ok"] is False, result
    assert result["write_denied"] is True, result
    assert result["wrote"] is False, result
    assert result["global_registry_writability"]["ok"] is lock_only, result
    assert result["requires_global_registry_repair"] is True, result
    assert registry.read_bytes() == source_before, result
    assert not (runtime / "registry.global.json").exists(), result
    assert not (runtime / "registry.global.json.ts-effect.lock").exists(), result


def assert_connect_fails_without_partial_local_state(root: Path, *, lock_only: bool = False) -> None:
    runtime = root / "runtime"
    project = root / "project"
    project.mkdir(parents=True)
    make_unwritable(runtime, lock_only=lock_only)
    try:
        cli_args = (
            "--registry",
            str(project / ".loopx" / "registry.json"),
            "--runtime-root",
            str(runtime),
            "connect",
            "--project",
            str(project),
            "--goal-id",
            GOAL_ID,
            "--objective",
            "Exercise global registry write-denied preflight.",
        )
        result = run_cli(*cli_args)
    finally:
        restore_writable(runtime)

    data = payload(result)
    assert result.returncode == 1, (result.returncode, data, result.stderr)
    assert data["ok"] is False, data
    assert data["global_sync"]["write_denied"] is True, data
    assert not (project / ".loopx" / "registry.json").exists(), data
    assert not (project / ".loopx" / "goals" / GOAL_ID / "ACTIVE_GOAL_STATE.md").exists(), data
    if lock_only:
        (runtime / "registry.global.json.lock").chmod(0o600)
        recovered = run_cli(*cli_args)
        data = payload(recovered)
        assert recovered.returncode == 0 and data["ok"] is True, data
        assert data["global_sync"]["ok"] is True, data
        assert (project / ".loopx" / "registry.json").exists()
        assert (project / ".loopx" / "goals" / GOAL_ID / "ACTIVE_GOAL_STATE.md").exists()



def assert_register_agent_fails_before_source_write(root: Path, *, lock_only: bool = False) -> None:
    runtime = root / "runtime"
    source_project = root / "source"
    source_registry = source_project / ".loopx" / "registry.json"
    global_registry = runtime / "registry.global.json"
    source_project.mkdir(parents=True)
    runtime.mkdir(parents=True)
    write_project_registry(source_registry, runtime=runtime, repo=source_project, agents=["codex-main-control"])
    global_payload = json.loads(source_registry.read_text(encoding="utf-8"))
    global_payload["registry_role"] = "global-local"
    global_payload["goals"][0]["source_registry"] = str(source_registry)
    write_json(global_registry, global_payload)
    source_before = source_registry.read_bytes()
    global_before = global_registry.read_bytes()

    make_unwritable(runtime, lock_only=lock_only)
    try:
        cli_args = (
            "--runtime-root",
            str(runtime),
            "register-agent",
            "--goal-id",
            GOAL_ID,
            "--agent-id",
            "codex-side-agent",
            "--execute",
        )
        result = run_cli(*cli_args)
    finally:
        restore_writable(runtime)

    data = payload(result)
    assert result.returncode == 1, (result.returncode, data, result.stderr)
    assert data["ok"] is False, data
    assert data["written"] is False, data
    assert data["global_sync"]["write_denied"] is True, data
    assert data["host_loop_activation"]["activated"] is False, data
    assert data["host_loop_activation"]["recommended_action"], data
    assert only_goal(source_registry)["coordination"]["registered_agents"] == ["codex-main-control"]
    assert source_registry.read_bytes() == source_before, data
    assert global_registry.read_bytes() == global_before, data
    if lock_only:
        (runtime / "registry.global.json.lock").chmod(0o600)
        recovered = run_cli(*cli_args)
        data = payload(recovered)
        assert recovered.returncode == 0 and data["ok"] is True, data
        assert data["registration_readback"]["verified"] is True, data
        expected_agents = ["codex-main-control", "codex-side-agent"]
        assert only_goal(source_registry)["coordination"]["registered_agents"] == expected_agents
        assert only_goal(global_registry)["coordination"]["registered_agents"] == expected_agents


def main() -> int:
    if os.name == "nt":
        print("global-registry-writability-smoke skipped on Windows")
        return 0
    with tempfile.TemporaryDirectory(prefix="loopx-global-writability-smoke-") as tmp:
        root = Path(tmp)
        assert_sync_reports_write_denied(root / "sync")
        assert_sync_reports_write_denied(root / "lock-only", lock_only=True)
        assert_connect_fails_without_partial_local_state(root / "connect")
        assert_connect_fails_without_partial_local_state(root / "connect-lock-only", lock_only=True)
        assert_register_agent_fails_before_source_write(root / "register-agent")
        assert_register_agent_fails_before_source_write(root / "register-agent-lock-only", lock_only=True)
    print("global-registry-writability-smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
