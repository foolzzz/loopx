"""Decision 42 (E2E pilot v1 gap N2): a stale Next Action never wakes a role_v1 orchestrator.

Under role_v1 the goal's Next Action is written by whichever Turn settled
last, typically the acceptor ("Settle todo_X as accepted; no developer repair
is required."). Once no agent todo was open, the upstream state-projection
check read that text as executable work without an Agent Todo
(``next_action_executable_without_agent_todo``), should-run demanded
``state_projection_gap_repair`` from every agent, and the dispatcher turned it
into an orchestrator action todo, a Fable Turn and a closure gate that
repeated the push approval. role_v1 goals no longer derive that demand
(decision 39 dropped the user-wait half; this drops the executable half);
the dispatcher's deterministic goal_complete gate marks the end of the work
instead. peer_v1 goals are unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from loopx.control_plane.agents.runtime_model import (
    AgentRuntimeModel,
    next_action_executable_demands_agent_todo,
)
from loopx.control_plane.quota.projection_repair import (
    build_state_projection_gap,
    revalidate_state_projection_gap,
)
from loopx.control_plane.testing.canary_harness import run_json_cli_result, write_fixture_registry
from loopx.state_projection import state_projection_gap_warning

GOAL_ID = "goal-n2"
ORCH, DEV, ACC = "orch", "dev", "acc"
ROLES = {ORCH: "orchestrator", DEV: "developer", ACC: "acceptor"}
PILOT_STATE = Path(__file__).resolve().parents[1] / "fixtures" / "role_v1_pilot" / "finished_goal_state.md"
STALE_NEXT_ACTION = "Settle todo_ee9cf116b96c as accepted; no developer repair is required."
EXECUTABLE_KIND = "next_action_executable_without_agent_todo"
USER_WAIT_KIND = "next_action_waits_without_user_todo"


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
        "quota", "should-run", "--goal-id", GOAL_ID, "--agent-id", agent, "--scan-path", str(goal["root"]),
        registry_path=goal["registry"], runtime_root=goal["runtime"],
    )
    assert code == 0, payload
    return payload


def _gap(*kinds: str) -> dict[str, Any]:
    evidence = [
        {"kind": kind, "target_role": "user" if kind == USER_WAIT_KIND else "agent", "section": "Next Action",
         "text": "Wait for the owner approval." if kind == USER_WAIT_KIND else STALE_NEXT_ACTION}
        for kind in kinds
    ]
    return {"schema_version": "state_projection_gap_v0", "kind": "state_projection_gap",
            "requires_todo_expansion": True, "target_roles": sorted({item["target_role"] for item in evidence}),
            "evidence_count": len(evidence), "first_evidence": evidence}


# --- the demand is re-derived on read ------------------------------------------------


def test_the_stale_acceptor_next_action_is_still_recorded_as_executable() -> None:
    """The upstream prose check itself is unchanged; only the demand is gated."""

    gap = state_projection_gap_warning(PILOT_STATE.read_text(encoding="utf-8"), user_todos={}, agent_todos={})
    assert gap and [item["kind"] for item in gap["first_evidence"]] == [EXECUTABLE_KIND]
    assert gap["first_evidence"][0]["text"] == STALE_NEXT_ACTION


@pytest.mark.parametrize(
    ("model", "demands"),
    [(AgentRuntimeModel.ROLE_V1, False), (AgentRuntimeModel.PEER_V1, True), (None, True)],
    ids=["role_v1", "peer_v1", "no_goal_model"],
)
def test_only_role_v1_drops_the_executable_evidence(model: AgentRuntimeModel | None, demands: bool) -> None:
    assert next_action_executable_demands_agent_todo(model) is demands
    gap = build_state_projection_gap({"state_projection_gap": _gap(EXECUTABLE_KIND)}, {}, agent_runtime_model=model)
    if demands:
        assert gap is not None and [item["kind"] for item in gap["first_evidence"]] == [EXECUTABLE_KIND]
    else:
        assert gap is None, "a gap persisted before this change is dropped on read as well"


def test_a_gap_with_other_evidence_keeps_it() -> None:
    """Evidence the gate does not own stays (a future kind, or a peer caller's)."""

    other = {"kind": "some_other_gap", "target_role": "agent", "section": "Next Action", "text": "x"}
    gap = _gap(EXECUTABLE_KIND)
    gap["first_evidence"].append(other)
    gap["evidence_count"] = 2
    revised = revalidate_state_projection_gap(gap, user_wait_demand=False, agent_executable_demand=False)
    assert revised is not None and revised["first_evidence"] == [other] and revised["evidence_count"] == 1
    assert revalidate_state_projection_gap(gap) is gap, "the default (peer_v1) keeps every item"


# --- should-run on the pilot's finished goal ------------------------------------------------


def test_the_pilot_state_after_push_approval_demands_nothing_from_any_role(tmp_path: Path) -> None:
    """Trimmed copy of the pilot state after the push approve (before N2 fired)."""

    state_text = PILOT_STATE.read_text(encoding="utf-8")
    assert f"- {STALE_NEXT_ACTION}" in state_text and "status=open" not in state_text
    goal = _goal(tmp_path, "role_v1", state_text)
    for agent in (ORCH, DEV, ACC):
        packet = _should_run(goal, agent)
        assert packet["effective_action"] == "normal_run", (agent, packet["effective_action"])
        assert (packet.get("stall_self_repair") or {}).get("trigger") != "state_projection_gap"
        assert not packet.get("state_projection_gap")
        assert packet.get("autonomous_replan_obligation") is None
        assert not packet.get("selected_todo"), "no role has work, so the dispatcher launches nothing"


def test_peer_v1_still_demands_the_repair_for_the_same_prose(tmp_path: Path) -> None:
    state_text = (
        "---\nstatus: active\n---\n# Goal\n## Objective\nShip it.\n\n## User Todo\n\n## Agent Todo\n\n"
        f"## Next Action\n\n- {STALE_NEXT_ACTION}\n"
    )
    goal = _goal(tmp_path, "peer_v1", state_text)
    packet = _should_run(goal, DEV)
    assert packet["effective_action"] == "state_projection_gap_repair"
    assert [item["kind"] for item in packet["state_projection_gap"]["first_evidence"]] == [EXECUTABLE_KIND]
