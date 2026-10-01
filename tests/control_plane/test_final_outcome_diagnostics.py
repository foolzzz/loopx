"""One final-outcome diagnosis survives public consumers without granting closure."""

from copy import deepcopy
import json

import pytest

from loopx.cli import main as cli_main
from loopx.control_plane.goals.acceptance_observation import (
    build_goal_acceptance_observation,
)
from loopx.control_plane.goals.goal_frontier.outcome_continuity import (
    acceptance_gaps_from_outcome_checkpoint,
)
from loopx.control_plane.quota.cli_projection import (
    compact_quota_should_run_cli_payload,
)
from loopx.control_plane.quota.turn_envelope import build_turn_envelope
from loopx.control_plane.scheduler.execution_context import (
    scheduler_execution_context_for_runtime_profile,
)
from loopx.control_plane.testing.quota_fixtures import (
    quota_status_payload,
    quota_todo_item,
)
from loopx.extensions.lark.presentation.projection_rows import (
    projection_rows_from_payload,
)
from loopx.quota import build_quota_should_run, render_quota_should_run_markdown

AGENT = "outcome-agent"
GOAL = "outcome-fixture"
STAMP = "2026-01-01T00:00:00Z"


def material_run():
    return {
        "goal_id": GOAL,
        "agent_id": AGENT,
        "generated_at": STAMP,
        "classification": "bounded_outcome_progress",
        "progress_scope": "agent_lane",
        "delivery_outcome": "outcome_progress",
        "agent_vision": {
            "agent_id": AGENT,
            "state": "active",
            "generated_at": STAMP,
            "vision_patch": {"vision_summary": "Verify the final outcome."},
            "path_delta": {
                "outcome": "continue",
                "evidence_refs": ["result:verified-milestone"],
                "prior_assumption": "The selected path is suitable.",
                "observed_reality": "Verified milestone supports the selected path.",
                "retained": ["Continue the selected path."],
            },
        },
        "vision_checkpoint": {
            "agent_id": AGENT,
            "satisfied": True,
            "decision": "patched",
            "generated_at": STAMP,
            "triggers": [
                {
                    "kind": "material_delivery_outcome",
                    "delivery_outcome": "outcome_progress",
                }
            ],
        },
    }


def decision():
    status = quota_status_payload(
        goal_id=GOAL,
        status="active",
        recommended_action="Continue scoped verification.",
        agent_todo_items=[
            quota_todo_item(
                todo_id="todo_verify",
                text="Continue scoped verification.",
                task_class="advancement_task",
                action_kind="implement",
                claimed_by=AGENT,
            )
        ],
        claim_scope_agent_id=AGENT,
        latest_runs=[material_run()],
        coordination={"agent_model": "peer_v1", "registered_agents": [AGENT]},
    )
    return build_quota_should_run(
        status,
        goal_id=GOAL,
        agent_id=AGENT,
        scheduler_execution_context=scheduler_execution_context_for_runtime_profile(
            "generic_cli"
        ),
    )


@pytest.mark.parametrize(
    "failure", ["stale", "unsatisfied", "path", "evidence", "outcome_gap"]
)
def test_other_failed_components_are_not_misdiagnosed_as_claim_only(failure):
    run = material_run()
    vision, checkpoint = run["agent_vision"], run["vision_checkpoint"]
    if failure == "stale":
        checkpoint["generated_at"] = "2025-12-31T00:00:00Z"
    elif failure == "unsatisfied":
        checkpoint["satisfied"] = False
    elif failure == "path":
        vision["path_delta"]["outcome"] = "wait"
    elif failure == "evidence":
        vision["path_delta"]["evidence_refs"] = []
    else:
        checkpoint["triggers"][0]["delivery_outcome"] = "outcome_gap"
    gap = acceptance_gaps_from_outcome_checkpoint(vision, checkpoint)[0]
    assert gap["reason_code"] == "final_outcome_checkpoint_incomplete"
    assert sum(not passed for passed in gap["component_checks"].values()) == 2


@pytest.mark.parametrize("claim", [None, "", "   "])
def test_blank_claims_are_missing_and_restoring_claim_clears_only_this_guard(claim):
    run = material_run()
    vision, checkpoint = run["agent_vision"], run["vision_checkpoint"]
    vision["vision_patch"]["acceptance_summary"] = claim
    assert (
        acceptance_gaps_from_outcome_checkpoint(vision, checkpoint)[0]["reason_code"]
        == "final_outcome_claim_missing"
    )
    vision["vision_patch"]["acceptance_summary"] = (
        "Final outcome verified by result:verified-milestone."
    )
    assert acceptance_gaps_from_outcome_checkpoint(vision, checkpoint) == []


