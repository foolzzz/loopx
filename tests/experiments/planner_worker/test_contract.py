from __future__ import annotations

import json

import pytest

from loopx.experiments.planner_worker.contract import (
    MAX_PLANNER_WORKER_STEPS,
    PLANNER_WORKER_PLAN_SCHEMA_VERSION,
    PLANNER_WORKER_STEP_SCHEMA_VERSION,
    build_planner_prompt,
    build_worker_step_prompt,
    parse_planner_worker_plan_text,
    planner_worker_plan_json_skeleton,
    resolve_planner_worker_executor,
    select_next_executable_step,
)


def valid_plan(*, executor: str = "cheap_worker") -> dict:
    tier = {
        "cheap_worker": "cheap",
        "strong_worker": "strong",
        "planner_only": "none",
    }[executor]
    return {
        "schema_version": PLANNER_WORKER_PLAN_SCHEMA_VERSION,
        "plan_id": "plan-1",
        "objective": "Update one fixture.",
        "steps": [
            {
                "schema_version": PLANNER_WORKER_STEP_SCHEMA_VERSION,
                "step_id": "edit-fixture",
                "planner_order": 1,
                "role": "worker",
                "target_files": ["result.txt"],
                "action_kind": "edit",
                "recommended_executor": executor,
                "worker_model_tier": tier,
                "worker_autonomy": "bounded",
                "worker_ready": True,
                "worker_blockers": [],
                "context_budget": {
                    "max_files": 1,
                    "max_bytes_per_file": 4096,
                    "allow_extra_files": False,
                },
                "research_summary": "The fixture contains one stale value.",
                "implementation_notes": "Replace the stale value.",
                "instruction": "Write the expected value to result.txt.",
                "depends_on": [],
                "validation_commands": ["python3 verify.py"],
                "done_criteria": ["verify.py exits zero"],
                "escalation_policy": "Stop if result.txt is outside the workspace.",
                "verification": "Run python3 verify.py.",
            }
        ],
    }


def parse(plan: dict) -> dict:
    return parse_planner_worker_plan_text(json.dumps(plan))


def test_model_output_requires_one_strict_json_contract() -> None:
    plan = valid_plan()

    assert parse(plan) == plan

    with pytest.raises(ValueError, match="single JSON object"):
        parse_planner_worker_plan_text(f"```json\n{json.dumps(plan)}\n```")
    with pytest.raises(ValueError, match="single JSON object"):
        parse_planner_worker_plan_text(f"plan follows\n{json.dumps(plan)}")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda plan: plan.update(schema_version="wrong"), "schema_version"),
        (
            lambda plan: plan["steps"].append(dict(plan["steps"][0])),
            "step_id must be unique",
        ),
        (
            lambda plan: plan["steps"][0].update(depends_on=["missing"]),
            "unknown dependency",
        ),
        (
            lambda plan: plan["steps"][0].update(depends_on=["edit-fixture"]),
            "dependency cycle",
        ),
        (
            lambda plan: plan["steps"][0].update(recommended_executor="invented"),
            "recommended_executor",
        ),
        (
            lambda plan: plan["steps"][0].update(worker_model_tier="invented"),
            "worker_model_tier",
        ),
        (
            lambda plan: plan["steps"][0]["context_budget"].update(max_files=-1),
            "max_files",
        ),
    ],
)
def test_model_output_rejects_illegal_states(mutate, message: str) -> None:
    plan = valid_plan()
    mutate(plan)

    with pytest.raises(ValueError, match=message):
        parse(plan)


def test_model_output_rejects_more_steps_than_declared_limit() -> None:
    plan = valid_plan()
    plan["steps"] = [
        {
            **plan["steps"][0],
            "step_id": f"step-{index}",
            "planner_order": index,
            "depends_on": [],
        }
        for index in range(1, 10)
    ]

    with pytest.raises(ValueError, match="at most 8 steps"):
        parse(plan)


def test_model_output_rejects_redundant_step_status() -> None:
    plan = valid_plan()
    plan["steps"][0]["status"] = "blocked"

    with pytest.raises(ValueError, match="unknown fields: status"):
        parse(plan)


def test_selector_and_router_make_the_contract_control_execution() -> None:
    plan = parse(valid_plan())
    selection = select_next_executable_step(plan, completed_step_ids=[])

    assert selection["status"] == "selected"
    assert selection["step"]["step_id"] == "edit-fixture"
    route = resolve_planner_worker_executor(
        selection["step"],
        model_routes={
            "cheap_worker": {"model": "deepseek-v4-flash", "effort": "medium"},
            "strong_worker": {"model": "gpt-5.5", "effort": "high"},
        },
    )
    assert route == {
        "executor": "cheap_worker",
        "model": "deepseek-v4-flash",
        "effort": "medium",
    }


