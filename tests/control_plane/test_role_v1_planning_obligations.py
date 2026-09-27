"""Fork decision 31: role_v1 goals owe no upstream planning obligations.

Under role_v1 the orchestrator reviews the plan every Turn, and developers
and acceptors escalate through orchestrator todos. The upstream per-agent
vision checkpoint and the "completed Todo without a successor or no-follow-up"
replan are therefore not derived for role_v1 goals (E2E pilot gap G4).
peer_v1 goals keep both obligations exactly as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from loopx.control_plane.goals.vision_checkpoint import build_vision_checkpoint
from loopx.control_plane.testing.canary_harness import (
    run_json_cli_result,
    write_fixture_registry,
)
from loopx.control_plane.testing.quota_fixtures import quota_status_payload
from loopx.control_plane.todos.active_state_todo_parser import parse_active_state_todos
from loopx.quota import build_quota_should_run

GOAL_ID = "goal-g4"
ORCH, DEV, ACC = "orch", "dev", "acc"
ROLES = {ORCH: "orchestrator", DEV: "developer", ACC: "acceptor"}
VISION_GAP_KINDS = {
    "vision_checkpoint_missing",
    "vision_outcome_checkpoint_required",
    "vision_acceptance_gap",
    "required_agent_vision_missing",
}
SUCCESSION_TRIGGER = "completed_advancement_without_successor"
PLAN_TODO = (
    "- [ ] [P1] Plan the orders feature.\n"
    "  <!-- loopx:todo todo_id=todo_plan role=agent task_class=advancement_task "
    "status=open claimed_by=orch action_kind=plan -->\n"
)
API_TODO = (
    "- [ ] [P1] Implement the orders API endpoint.\n"
    "  <!-- loopx:todo todo_id=todo_api role=agent task_class=advancement_task "
    "status=open claimed_by=dev -->\n"
)


# --- fixture ----------------------------------------------------------------


def _set_model(registry: Path, model: str) -> None:
    payload = json.loads(registry.read_text(encoding="utf-8"))
    coordination = payload["goals"][0]["coordination"]
    coordination["agent_model"] = model
    if model == "role_v1":
        coordination["agent_roles"] = dict(ROLES)
    else:
        coordination.pop("agent_roles", None)
    registry.write_text(json.dumps(payload), encoding="utf-8")


def _goal(tmp_path: Path, model: str, todos: str) -> dict[str, Path]:
    runtime, registry, state = tmp_path / "runtime", tmp_path / "registry.json", tmp_path / "state.md"
    state.write_text(
        "---\nstatus: active\n---\n# Goal\n## Objective\nShip the orders feature.\n\n"
        f"## Agent Todo\n{todos}",
        encoding="utf-8",
    )
    write_fixture_registry(
        project=tmp_path, runtime_root=runtime, registry_path=registry, goal_id=GOAL_ID,
        domain="role-v1", adapter_kind="generic_project_goal_v0", state_file=str(state),
        registered_agents=[ORCH, DEV, ACC], quota_allowed_slots=None,
    )
    _set_model(registry, model)
    return {"root": tmp_path, "runtime": runtime, "registry": registry}


def _cli(goal: dict[str, Path], *args: str) -> dict[str, Any]:
    code, payload = run_json_cli_result(
        *args, registry_path=goal["registry"], runtime_root=goal["runtime"],
    )
    assert code == 0, payload
    return payload


def _complete(goal: dict[str, Path], todo_id: str, agent: str) -> None:
    _cli(
        goal, "todo", "complete", "--goal-id", GOAL_ID, "--todo-id", todo_id,
        "--claimed-by", agent, "--agent-id", agent, "--evidence", "work validated",
    )


def _material_refresh(goal: dict[str, Path], agent: str) -> dict[str, Any]:
    """A material closeout without a vision decision, as a Turn settlement writes it."""

    return _cli(
        goal, "refresh-state", "--goal-id", GOAL_ID, "--classification", "validated_progress",
        "--agent-id", agent, "--delivery-outcome", "outcome_progress",
        "--delivery-batch-scale", "single_surface",
    )


def _should_run(goal: dict[str, Path], agent: str) -> dict[str, Any]:
    return _cli(
        goal, "quota", "should-run", "--goal-id", GOAL_ID, "--agent-id", agent,
        "--scan-path", str(goal["root"]),
    )


def _obligations(packet: dict[str, Any]) -> tuple[list[str], list[str]]:
    obligation = packet.get("autonomous_replan_obligation") or {}
    triggers = [str(item.get("kind")) for item in obligation.get("triggers") or []]
    gaps = [
        str(gap.get("kind"))
        for gap in (packet.get("goal_frontier_projection") or {}).get("acceptance_gaps") or []
    ]
    return triggers, gaps


def _assert_no_planning_obligation(packet: dict[str, Any]) -> None:
    triggers, gaps = _obligations(packet)
    assert packet.get("autonomous_replan_obligation") is None, triggers
    assert packet.get("effective_action") != "autonomous_replan_required"
    assert packet.get("replan_action_packet") is None
    assert not VISION_GAP_KINDS & set(gaps), gaps


# --- the orchestrator's own planning todo ------------------------------------


def test_role_v1_orchestrator_planning_completion_leaves_no_obligation(tmp_path: Path) -> None:
    goal = _goal(tmp_path, "role_v1", PLAN_TODO + API_TODO)
    _complete(goal, "todo_plan", ORCH)
    refreshed = _material_refresh(goal, ORCH)
    checkpoint = refreshed["vision_checkpoint"]
    assert (checkpoint["decision"], checkpoint["required"], checkpoint["satisfied"]) == (
        "not_required", False, True,
    )
    for agent in (ORCH, DEV, ACC):
        _assert_no_planning_obligation(_should_run(goal, agent))


def test_role_v1_orchestrator_without_follow_up_is_not_asked_to_replan(tmp_path: Path) -> None:
    """The planning todo was the last advancement work: no no-follow-up replan."""

    goal = _goal(tmp_path, "role_v1", PLAN_TODO)
    _complete(goal, "todo_plan", ORCH)
    packet = _should_run(goal, ORCH)
    _assert_no_planning_obligation(packet)
    assert packet["effective_action"] == "normal_run"


@pytest.mark.parametrize(
    ("todos", "expected_trigger", "expected_gap"),
    [
        (PLAN_TODO, SUCCESSION_TRIGGER, None),
        (PLAN_TODO + API_TODO, "vision_checkpoint_missing", "vision_checkpoint_missing"),
    ],
    ids=["no_follow_up", "vision_checkpoint"],
)
def test_peer_v1_orchestrator_completion_still_raises_the_obligation(
    tmp_path: Path, todos: str, expected_trigger: str, expected_gap: str | None,
) -> None:
    goal = _goal(tmp_path, "peer_v1", todos)
    _complete(goal, "todo_plan", ORCH)
    if expected_gap:
        checkpoint = _material_refresh(goal, ORCH)["vision_checkpoint"]
        assert (checkpoint["decision"], checkpoint["required"]) == ("missing_required", True)
    packet = _should_run(goal, ORCH)
    triggers, gaps = _obligations(packet)
    assert packet["effective_action"] == "autonomous_replan_required"
    assert expected_trigger in triggers
    if expected_gap:
        assert expected_gap in gaps


# --- goals that already carry an obligation ----------------------------------


@pytest.mark.parametrize("todos", [PLAN_TODO, PLAN_TODO + API_TODO], ids=["no_follow_up", "vision_checkpoint"])
def test_an_existing_obligation_no_longer_blocks_once_the_goal_is_role_v1(
    tmp_path: Path, todos: str,
) -> None:
    """Obligations are derived, not stored: a role_v1 goal no longer derives them."""

    goal = _goal(tmp_path, "peer_v1", todos)
    _complete(goal, "todo_plan", ORCH)
    if API_TODO in todos:
        _material_refresh(goal, ORCH)
    assert _should_run(goal, ORCH)["effective_action"] == "autonomous_replan_required"

    _set_model(goal["registry"], "role_v1")
    for agent in (ORCH, DEV, ACC):
        _assert_no_planning_obligation(_should_run(goal, agent))


def _closed_goal_with_missing_checkpoint(coordination: dict[str, Any]) -> dict[str, Any]:
    parsed = parse_active_state_todos(
        "## User Todo\n\n## Agent Todo\n\n"
        "- [x] [P0] Plan the orders feature.\n"
        "  <!-- loopx:todo todo_id=todo_plan status=done task_class=advancement_task "
        "claimed_by=orch no_followup=true evidence=plan%20applied -->\n"
    )
    return quota_status_payload(
        goal_id=GOAL_ID, status="active", recommended_action="Resolve the vision checkpoint.",
        user_todos=parsed["user_todos"], agent_todos=parsed["agent_todos"],
        coordination=coordination,
        latest_runs=[{
            "classification": "validated_progress",
            "generated_at": "2026-09-26T00:00:00+00:00",
            "progress_scope": "goal",
            "agent_id": ORCH,
            "vision_checkpoint": {
                "schema_version": "vision_checkpoint_v0", "agent_id": ORCH, "required": True,
                "satisfied": False, "decision": "missing_required", "missing_baseline": True,
                "triggers": [{"kind": "material_delivery_outcome"}, {"kind": "durable_next_action_update"}],
                "required_resolution": ["write_vision_patch"],
            },
        }],
    )


def test_a_persisted_missing_checkpoint_is_ignored_for_role_v1() -> None:
    """The pilot's state: a missing checkpoint recorded before this change."""

    role_v1 = {"agent_model": "role_v1", "registered_agents": [ORCH, DEV, ACC], "agent_roles": ROLES}
    decision = build_quota_should_run(
        _closed_goal_with_missing_checkpoint(role_v1), goal_id=GOAL_ID, agent_id=ORCH,
    )
    _assert_no_planning_obligation(decision)

    peer_v1 = {"agent_model": "peer_v1", "registered_agents": [ORCH, DEV, ACC]}
    decision = build_quota_should_run(
        _closed_goal_with_missing_checkpoint(peer_v1), goal_id=GOAL_ID, agent_id=ORCH,
    )
    assert decision["effective_action"] == "autonomous_replan_required"
    assert "vision_checkpoint_missing" in _obligations(decision)[1]


