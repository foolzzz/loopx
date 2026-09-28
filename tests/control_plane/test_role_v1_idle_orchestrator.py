"""Fork decision 39: an idle role_v1 orchestrator is not a self-reported wait.

peer_v1 agents self-report a wait in their Next Action, so a Next Action that
reads as a wait without an open User Todo is a state-projection gap, and
should-run demands a projection repair. Under role_v1 the orchestrator is
event-triggered and idles by design; its Next Action ("Orchestrator idles ...
re-engages only if ... a user gate appears") read as such a wait, and the
dispatcher opened an orchestrator action todo for the repair (E2E pilot gap
G13). role_v1 goals no longer raise that demand; peer_v1 goals still do.

The sibling cadence replan (``periodic_review_due``, raised after a number of
durable runs of any lane and routed to the orchestrator) is not derived for
role_v1 either; stall replans from run history still are.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from loopx.control_plane.agents.runtime_model import AgentRuntimeModel
from loopx.control_plane.goals.goal_frontier.role_v1_cadence import replan_obligation_sources
from loopx.control_plane.quota.projection_repair import build_state_projection_gap
from loopx.control_plane.testing.canary_harness import (
    run_json_cli_result,
    write_fixture_registry,
)
from loopx.control_plane.status.autonomous_replan_projection import (
    AUTONOMOUS_REPLAN_PERIODIC_RUN_THRESHOLD,
)
from loopx.state_projection import state_projection_gap_warning

GOAL_ID = "goal-g13"
ORCH, DEV, ACC = "orch", "dev", "acc"
ROLES = {ORCH: "orchestrator", DEV: "developer", ACC: "acceptor"}
PILOT_STATE = Path(__file__).resolve().parents[1] / "fixtures" / "role_v1_pilot" / "idle_orchestrator_state.md"
IDLE_NEXT_ACTION = (
    "Orchestrator idles. Next eligible work is acc reviewing todo_api (integration check, "
    "in_review). Orchestrator re-engages only if acc rejects it a second time (new "
    "escalation) or a user gate appears."
)
USER_WAIT_KIND = "next_action_waits_without_user_todo"
EXECUTABLE_KIND = "next_action_executable_without_agent_todo"
DONE_API_TODO = (
    "- [x] [P1] Implement the orders API endpoint.\n"
    "  <!-- loopx:todo todo_id=todo_api role=agent task_class=advancement_task "
    "status=done claimed_by=dev required_role=developer "
    "completion_continuation=active_goal -->\n"
)


# --- fixture ----------------------------------------------------------------


def _state(next_action: str, todos: str = DONE_API_TODO) -> str:
    return (
        "---\nstatus: active\n---\n# Goal\n## Objective\nShip the orders feature.\n\n"
        f"## User Todo\n\n## Agent Todo\n\n{todos}\n## Next Action\n\n- {next_action}\n"
    )


def _goal(tmp_path: Path, model: str, state_text: str) -> dict[str, Path]:
    runtime, registry, state = tmp_path / "runtime", tmp_path / "registry.json", tmp_path / "state.md"
    state.write_text(state_text, encoding="utf-8")
    write_fixture_registry(
        project=tmp_path, runtime_root=runtime, registry_path=registry, goal_id=GOAL_ID,
        domain="role-v1", adapter_kind="generic_project_goal_v0", state_file=str(state),
        registered_agents=[ORCH, DEV, ACC], quota_allowed_slots=None,
    )
    payload = json.loads(registry.read_text(encoding="utf-8"))
    coordination = payload["goals"][0]["coordination"]
    coordination["agent_model"] = model
    if model == "role_v1":
        coordination["agent_roles"] = dict(ROLES)
    registry.write_text(json.dumps(payload), encoding="utf-8")
    return {"root": tmp_path, "runtime": runtime, "registry": registry}


def _should_run(goal: dict[str, Path], agent: str) -> dict[str, Any]:
    code, payload = run_json_cli_result(
        "quota", "should-run", "--goal-id", GOAL_ID, "--agent-id", agent,
        "--scan-path", str(goal["root"]),
        registry_path=goal["registry"], runtime_root=goal["runtime"],
    )
    assert code == 0, payload
    return payload


def _assert_no_projection_demand(packet: dict[str, Any]) -> None:
    assert packet["effective_action"] != "state_projection_gap_repair"
    assert (packet.get("stall_self_repair") or {}).get("trigger") != "state_projection_gap"
    assert not packet.get("state_projection_gap")


# --- the demand is computed on read -----------------------------------------


def _persisted_gap(*evidence_kinds: str) -> dict[str, Any]:
    evidence = [
        {"kind": kind, "target_role": "user" if kind == USER_WAIT_KIND else "agent",
         "section": "Next Action",
         "text": IDLE_NEXT_ACTION if kind == USER_WAIT_KIND else "Run the integration tests."}
        for kind in evidence_kinds
    ]
    return {
        "schema_version": "state_projection_gap_v0", "kind": "state_projection_gap",
        "requires_todo_expansion": True,
        "target_roles": sorted({item["target_role"] for item in evidence}),
        "evidence_count": len(evidence), "first_evidence": evidence,
    }


def test_the_idle_next_action_is_recorded_as_a_user_wait() -> None:
    """The upstream prose check itself is unchanged: it still sees a wait."""

    gap = state_projection_gap_warning(_state(IDLE_NEXT_ACTION), user_todos={}, agent_todos={})
    assert gap and [item["kind"] for item in gap["first_evidence"]] == [USER_WAIT_KIND]


@pytest.mark.parametrize(
    ("model", "expected"),
    [(AgentRuntimeModel.ROLE_V1, None), (AgentRuntimeModel.PEER_V1, [USER_WAIT_KIND]), (None, [USER_WAIT_KIND])],
    ids=["role_v1", "peer_v1", "no_goal_model"],
)
def test_a_persisted_user_wait_gap_demands_repair_only_outside_role_v1(
    model: AgentRuntimeModel | None, expected: list[str] | None,
) -> None:
    gap = build_state_projection_gap(
        {"state_projection_gap": _persisted_gap(USER_WAIT_KIND)}, {}, agent_runtime_model=model,
    )
    assert (gap and [item["kind"] for item in gap["first_evidence"]]) == expected


def test_role_v1_drops_the_executable_next_action_evidence_too() -> None:
    """Decision 42 extends decision 39: the executable sibling is dropped as well (pilot v1 gap N2).

    ``tests/control_plane/test_role_v1_stale_next_action_n2.py`` covers it in depth.
    """

    gap = build_state_projection_gap(
        {"state_projection_gap": _persisted_gap(EXECUTABLE_KIND, USER_WAIT_KIND)}, {},
        agent_runtime_model=AgentRuntimeModel.ROLE_V1,
    )
    assert gap is None


# --- should-run ----------------------------------------------------------------


def test_role_v1_idle_orchestrator_demands_no_projection_repair(tmp_path: Path) -> None:
    goal = _goal(tmp_path, "role_v1", _state(IDLE_NEXT_ACTION))
    for agent in (ORCH, DEV, ACC):
        packet = _should_run(goal, agent)
        _assert_no_projection_demand(packet)
        assert packet["effective_action"] == "normal_run"
        assert not packet.get("selected_todo")


@pytest.mark.parametrize(
    "next_action", [IDLE_NEXT_ACTION, "Wait for the owner approval of the release."],
    ids=["idle_orchestrator_prose", "owner_wait"],
)
def test_peer_v1_still_demands_the_projection_repair(tmp_path: Path, next_action: str) -> None:
    # No Todo at all: a done one would raise the peer_v1 no-follow-up replan first.
    goal = _goal(tmp_path, "peer_v1", _state(next_action, todos=""))
    packet = _should_run(goal, DEV)
    assert packet["effective_action"] == "state_projection_gap_repair"
    evidence = packet["state_projection_gap"]["first_evidence"]
    assert [item["kind"] for item in evidence] == [USER_WAIT_KIND]


def test_the_pilot_state_demands_no_projection_repair_under_role_v1(tmp_path: Path) -> None:
    """The E2E pilot's final state (trimmed copy): every agent was sent to repair."""

    state_text = PILOT_STATE.read_text(encoding="utf-8")
    assert "Orchestrator idles." in state_text
    goal = _goal(tmp_path, "role_v1", state_text)
    for agent in (ORCH, DEV, ACC):
        packet = _should_run(goal, agent)
        _assert_no_projection_demand(packet)
        assert packet["effective_action"] == "normal_run"
        assert packet.get("autonomous_replan_obligation") is None