def test_same_diagnosis_reaches_cli_managed_turn_frontend_and_lark():
    guard = decision()
    assert guard["goal_frontier_projection"]["replan_required"] is True
    audit = guard["vision_continuation_audit"]
    diagnostic = audit["outcome_checkpoint_diagnostics"][0]
    assert diagnostic["reason_code"] == "final_outcome_claim_missing"
    assert audit["closeout_allowed_without_evidence"] is False
    trigger = guard["autonomous_replan_obligation"]["triggers"][0]
    assert all(trigger[key] == value for key, value in diagnostic.items())
    assert (
        "acceptance_summary"
        in guard["autonomous_replan_obligation"]["recommended_action"]
    )
    compact = compact_quota_should_run_cli_payload(guard)
    assert compact["vision_continuation_audit"]["outcome_checkpoint_diagnostics"] == [
        diagnostic
    ]
    markdown = render_quota_should_run_markdown(compact)
    assert diagnostic["reason_code"] in markdown
    assert "final_outcome_claim_present=fail" in markdown
    assert diagnostic["resolution_hint"] in markdown
    envelope = build_turn_envelope(guard)
    assert envelope["contract_capsule"]["vision_continuation_audit"][
        "outcome_checkpoint_diagnostics"
    ] == [diagnostic]
    observation = build_goal_acceptance_observation(
        {"id": GOAL, "latest_runs": [material_run()]},
        {},
    )
    gap = next(row for row in observation["acceptance_gaps"] if row.get("reason_code"))
    assert all(gap[key] == value for key, value in diagnostic.items())
    _, rows, warnings = projection_rows_from_payload(
        guard,
        goal_id=GOAL,
        agent_id=AGENT,
        source_id="quota",
        include_done=False,
        limit=20,
    )
    assert warnings == []
    row = next(row for row in rows if row["action_kind"] == "outcome_checkpoint")
    assert all(row[key] == value for key, value in diagnostic.items())
    assert "final_outcome_claim_present=fail" in row["evidence"]
    _, peer_rows, _ = projection_rows_from_payload(
        guard,
        goal_id=GOAL,
        agent_id="other-agent",
        source_id="quota",
        include_done=False,
        limit=20,
    )
    assert not any(row["action_kind"] == "outcome_checkpoint" for row in peer_rows)


def test_public_cli_diagnostic_reentry_preserves_guard_identity_without_spending(
    tmp_path, capsys
):
    project, runtime = tmp_path / "project", tmp_path / "runtime"
    project.mkdir()
    state = project / "ACTIVE_GOAL_STATE.md"
    state.write_text(
        "# Active Goal\n\n## Agent Todo\n\n- [ ] Continue verification.\n"
        f"  <!-- loopx:todo todo_id=todo_verify status=open task_class=advancement_task claimed_by={AGENT} -->\n"
    )
    registry = project / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "goals": [
                    {
                        "id": GOAL,
                        "status": "active",
                        "domain": "software",
                        "repo": str(project),
                        "state_file": state.name,
                        "adapter": {
                            "kind": "harness_self_improvement",
                            "status": "connected-read-only",
                        },
                        "coordination": {
                            "agent_model": "peer_v1",
                            "registered_agents": [AGENT],
                        },
                    }
                ]
            }
        )
    )
    runs = runtime / "goals" / GOAL / "runs" / "index.jsonl"
    runs.parent.mkdir(parents=True)
    runs.write_text(json.dumps(material_run()) + "\n")
    before = deepcopy((state.read_text(), runs.read_text()))
    results = []
    for _ in range(2):
        assert (
            cli_main(
                [
                    "--registry",
                    str(registry),
                    "--runtime-root",
                    str(runtime),
                    "--format",
                    "json",
                    "quota",
                    "should-run",
                    "--goal-id",
                    GOAL,
                    "--agent-id",
                    AGENT,
                    "--runtime-profile",
                    "generic_cli",
                    "--turn-instance-id",
                    "claim-diagnostic-turn",
                ]
            )
            == 0
        )
        result = json.loads(capsys.readouterr().out)
        assert result["goal_frontier_projection"]["replan_required"] is True
        assert (
            result["vision_continuation_audit"]["outcome_checkpoint_diagnostics"][0][
                "reason_code"
            ]
            == "final_outcome_claim_missing"
        )
        results.append(result)
    assert (
        results[0]["autonomous_replan_obligation"]["obligation_id"]
        == results[1]["autonomous_replan_obligation"]["obligation_id"]
    )
    assert results[0]["quota"] == results[1]["quota"]
    assert (state.read_text(), runs.read_text()) == before

    # Follow the diagnosis through the actual write boundary, retaining the
    # valid route and refs while obeying the existing durable-field replan gate.
    from loopx.state_refresh import refresh_state_run

    corrected = material_run()["agent_vision"]
    corrected["vision_patch"]["acceptance_summary"] = (
        "Final outcome verified by result:verified-milestone."
    )
    corrected.pop("generated_at")
    corrected["path_delta"]["outcome"] = "replan"
    corrected["path_delta"]["observed_reality"] = (
        "The route and evidence are valid; add the missing final-outcome claim."
    )
    written = refresh_state_run(
        registry_path=registry,
        runtime_root_override=str(runtime),
        goal_id=GOAL,
        project=project,
        state_file=state,
        agent_id=AGENT,
        classification="bounded_outcome_progress",
        recommended_action="Continue verification.",
        delivery_outcome="outcome_progress",
        delivery_batch_scale="multi_surface",
        agent_vision_packet=corrected,
        autonomous_replan_recorded=True,
        dry_run=False,
        sync_global=False,
    )
    assert written["ok"] is True
    assert written["vision_checkpoint"]["satisfied"] is True
    assert written["agent_vision"]["path_delta"]["outcome"] == "replan"
    assert written["agent_vision"]["path_delta"]["evidence_refs"] == [
        "result:verified-milestone"
    ]