# --- developer and acceptor completions ---------------------------------------


@pytest.mark.parametrize(
    ("agent", "role_args"),
    [
        (DEV, ["--required-role", "developer", "--requires-acceptance", "false"]),
        (ACC, ["--required-role", "acceptor"]),
    ],
    ids=["developer", "acceptor"],
)
@pytest.mark.parametrize("model", ["role_v1", "peer_v1"])
def test_developer_and_acceptor_completions(
    tmp_path: Path, agent: str, role_args: list[str], model: str,
) -> None:
    goal = _goal(tmp_path, model, "")
    added = _cli(
        goal, "todo", "add", "--goal-id", GOAL_ID, "--role", "agent",
        "--text", f"Bounded {agent} work", "--task-class", "advancement_task",
        "--claimed-by", agent, *(role_args if model == "role_v1" else []),
    )
    _complete(goal, str(added["todo_id"]), agent)
    if model == "role_v1":
        assert _material_refresh(goal, agent)["vision_checkpoint"]["decision"] == "not_required"
        for reader in (ORCH, DEV, ACC):
            _assert_no_planning_obligation(_should_run(goal, reader))
        return
    # peer_v1 regression: the completing lane owes a successor or no-follow-up
    # decision, and that open obligation fences its next writeback.
    packet = _should_run(goal, agent)
    assert packet["effective_action"] == "autonomous_replan_required"
    assert SUCCESSION_TRIGGER in _obligations(packet)[0]


