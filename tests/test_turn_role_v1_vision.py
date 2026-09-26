"""Fork decision 31: a role_v1 Turn owes no per-agent vision decision.

The E2E pilot's orchestrator Turns failed validation on an invalid
agent_vision_json (gap G4). A role_v1 Turn plan is marked so the host is not
asked for the vision fields and the result validator ignores them; peer_v1
Turns keep the vision contract.
"""

from __future__ import annotations

import json
from typing import Any

from loopx.control_plane.turn_driver.codex_cli import (
    ROLE_V1_NO_VISION_INSTRUCTION,
    _prompt,
)
from loopx.control_plane.turn_driver.executor import (
    build_loopx_turn_host_request,
    mark_turn_plan_vision_checkpoint_not_required,
    validate_loopx_turn_host_result,
)

GOAL_ID = "goal-g4"
ORCH = "orch"

TURN_KEY = "sha256:" + "a" * 64


def _plan(*, role_v1: bool) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "transaction": {"turn_key": TURN_KEY},
        "route": {"kind": "codex_cli", "would_invoke_host": True},
        "turn_envelope": {"goal_id": GOAL_ID, "agent_id": ORCH},
    }
    if role_v1:
        mark_turn_plan_vision_checkpoint_not_required(plan)
    return plan


def _host_result(**overrides: str) -> dict[str, Any]:
    return {
        "schema_version": "loopx_turn_result_v0",
        "turn_key": TURN_KEY,
        "result_kind": "validated_progress",
        "completed_phases": ["host_execute", "typed_result"],
        "classification": "orchestrator_planning",
        "recommended_action": "Wait for the developer delivery.",
        "next_action": "Review the delivered todo when the acceptor verdict lands.",
        "delivery_batch_scale": "single_surface",
        "delivery_outcome": "outcome_progress",
        "summary": "Resolved the escalation by splitting the todo.",
        **overrides,
    }


def test_role_v1_turn_result_owes_no_vision_fields() -> None:
    plain = _host_result(vision_unchanged_reason="", path_delta_mode="", agent_vision_json="")
    role = validate_loopx_turn_host_result(_plan(role_v1=True), plain)
    assert role["ok"], role["errors"]
    assert role["result"]["path_delta_mode"] == "unchanged"
    peer = validate_loopx_turn_host_result(_plan(role_v1=False), plain)
    assert "unchanged path_delta_mode requires vision_unchanged_reason" in peer["errors"]


def test_role_v1_turn_ignores_an_invalid_vision_packet() -> None:
    """The pilot failure: three orchestrator Turns died on invalid agent_vision_json."""

    invalid = _host_result(
        path_delta_mode="material_replan",
        agent_vision_json=json.dumps({"vision_patch": {"advancement_policy": "sometimes"}}),
    )
    role = validate_loopx_turn_host_result(_plan(role_v1=True), invalid)
    assert role["ok"], role["errors"]
    assert "agent_vision" not in role["result"]
    peer = validate_loopx_turn_host_result(_plan(role_v1=False), invalid)
    assert any(error.startswith("invalid agent_vision_json") for error in peer["errors"])
    assert "material_replan path_delta_mode requires result_kind replan_required" in peer["errors"]


def test_role_v1_turn_prompt_does_not_ask_for_a_vision() -> None:
    role_request = build_loopx_turn_host_request(_plan(role_v1=True))
    assert role_request["vision_checkpoint_policy"] == "not_required"
    role_prompt = _prompt(role_request)
    assert ROLE_V1_NO_VISION_INSTRUCTION in role_prompt
    assert "goal_path_delta_v0 in agent_vision_json" not in role_prompt
    assert "provide vision_unchanged_reason" not in role_prompt

    peer_request = build_loopx_turn_host_request(_plan(role_v1=False))
    assert "vision_checkpoint_policy" not in peer_request
    peer_prompt = _prompt(peer_request)
    assert ROLE_V1_NO_VISION_INSTRUCTION not in peer_prompt
    assert "goal_path_delta_v0 in agent_vision_json" in peer_prompt
