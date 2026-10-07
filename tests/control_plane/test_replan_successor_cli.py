"""Real CLI regression coverage retained after live model harness retirement."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vision_closeout_support import _build_fixture, _execute_loopx


@pytest.mark.parametrize(
    ("terminal_args", "expected_error"),
    [
        (
            "--progress-result-class no_followup ",
            "no_followup requires --progress-coverage-scope-id",
        ),
        (
            "--progress-result-class exploration_exhausted ",
            "exploration_exhausted requires --progress-coverage-scope-id",
        ),
        (
            "--progress-result-class exploration_exhausted "
            "--progress-coverage-scope-id coverage-fixture-all-surfaces ",
            "exploration_exhausted requires --progress-coverage-complete",
        ),
    ],
)
def test_terminal_progress_cli_reports_missing_coverage_contract(
    tmp_path: Path,
    terminal_args: str,
    expected_error: str,
) -> None:
    fixture = _build_fixture(tmp_path / "fixture")
    index_path = (
        fixture.runtime_root
        / "goals"
        / "replan-semantic-action-fixture"
        / "runs"
        / "index.jsonl"
    )
    before = index_path.read_text(encoding="utf-8")

    with pytest.raises(RuntimeError, match=expected_error):
        _execute_loopx(
            "loopx --format json --registry ignored --runtime-root ignored "
            "refresh-state --goal-id replan-semantic-action-fixture "
            "--agent-id codex-replan-semantic-action "
            "--progress-scope agent_lane "
            "--classification bounded_replan_terminal "
            f"{terminal_args}"
            "--progress-evidence-id evidence-frontier-inventory "
            "--no-global-sync --suppress-external-sinks",
            fixture=fixture,
            turn_instance_id="invalid-terminal-progress-turn",
        )

    assert index_path.read_text(encoding="utf-8") == before



def test_successor_dry_run_shares_quota_agent_scope_for_user_gates(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path / "fixture")
    other_agent_id = "codex-replan-other-agent"
    registry = json.loads(fixture.global_registry_path.read_text(encoding="utf-8"))
    registry["goals"][0]["coordination"]["registered_agents"].append(
        other_agent_id
    )
    registry["goals"][0]["coordination"]["agent_profiles"][other_agent_id] = {
        "schema_version": "agent_profile_v1",
        "agent_id": other_agent_id,
        "profile_role": "independent-review",
        "scope_summary": "Review an independent bounded delivery.",
        "default_task_classes": ["user_action"],
        "vision_requirement": "optional",
    }
    registry_text = json.dumps(
        registry,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    for registry_path in (
        fixture.global_registry_path,
        fixture.project_root / ".loopx" / "registry.json",
    ):
        registry_path.write_text(registry_text, encoding="utf-8")
    state_path = (
        fixture.project_root
        / ".loopx"
        / "goals"
        / "replan-semantic-action-fixture"
        / "ACTIVE_GOAL_STATE.md"
    )
    state_text = state_path.read_text(encoding="utf-8").replace(
        "## Agent Todo\n",
        (
            "## User Todo / Owner Review Reading Queue\n\n"
            "- [ ] [P0] Review the other agent's independent delivery.\n"
            "  <!-- loopx:todo todo_id=todo_other_agent_review status=open "
            "task_class=user_action "
            "bound_agent=codex-replan-other-agent -->\n\n"
            "## Agent Todo\n"
        ),
    )
    state_path.write_text(state_text, encoding="utf-8")

    quota = json.loads(
        _execute_loopx(
            fixture.quota_guard_command,
            fixture=fixture,
            turn_instance_id="agent-scoped-quota-turn",
        )
    )
    obligation_id = quota["autonomous_replan_obligation"]["obligation_id"]
    before = state_path.read_text(encoding="utf-8")
    dry_run = json.loads(
        _execute_loopx(
            "loopx --format json --registry ignored --runtime-root ignored "
            "todo add --goal-id replan-semantic-action-fixture "
            "--role agent --task-class advancement_task "
            "--action-kind inspect --target-key surface:next-bounded-slice "
            "--text '[P0] Inspect the next bounded surface' "
            "--claimed-by codex-replan-semantic-action "
            f"--replan-obligation-id {obligation_id} --dry-run",
            fixture=fixture,
            turn_instance_id="agent-scoped-successor-turn",
        )
    )

    assert dry_run["ok"] is True
    assert dry_run["dry_run"] is True
    assert dry_run["added"] is True
    assert state_path.read_text(encoding="utf-8") == before



def test_stale_successor_obligation_is_rejected_before_todo_mutation(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path / "fixture")
    state_path = (
        fixture.project_root
        / ".loopx"
        / "goals"
        / "replan-semantic-action-fixture"
        / "ACTIVE_GOAL_STATE.md"
    )
    before = state_path.read_text(encoding="utf-8")

    with pytest.raises(RuntimeError, match="does not match the current open obligation"):
        _execute_loopx(
            "loopx --format json --registry ignored todo add "
            "--goal-id replan-semantic-action-fixture --role agent "
            "--task-class advancement_task "
            "--action-kind validate --target-key surface:stale-target "
            "--text '[P0] Stale successor must not be written' "
            "--claimed-by codex-replan-semantic-action "
            "--replan-obligation-id replan-0000000000000000",
            fixture=fixture,
            turn_instance_id="stale-successor-turn",
        )

    assert state_path.read_text(encoding="utf-8") == before



def test_untyped_successor_is_rejected_before_todo_mutation(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path / "fixture")
    state_path = (
        fixture.project_root
        / ".loopx"
        / "goals"
        / "replan-semantic-action-fixture"
        / "ACTIVE_GOAL_STATE.md"
    )
    quota = json.loads(
        _execute_loopx(
            fixture.quota_guard_command,
            fixture=fixture,
            turn_instance_id="untyped-successor-turn",
        )
    )
    obligation_id = quota["autonomous_replan_obligation"]["obligation_id"]
    before = state_path.read_text(encoding="utf-8")

    with pytest.raises(RuntimeError, match="requires --action-kind"):
        _execute_loopx(
            "loopx --format json --registry ignored todo add "
            "--goal-id replan-semantic-action-fixture --role agent "
            "--task-class advancement_task "
            "--text '[P0] Replan again without an executable target' "
            "--claimed-by codex-replan-semantic-action "
            f"--replan-obligation-id {obligation_id}",
            fixture=fixture,
            turn_instance_id="untyped-successor-turn",
        )

    assert state_path.read_text(encoding="utf-8") == before