@pytest.mark.parametrize(
    ("change", "expected_status"),
    [
        ({"worker_ready": False}, "blocked"),
        ({"worker_ready": False, "worker_blockers": ["missing symbol"]}, "blocked"),
        ({"recommended_executor": "planner_only", "worker_model_tier": "none"}, "planner_required"),
        ({"depends_on": ["inspect"]}, "waiting_dependencies"),
    ],
)
def test_selector_does_not_launch_an_ineligible_worker(change: dict, expected_status: str) -> None:
    plan = valid_plan()
    if "depends_on" in change:
        inspect = {
            **plan["steps"][0],
            "step_id": "inspect",
            "planner_order": 2,
            "recommended_executor": "planner_only",
            "worker_model_tier": "none",
        }
        plan["steps"][0]["planner_order"] = 1
        plan["steps"][0].update(change)
        plan["steps"].insert(0, inspect)
        completed = []
    else:
        plan["steps"][0].update(change)
        completed = []

    selection = select_next_executable_step(parse(plan), completed_step_ids=completed)

    assert selection["status"] == expected_status
    assert selection["step"]["step_id"] in {"edit-fixture", "inspect"}


def test_selector_skips_ineligible_step_for_later_independent_work() -> None:
    plan = valid_plan()
    plan["steps"][0]["worker_ready"] = False
    later = {
        **valid_plan(executor="strong_worker")["steps"][0],
        "step_id": "independent-ready-step",
        "planner_order": 2,
    }
    plan["steps"].append(later)

    selection = select_next_executable_step(parse(plan), completed_step_ids=[])

    assert selection["status"] == "selected"
    assert selection["step"]["step_id"] == "independent-ready-step"


def _skeleton_plan() -> dict:
    plan = planner_worker_plan_json_skeleton()
    plan["plan_id"] = "contract-skeleton"
    plan["objective"] = "Update one fixture."
    step = plan["steps"][0]
    step["step_id"] = "update-fixture"
    step["target_files"] = ["fixture.txt"]
    step["instruction"] = "Write expected to fixture.txt."
    return plan


def test_the_skeleton_the_planner_is_shown_parses_selects_and_routes() -> None:
    parsed = parse_planner_worker_plan_text(json.dumps(_skeleton_plan()))
    selection = select_next_executable_step(parsed, completed_step_ids=[])

    assert selection["status"] == "selected"
    assert selection["step"]["step_id"] == "update-fixture"
    route = resolve_planner_worker_executor(
        selection["step"],
        model_routes={"cheap_worker": {"model": "deepseek-v4-flash", "effort": "medium"}},
    )
    assert route["model"] == "deepseek-v4-flash"


@pytest.mark.parametrize("max_steps", [0, MAX_PLANNER_WORKER_STEPS + 1])
def test_skeleton_rejects_step_budgets_outside_the_contract(max_steps: int) -> None:
    with pytest.raises(ValueError, match="max_steps must be between 1 and"):
        planner_worker_plan_json_skeleton(max_steps=max_steps)


def test_planner_prompt_demands_strict_json_in_the_skeleton_schema() -> None:
    prompt = build_planner_prompt(objective="Update one fixture.", task_instruction="Return a bounded plan.")

    assert "Do not edit files. Return one worker-ready plan as strict JSON." in prompt
    assert "Every step must name exact target files" in prompt
    assert json.dumps(planner_worker_plan_json_skeleton(), ensure_ascii=False, indent=2) in prompt
    assert prompt.endswith("Objective:\nUpdate one fixture.\n\nTask instruction:\nReturn a bounded plan.")
    with pytest.raises(ValueError):
        build_planner_prompt(objective=" ", task_instruction="Return a bounded plan.")


def test_worker_prompt_scopes_execution_to_the_selected_step() -> None:
    plan = parse_planner_worker_plan_text(json.dumps(_skeleton_plan()))
    prompt = build_worker_step_prompt(plan=plan, step=plan["steps"][0])

    assert "Execute only this selected plan step. Do not re-plan the whole task." in prompt
    assert "Target files: fixture.txt" in prompt
    assert "- python3 -m pytest -q tests/test_target.py" in prompt
    with pytest.raises(ValueError, match="step_id not found in plan"):
        build_worker_step_prompt(plan=plan, step={"step_id": "unknown-step"})
