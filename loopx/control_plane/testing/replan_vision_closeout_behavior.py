"""Provider-neutral required-vision scenario acceptance contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .model_behavior_qualification import model_behavior_semantic_contract_from_packet


def required_vision_scenario_contract(
    source_packet: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the source scenario and bind its full closeout acceptance."""
    semantics = model_behavior_semantic_contract_from_packet(
        source_packet, arm="full_packet"
    )
    vision = semantics["vision_continuation"]
    trigger_kinds = set(vision.get("trigger_kinds", []))
    required = {
        "selected_todo_id": None,
        "user_action_required": False,
        "must_attempt_work": True,
        "quiet_noop_allowed": False,
    }
    if any(contract.get(field) != value for field, value in required.items()):
        raise ValueError("required-vision scenario must execute before quiet wait")
    if (
        vision.get("required") is not True
        or "required_agent_vision_missing" not in trigger_kinds
    ):
        raise ValueError("required-vision scenario must preserve the profile gap")
    if semantics["required_reads"]:
        raise ValueError("required-vision replan must not require a model read ritual")
    action_packet = source_packet.get("replan_action_packet")
    obligation = source_packet.get("autonomous_replan_obligation")
    if not (
        isinstance(action_packet, Mapping)
        and isinstance(obligation, Mapping)
        and action_packet.get("decision") == "replan_required"
        and action_packet.get("obligation_id") == obligation.get("obligation_id")
        and dict(obligation.get("replan_context") or {}).get("delivery")
        == "host_projected"
    ):
        raise ValueError(
            "required-vision scenario must preserve host-delivered replan context"
        )
    if semantics["scheduler_action"].get("action") != "run_now":
        raise ValueError("required-vision scenario must remain immediately runnable")
    return {
        "qualification_scope": "required_vision_closeout",
        "trigger_kinds": sorted({item["kind"] for item in obligation["triggers"]}),
        "required_semantic_outcomes": list(
            action_packet["uncovered_frontier"]["required_any_of"]
        ),
        "vision_closeout": {
            "checkpoint_satisfied": True,
            "bound_writeback": True,
            "settled": True,
            "spend_count": 1,
            "original_obligation_closed": True,
        },
    }
