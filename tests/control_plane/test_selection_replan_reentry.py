"""A deferred selection must expose a runnable recovery before settlement."""
import json
import shlex

import pytest

from test_quota_settlement_cli import (
    AGENT_ID, GOAL_ID, SELECTED_REPLAN_TODO_ID, TODO_ID,
    _configure_autonomous_replan_fixture, _configure_selected_todo_replan_fixture,
    _configure_selectable_alternative, _heartbeat_receipt_count, _projected_cli_args,
    _run_cli, _run_generated_cli, _spend_run_count, _write_fixture,
)


@pytest.mark.parametrize("binding", ["todo", "autonomous_replan"])
def test_deferred_selection_recovers_same_turn_and_settles_once(tmp_path, binding):
    project, runtime, registry = _write_fixture(tmp_path)
    _configure_selectable_alternative(project)
    turn = "turn-selection-preempted"
    guard = ("quota", "should-run", "--runtime-profile", "generic_cli", "--goal-id", GOAL_ID,
             "--agent-id", AGENT_ID, "--turn-instance-id", turn, "--scan-path", str(project))
    rc, first = _run_cli(registry, runtime, *guard)
    assert rc == 0 and first["decision"] == "run"
    assert first["interaction_contract"]["cli_channel"]["selection_required"]
    assert "settlement_identity" not in first["heartbeat_receipt"]
    if binding == "todo":
        _configure_selected_todo_replan_fixture(project, registry)
        selected_id = SELECTED_REPLAN_TODO_ID
    else:
        _configure_autonomous_replan_fixture(project, runtime, registry)
        selected_id = TODO_ID
    rc, deferred = _run_cli(registry, runtime, *guard, "--todo-id", selected_id)
    assert rc == 1 and not deferred["should_run"]
    selection_deferred = deferred["action_selection_qualification"]["state"] == "deferred"
    if selection_deferred:
        assert deferred["heartbeat_receipt"]["event_id"] != first["heartbeat_receipt"]["event_id"]
        assert deferred["heartbeat_receipt"]["status"] == "selection_retained"
        assert deferred["heartbeat_receipt"]["pending_action_selection"] == {
            "todo_id": selected_id,
            "state": "deferred",
            "reason": deferred["action_selection_qualification"]["reason"],
            "settlement_bound": False,
        }
    else:
        assert deferred["heartbeat_receipt"]["event_id"] == first["heartbeat_receipt"]["event_id"]
        assert deferred["heartbeat_receipt"]["status"] == "replayed"
        assert "pending_action_selection" not in deferred["heartbeat_receipt"]
    expected_before_resume = 2 if selection_deferred else 1
    assert _heartbeat_receipt_count(runtime, turn) == expected_before_resume
    channel = deferred["interaction_contract"]["cli_channel"]
    assert "settlement_plan" not in channel
    assert "replan_settlement_contract" not in channel
    [command] = channel["next_cli_actions"]
    assert "rerun quota should-run" in deferred["recommended_action"]
    assert deferred["execution_obligation"]["reason"] == deferred["recommended_action"]
    assert deferred["interaction_contract"]["agent_channel"]["primary_action"] == command
    rc, envelope = _run_cli(
        registry, runtime, *guard, "--todo-id", selected_id, "--turn-envelope"
    )
    assert rc == 1, envelope
    assert envelope["replan_action_packet"] is None
    assert envelope["action"]["primary_action"].startswith("loopx ")
    assert not envelope["action"]["must_attempt"]
    assert not envelope["action"]["delivery_allowed"]
    [recovery_preview] = envelope["writeback"]["next_cli_actions"]
    assert recovery_preview.startswith("loopx ")
    assert not envelope["writeback"]["spend_after_validation"]
    assert _heartbeat_receipt_count(runtime, turn) == expected_before_resume
    argv = shlex.split(command)
    assert argv[argv.index("--turn-instance-id") + 1] == turn
    assert "--todo-id" not in argv and "--replan-obligation-id" not in argv
    assert "should-run" in argv
    assert argv[argv.index("--runtime-profile") + 1] == "generic_cli"
    assert not deferred["interaction_contract"]["agent_channel"]["must_attempt"]
    rc, resumed = _run_generated_cli(command, registry_path=registry)
    assert rc == 0, resumed
    receipt = resumed["heartbeat_receipt"]
    assert receipt["status"] == "upgraded"
    identity = receipt["settlement_identity"]
    assert identity["turn_instance_id"] == turn
    assert ("todo_id" in identity) == (binding == "todo")
    if selection_deferred:
        assert receipt["pending_action_selection"]["todo_id"] == selected_id
        assert receipt["pending_action_selection"]["settlement_bound"] is (
            binding == "todo"
        )
    else:
        assert "pending_action_selection" not in receipt
    expected_after_resume = expected_before_resume + 1
    assert _heartbeat_receipt_count(runtime, turn) == expected_after_resume
    cli = resumed["interaction_contract"]["cli_channel"]
    assert cli["settlement_plan"]["identity"] == identity
    refresh = next(c for c in cli["next_cli_actions"] if "refresh-state" in c)
    if binding == "todo":
        decision = tmp_path / "selection-replan-vision.json"
        decision.write_text(
            json.dumps(
                {
                    "schema_version": "goal_vision_replan_contract_v0",
                    "state": "vision_patch_proposed",
                    "vision_patch": {
                        "vision_summary": (
                            "Validate the existing bounded slices in dependency order."
                        ),
                        "acceptance_summary": (
                            "Each slice has independent validation before dependent "
                            "work proceeds."
                        ),
                        "advancement_policy": "as_needed",
                    },
                    "path_delta": {
                        "schema_version": "goal_path_delta_v0",
                        "outcome": "replan",
                        "prior_assumption": (
                            "The long chain needed a bounded review."
                        ),
                        "observed_reality": (
                            "The reviewed chain has a runnable validation slice."
                        ),
                        "retained": ["Existing acceptance boundaries"],
                        "changed": ["Proceed with the first validation slice"],
                        "evidence_refs": ["evidence:selection-replan"],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        refresh = refresh.replace(
            "<path-to-evidence-linked-goal-vision-replan-contract-v0.json>",
            str(decision),
        )
    for key, value in {"<advanced|blocked|exploration_exhausted|no_followup>": "advanced",
                       "<surface-id>": "accepted-artifact", "<hypothesis-id>": "adoption",
                       "<probe-kind>": "acceptance", "<evidence-id>": "evidence:readback"}.items():
        refresh = refresh.replace(key, value)
    rc, result = _run_cli(registry, runtime, *_projected_cli_args(refresh, turn_instance_id=turn))
    assert rc == 0, result
    assert result["settlement_result"]["ok"]
    spend = next(c for c in cli["next_cli_actions"] if "spend-slot" in c)
    spend_args = _projected_cli_args(spend, turn_instance_id=turn)
    for replay in (False, True):
        rc, result = _run_cli(registry, runtime, *spend_args, "--scan-path", str(project))
        assert rc == 0, result
        assert result["settlement_result"]["ok"]
        if replay:
            assert result["idempotent_replay"] and not result["appended"]
    assert _spend_run_count(runtime) == 1
    rc, settled = _run_cli(registry, runtime, *guard)
    assert rc == 0
    if binding == "autonomous_replan":
        assert settled["effective_action"] == "heartbeat_settled_skip"
    assert settled["heartbeat_receipt"]["settlement_identity"] == identity
    rc, conflict = _run_cli(registry, runtime, *guard, "--todo-id", "todo_another_selection")
    assert rc == 1 and conflict["ok"] is False
    if binding == "autonomous_replan":
        facts = conflict["action_selection_conflict"]
        assert facts["receipt_replan_obligation_id"] == identity[
            "replan_obligation_id"
        ]
        if selection_deferred:
            assert facts["retained_selection"] is True
            assert facts["retained_selection_todo_id"] == selected_id
        else:
            assert "retained_selection" not in facts
            assert "retained_selection_todo_id" not in facts
            assert facts["qualification_state"] is None
            assert "no pending Todo selection" in conflict["reason"]
            assert "selection the Turn retains" not in conflict["recommended_action"]
    assert _heartbeat_receipt_count(runtime, turn) == expected_after_resume
    assert _spend_run_count(runtime) == 1


def test_reentry_never_replaces_retained_selection_with_recommended_todo(tmp_path):
    project, runtime, registry = _write_fixture(tmp_path)
    _configure_selectable_alternative(project)
    turn = "turn-selection-recommendation-drift"
    guard = ("quota", "should-run", "--runtime-profile", "generic_cli", "--goal-id", GOAL_ID,
             "--agent-id", AGENT_ID, "--turn-instance-id", turn, "--scan-path", str(project))
    rc, first = _run_cli(registry, runtime, *guard)
    assert rc == 0, first
    assert first["interaction_contract"]["cli_channel"]["selection_required"]

    # The original explicit choice leaves the refreshed frontier while a
    # different recommended Todo becomes visible under a hard replan.
    _configure_selected_todo_replan_fixture(project, registry)
    retained_todo_id = "todo_chain_000000000001"
    rc, deferred = _run_cli(
        registry, runtime, *guard, "--todo-id", retained_todo_id
    )
    assert rc == 1, deferred
    assert deferred["action_selection_qualification"]["state"] == "deferred"
    assert deferred["heartbeat_receipt"]["pending_action_selection"]["todo_id"] == retained_todo_id
    [command] = deferred["interaction_contract"]["cli_channel"]["next_cli_actions"]

    rc, resumed = _run_generated_cli(command, registry_path=registry)
    assert rc == 0, resumed
    assert resumed["decision"] == "autonomous_replan_required"
    assert resumed.get("selected_todo") is None
    retained = resumed["retained_action_selection"]
    assert retained["disposition"] == "bind_autonomous_replan"
    assert retained["retained_todo_id"] == retained_todo_id
    assert retained["projected_todo_id"] == SELECTED_REPLAN_TODO_ID
    identity = resumed["heartbeat_receipt"]["settlement_identity"]
    assert identity["binding_kind"] == "autonomous_replan"
    assert "todo_id" not in identity
    assert resumed["heartbeat_receipt"]["pending_action_selection"] == {
        "todo_id": retained_todo_id,
        "state": "deferred_to_fresh_turn",
        "reason": "autonomous_replan_preemption",
        "settlement_bound": False,
    }
    plan = resumed["interaction_contract"]["cli_channel"]["settlement_plan"]
    assert plan["identity"] == identity
    assert all(
        "--replan-obligation-id" in action and "--todo-id" not in action
        for action in resumed["interaction_contract"]["cli_channel"]["next_cli_actions"]
    )
    rc, conflict = _run_cli(
        registry, runtime, *guard, "--todo-id", SELECTED_REPLAN_TODO_ID
    )
    assert rc == 1, conflict
    facts = conflict["action_selection_conflict"]
    assert facts["retained_selection"] is True
    assert facts["retained_selection_todo_id"] == retained_todo_id
    assert facts["receipt_replan_obligation_id"] == identity["replan_obligation_id"]