# --- the vision checkpoint transition (TypeScript owner) -----------------------


def _checkpoint(*, required: bool, unchanged: str | None = None) -> dict[str, Any]:
    return build_vision_checkpoint(
        agent_id=ORCH, agent_vision=None, existing_agent_vision=None,
        vision_unchanged_reason=unchanged, delivery_outcome="outcome_progress",
        active_state_next_action_update={"would_update": True}, checkpoint_required=required,
    )


def test_checkpoint_policy_not_required_never_records_a_missing_decision() -> None:
    peer = _checkpoint(required=True)
    assert (peer["decision"], peer["required"], peer["satisfied"]) == ("missing_required", True, False)
    assert "policy" not in peer

    role = _checkpoint(required=False)
    assert (role["decision"], role["required"], role["satisfied"]) == ("not_required", False, True)
    assert role["policy"] == "not_required"
    assert [trigger["kind"] for trigger in role["triggers"]] == [
        "material_delivery_outcome", "durable_next_action_update",
    ]
    assert "required_resolution" not in role

    # The pilot's host result: an unchanged reason without a persisted baseline.
    role = _checkpoint(required=False, unchanged="Routine continuation of the plan todo.")
    assert role["decision"] == "not_required"
    assert "missing_baseline" not in role
    peer = _checkpoint(required=True, unchanged="Routine continuation of the plan todo.")
    assert (peer["decision"], peer["missing_baseline"]) == ("missing_required", True)
