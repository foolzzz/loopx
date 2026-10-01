"""Typed rejection builds recovery before any settlement capability exists."""
from __future__ import annotations

from copy import deepcopy
import shlex

import pytest

from loopx.cli_commands.quota_action_selection import (
    _requested_quota_action_selection_preflight,
)
from loopx.control_plane.work_items import interaction_contract
from loopx.control_plane.work_items.action_selection_contract import (
    action_selection_needs_recovery,
    bind_action_selection_recovery_command,
)


def _source(state: str) -> dict:
    return {
        "goal_id": "selection-fixture", "agent_identity": {"agent_id": "fixture-agent"},
        "ok": True, "should_run": True, "effective_action": "autonomous_replan",
        "recommended_action": "Handle the preempting replan",
        "execution_obligation": {"must_attempt_work": True, "delivery_allowed": True},
        "action_selection_qualification": {
            "schema_version": "action_selection_qualification_v0", "state": state,
            "requested_todo_id": "todo_pending", "reason": "current_delivery_gate",
            "recovery_action": "reenter_guard_without_selection",
        },
    }


@pytest.mark.parametrize("state", ["deferred", "rejected"])
def test_recovery_never_constructs_settlement_or_executable_primary_action(monkeypatch, state):
    def unexpected(*args, **kwargs):
        pytest.fail("unadmitted selection constructed an executable projection")

    monkeypatch.setattr(interaction_contract, "_turn_scoped_cli_settlement_context", unexpected)
    monkeypatch.setattr(interaction_contract, "build_primary_action_projection", unexpected)
    source = _source(state)
    contract = interaction_contract.build_interaction_contract(
        source, turn_instance_id="selection-turn", runtime_root="/runtime with spaces",
        available_capabilities=["shell"],
    )
    source["interaction_contract"] = contract
    assert contract["agent_channel"]["must_attempt"] is False
    assert contract["agent_channel"]["delivery_allowed"] is False
    cli = contract["cli_channel"]
    assert cli["spend_after_validation"] is False
    assert cli["selection_required"] is False
    for field in ("settlement_plan", "replan_settlement_contract", "selection_command", "selection_policy_ref"):
        assert field not in cli
    bind_action_selection_recovery_command(
        source, registry_path="/registry with spaces.json", runtime_root="/runtime with spaces",
        goal_id="selection-fixture", agent_id="fixture-agent", turn_instance_id="selection-turn",
        scheduler_args=" --runtime-profile generic_cli", available_capabilities=["shell"],
    )
    [command] = cli["next_cli_actions"]
    assert contract["agent_channel"]["primary_action"] == command
    argv = shlex.split(command)
    for flag, value in {"--registry": "/registry with spaces.json", "--runtime-root": "/runtime with spaces",
                        "--goal-id": "selection-fixture", "--agent-id": "fixture-agent",
                        "--turn-instance-id": "selection-turn", "--available-capability": "shell"}.items():
        assert argv[argv.index(flag) + 1] == value
    assert argv[argv.index("--runtime-profile") + 1] == "generic_cli"
    assert "--todo-id" not in argv and "--replan-obligation-id" not in argv


@pytest.mark.parametrize("state", ["deferred", "rejected"])
def test_preflight_returns_one_result_without_mutating_source(state):
    source = _source(state)
    before = deepcopy(source)
    result = _requested_quota_action_selection_preflight(
        source, requested_todo_id="todo_pending", receipt_bound_todo_id=None,
        receipt_bound_replan_obligation_id=None,
    )
    assert source == before
    assert result["error_code"] == f"quota_action_selection_{state}"
    assert result["should_run"] is False
    assert result["execution_obligation"]["kind"] == "quota_skip"
    assert result["execution_obligation"]["must_attempt_work"] is False
    for flag in ("normal_delivery_allowed", "recovery_delivery_allowed", "self_repair_allowed",
                 "capability_repair_allowed", "workspace_repair_allowed", "actionable_by_codex"):
        assert result[flag] is False
    assert result["spend_after_validation"] is False


@pytest.mark.parametrize("bound_field", ["selected_todo", "autonomous_replan_obligation"])
def test_committed_binding_is_owned_by_receipt_reconciliation(bound_field):
    source = _source("deferred")
    source[bound_field] = {"selection_binding": "heartbeat_receipt"}
    assert action_selection_needs_recovery(source) is False


def test_qualified_selection_is_not_recovery():
    assert action_selection_needs_recovery(_source("qualified")) is False


@pytest.mark.parametrize("delivery_allowed", [False, True, None])
def test_pending_workspace_repair_requires_explicit_delivery_refusal(delivery_allowed):
    from loopx.control_plane.quota.error_codes import QuotaActionSelectionConflictError

    source = _source("qualified")
    source.update(
        effective_action="agent_workspace_repair", workspace_repair_allowed=True,
        selected_todo={"todo_id": "todo_pending", "selection_binding": "pending_action_selection"},
        execution_obligation={"kind": "agent_workspace_repair", "must_attempt_work": True},
    )
    agent = {"must_attempt": True}
    if delivery_allowed is not None:
        agent["delivery_allowed"] = delivery_allowed
    source["interaction_contract"] = {"agent_channel": agent}
    kwargs = dict(requested_todo_id="todo_pending", receipt_bound_todo_id=None,
                  receipt_bound_replan_obligation_id=None)
    if delivery_allowed is False:
        assert _requested_quota_action_selection_preflight(source, **kwargs) is None
    else:
        with pytest.raises(QuotaActionSelectionConflictError):
            _requested_quota_action_selection_preflight(source, **kwargs)


