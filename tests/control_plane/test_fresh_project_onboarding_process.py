"""Fresh-project onboarding through real `python -m loopx.cli` processes.

Regression for the fresh-project defects: `agent-onboard` on a project with no
`.loopx/registry.json` returns a typed identity gate instead of raising, and
the guided Todo template uses `--claimed-by`, which `todo add` accepts. The
chain `agent-onboard -> bootstrap -> todo add -> agent-onboard` runs twice, in
two projects that share one isolated HOME and runtime root, so state from the
first project cannot leak into the second.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from loopx.bootstrap_command_pack import build_start_goal_guided_packet

REPO_ROOT = Path(__file__).resolve().parents[2]


def _cli(env: dict[str, str], *args: str) -> tuple[int, dict[str, Any], str]:
    completed = subprocess.run(
        [sys.executable, "-m", "loopx.cli", "--format", "json", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    output = completed.stdout + completed.stderr
    assert "Traceback" not in output, output[-2000:]
    payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
    return completed.returncode, payload, output


def _register_agent(registry_path: Path, goal_id: str, agent_id: str) -> None:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    goal = next(goal for goal in registry["goals"] if goal.get("id") == goal_id)
    goal.setdefault("coordination", {}).setdefault("registered_agents", []).append(agent_id)
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")


def _onboard_new_project(env: dict[str, str], project: Path, goal_id: str, agent_id: str) -> None:
    # A bare project answers with a typed identity gate, not a crash.
    code, bare, output = _cli(env, "agent-onboard", "--agent-type", "claude-code", "--project", str(project))
    assert code == 0, output
    assert bare["ok"] is True
    assert bare["identity_selection_gate"]["activation_allowed"] is False

    code, _, output = _cli(
        env, "bootstrap", "--project", str(project), "--goal-id", goal_id,
        "--objective", "fresh project onboarding regression",
        "--adapter-kind", "read_only_project_map_v0", "--adapter-status", "connected-read-only",
        "--no-global-sync",
    )
    assert code == 0, output
    registry = project / ".loopx" / "registry.json"
    assert registry.is_file()
    _register_agent(registry, goal_id, agent_id)

    code, seeded, output = _cli(
        env, "--registry", str(registry), "todo", "add", "--goal-id", goal_id, "--role", "agent",
        "--text", "[P0] Implement the first bounded segment.", "--task-class", "advancement_task",
        "--action-kind", "implementation", "--claimed-by", agent_id, "--execute",
    )
    assert code == 0, output
    assert seeded["ok"] is True

    guided = build_start_goal_guided_packet(
        project=project, goal_id=goal_id, agent_id=agent_id, cli_bin="loopx",
        host_surface="claude-code", goal_text="fresh project onboarding regression",
    )
    steps = [step for step in guided["guided_transaction"]["ordered_steps"] if isinstance(step, dict)]
    assert not [step for step in steps if step.get("id") == "write_ordered_todos"]
    (delta_step,) = [step for step in steps if step.get("id") == "apply_todo_delta"]
    assert delta_step["todo_delta"]["schema_version"] == "loopx_guided_todo_delta_v0"
    assert delta_step["todo_delta"]["runnable_frontier_count"] >= 1
    template = delta_step["add_new_command_template"]
    assert "--claimed-by" in template and "--agent-id" not in template

    code, added, output = _cli(
        env, "--registry", str(registry), "todo", "add", "--goal-id", goal_id, "--project", str(project),
        "--role", "agent", "--claimed-by", agent_id, "--task-class", "advancement_task",
        "--action-kind", "verify", "--text", "[P1] Guard the fresh-project regression.",
    )
    assert code == 0, output
    assert added["ok"] is True and added["todo_id"]

    code, connected, output = _cli(env, "agent-onboard", "--agent-type", "claude-code", "--project", str(project))
    assert code == 0, output
    assert connected["ok"] is True
    assert connected["goal_id"] == goal_id


def test_fresh_projects_onboard_through_the_cli_without_leaking_state(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "LOOPX_RUNTIME_ROOT": str(tmp_path / "runtime"),
        "PYTHONPATH": str(REPO_ROOT),
    }
    env.pop("LOOPX_REGISTRY", None)
    for name in ("home", "runtime", "first", "second"):
        (tmp_path / name).mkdir()

    _onboard_new_project(env, tmp_path / "first", "fresh-first", "fresh-agent-1")
    _onboard_new_project(env, tmp_path / "second", "fresh-second", "fresh-agent-2")

    assert not (tmp_path / "first" / ".loopx" / "goals" / "fresh-second").exists()
    assert not (tmp_path / "second" / ".loopx" / "goals" / "fresh-first").exists()