# --- sibling: the cadence replan from run history -------------------------------


def _refresh_dev(goal: dict[str, Path], *extra: str) -> None:
    code, payload = run_json_cli_result(
        "refresh-state", "--goal-id", GOAL_ID, "--agent-id", DEV, "--progress-scope", "agent_lane", *extra,
        registry_path=goal["registry"], runtime_root=goal["runtime"],
    )
    assert code == 0, payload


def _replan_triggers(packet: dict[str, Any]) -> list[str]:
    obligation = packet.get("autonomous_replan_obligation") or {}
    return [str(item.get("kind")) for item in obligation.get("triggers") or []]


def _run_obligation(kind: str) -> dict[str, Any]:
    return {"schema_version": "autonomous_replan_obligation_v0", "required": True, "agent_id": DEV,
            "triggers": [{"kind": kind, "section": "run_history"}]}


@pytest.mark.parametrize("model", [AgentRuntimeModel.ROLE_V1, AgentRuntimeModel.PEER_V1, None])
def test_replan_sources_drop_only_cadence_obligations_under_role_v1(model: AgentRuntimeModel | None) -> None:
    item = {
        "autonomous_replan_obligation": _run_obligation("periodic_review_due"),
        "autonomous_replan_obligations_by_agent": {
            DEV: _run_obligation("periodic_review_due"),
            ACC: _run_obligation("typed_progress_repeat"),
        },
    }
    asset = {"autonomous_replan_obligations_by_agent": {DEV: _run_obligation("periodic_review_due")}}
    selected_item, selected_asset = replan_obligation_sources(item, asset, model)
    if model is not AgentRuntimeModel.ROLE_V1:
        assert (selected_item, selected_asset) == (item, asset)
        return
    assert selected_item == {"autonomous_replan_obligations_by_agent": {ACC: _run_obligation("typed_progress_repeat")}}
    assert selected_asset == {}
    assert "autonomous_replan_obligation" in item  # the sources are not mutated