def _scoped_gate_status():
    """Reuse the real scoped user-gate fallback producer fixture."""
    import sys

    sys.path.insert(0, "tests/control_plane")
    from test_user_gate_lane_progress import (
        AGENT_ID,
        GENERIC_CONTEXT,
        GOAL_ID,
        _status_payload,
    )

    return _status_payload(gate_action_kind="approve_product_first_screen"), {
        "goal_id": GOAL_ID, "agent_id": AGENT_ID,
        "scheduler_execution_context": GENERIC_CONTEXT,
    }


def test_scoped_fallback_cannot_execute_under_an_unadmitted_selection():
    """A runnable scoped fallback does not survive a refused selection.

    The fallback readback grants safe_bypass_allowed, and the shipped heartbeat
    task body reads that under should_run=false as permission for one bounded
    step plus a spend. The selection refusal has to close that authority too,
    or the same packet says both "no work" and "do one step and spend".
    """
    from loopx.control_plane.quota.turn_envelope import build_turn_envelope
    from loopx.control_plane.turn_driver.host_candidate import extract_turn_authority
    from loopx.presentation.renderers.quota_markdown import render_quota_should_run_markdown
    from loopx.quota import build_quota_should_run

    status, kwargs = _scoped_gate_status()
    payload = build_quota_should_run(
        status, requested_action_todo_id="todo_not_projected", **kwargs,
    )

    assert payload["effective_action"] == "quota_skip"
    assert payload["state"] == "quota_action_selection_rejected"
    for flag in (
        "ok", "should_run", "actionable_by_codex", "safe_bypass_allowed",
        "spend_allowed_now", "spend_after_validation", "normal_delivery_allowed",
        "recovery_delivery_allowed", "self_repair_allowed",
        "capability_repair_allowed", "workspace_repair_allowed",
    ):
        assert payload[flag] is False, flag
    assert payload["safe_bypass_kind"] is None
    assert payload["safe_bypass_policy"] is None
    assert "scoped_user_gate_fallback" not in payload

    obligation = payload["execution_obligation"]
    assert obligation["kind"] == "quota_skip"
    assert obligation["must_attempt_work"] is False
    assert obligation["delivery_allowed"] is False
    assert "no quota spend" in obligation["spend_policy"]

    interaction = payload["interaction_contract"]
    assert interaction["mode"] != "scoped_user_gate_fallback"
    assert interaction["agent_channel"]["must_attempt"] is False
    assert interaction["agent_channel"]["delivery_allowed"] is False
    assert interaction["cli_channel"]["spend_allowed_now"] is False
    assert interaction["cli_channel"]["spend_after_validation"] is False

    assert "protocol_action_packet" not in payload

    envelope = build_turn_envelope(payload)
    assert envelope["writeback"]["spend_allowed_now"] is False
    assert envelope["writeback"]["spend_after_validation"] is False
    capsule = envelope["contract_capsule"]
    assert "protocol_action_packet" not in capsule
    assert capsule["interaction_contract"]["mode"] != "scoped_user_gate_fallback"
    assert capsule["execution_obligation"]["must_attempt_work"] is False
    assert extract_turn_authority({"turn_envelope": envelope})["write_scope"] == []

    guidance = render_quota_should_run_markdown(payload)
    assert "safe_bypass" not in guidance
    assert "spend only after validated writeback" not in guidance


def test_scoped_fallback_still_runs_without_a_refused_selection():
    """The refusal closes the grant; it does not retire the fallback path."""
    from loopx.quota import build_quota_should_run

    status, kwargs = _scoped_gate_status()
    payload = build_quota_should_run(status, **kwargs)

    assert payload["should_run"] is True
    assert payload["safe_bypass_allowed"] is True
    assert payload["safe_bypass_kind"] == "scoped_user_gate_fallback"
    assert payload["interaction_contract"]["mode"] == "scoped_user_gate_fallback"


@pytest.mark.parametrize("state", ["deferred", "rejected"])
def test_recovery_fields_close_the_safe_bypass_grant(state):
    """Both refusal states share one owner, so both close the same authority."""
    from loopx.control_plane.work_items.action_selection_contract import (
        build_action_selection_recovery_fields,
    )

    source = _source(state)
    source.update(
        safe_bypass_allowed=True, safe_bypass_kind="scoped_user_gate_fallback",
        safe_bypass_policy="advance the fallback; spend only after validated writeback",
    )
    recovery = build_action_selection_recovery_fields(source)
    assert recovery["safe_bypass_allowed"] is False
    assert recovery["safe_bypass_kind"] is None
    assert recovery["safe_bypass_policy"] is None