def test_role_v1_developer_turns_raise_no_periodic_replan_for_the_orchestrator(tmp_path: Path) -> None:
    """20 durable developer runs made the idle orchestrator owe a periodic replan."""

    goal = _goal(tmp_path, "role_v1", _state(IDLE_NEXT_ACTION))
    for _ in range(AUTONOMOUS_REPLAN_PERIODIC_RUN_THRESHOLD):
        _refresh_dev(
            goal, "--classification", "validated_progress", "--delivery-outcome", "outcome_progress",
            "--delivery-batch-scale", "single_surface",
        )
    for agent in (ORCH, DEV):
        packet = _should_run(goal, agent)
        assert packet["effective_action"] == "normal_run", _replan_triggers(packet)
        assert packet.get("autonomous_replan_obligation") is None


def test_role_v1_developer_stall_still_routes_a_replan_to_the_orchestrator(tmp_path: Path) -> None:
    """A stall from run history is a real stuck case: the orchestrator still owes the replan."""

    goal = _goal(tmp_path, "role_v1", _state(IDLE_NEXT_ACTION))
    for _ in range(2):
        _refresh_dev(
            goal, "--classification", "blocked_on_review", "--progress-result-class", "blocked",
            "--progress-blocker-id", "blocker-review", "--progress-evidence-id", "evidence-review",
        )
    packet = _should_run(goal, ORCH)
    assert packet["effective_action"] == "autonomous_replan_required"
    assert _replan_triggers(packet) == ["typed_progress_repeat"]
