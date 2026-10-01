import ast
import inspect
import json
from pathlib import Path

from loopx.control_plane.quota import slot_accounting


ROOT = Path(__file__).resolve().parents[2]
RETIRED_SETTLEMENT_HELPER = "_latest_unspent_accountable_delivery_run"
CURRENT_SETTLEMENT_HELPER = "_latest_unspent_turn_settlement_run"
COURSE_DOC = (
    "docs/development/control-plane-course/08-evidence-refresh-and-self-repair.md"
)
SETTLEMENT_DOCS = (
    "docs/development/bugs/settlement-infer-persisted-break.md",
    COURSE_DOC,
)


def test_maintained_docs_do_not_reference_retired_settlement_helper() -> None:
    docs_root = ROOT / "docs"
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in docs_root.rglob("*.md")
        if "archive" not in path.relative_to(docs_root).parts
        and RETIRED_SETTLEMENT_HELPER in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_settlement_docs_reference_current_helper() -> None:
    for relative_path in SETTLEMENT_DOCS:
        content = (ROOT / relative_path).read_text(encoding="utf-8")

        assert CURRENT_SETTLEMENT_HELPER in content
        assert RETIRED_SETTLEMENT_HELPER not in content
    assert callable(slot_accounting._latest_unspent_turn_settlement_run)


def test_unspent_candidate_accepts_typed_gap_until_matching_spend(
    tmp_path: Path,
) -> None:
    goal_id = "settlement-note-fixture"
    effect_id = "effect-settlement-note"
    index_path = tmp_path / "goals" / goal_id / "runs" / "index.jsonl"
    index_path.parent.mkdir(parents=True)
    typed_gap = {
        "goal_id": goal_id,
        "agent_id": "agent-note",
        "todo_id": "todo_note",
        "delivery_outcome": "outcome_gap",
        "settlement_identity": {"effect_id": effect_id},
        "progress_observation": {
            "schema_version": "typed_progress_observation_v0",
            "result_class": "blocked",
            "work_item_id": "todo_note",
            "blocker_id": "blocker-note",
            "evidence_ids": ["evidence-note"],
        },
    }
    index_path.write_text(json.dumps(typed_gap) + "\n", encoding="utf-8")

    selected = slot_accounting._latest_unspent_turn_settlement_run(
        tmp_path,
        goal_id,
        agent_id="agent-note",
        settlement_effect_id=effect_id,
    )
    assert selected == typed_gap

    spent = {
        "goal_id": goal_id,
        "agent_id": "agent-note",
        "classification": "quota_slot_spent",
        "effect_ref": f"{effect_id}#quota_spend",
    }
    index_path.write_text(
        "".join(json.dumps(record) + "\n" for record in (typed_gap, spent)),
        encoding="utf-8",
    )
    assert (
        slot_accounting._latest_unspent_turn_settlement_run(
            tmp_path,
            goal_id,
            agent_id="agent-note",
            settlement_effect_id=effect_id,
        )
        is None
    )


def test_course_spend_excerpt_preserves_turn_settlement_binding() -> None:
    content = (ROOT / COURSE_DOC).read_text(encoding="utf-8")
    call_start = content.index(
        f"delivery_completion_run = {CURRENT_SETTLEMENT_HELPER}("
    )
    call_end = content.index("\ndelivery_workspace = (", call_start)
    course_module = ast.parse(content[call_start:call_end])
    assert len(course_module.body) == 1
    helper_assignment = course_module.body[0]
    assert isinstance(helper_assignment, ast.Assign)
    assert len(helper_assignment.targets) == 1
    assert isinstance(helper_assignment.targets[0], ast.Name)
    assert helper_assignment.targets[0].id == "delivery_completion_run"
    assert isinstance(helper_assignment.value, ast.Call)
    assert isinstance(helper_assignment.value.func, ast.Name)
    assert helper_assignment.value.func.id == CURRENT_SETTLEMENT_HELPER
    course_keywords = {
        keyword.arg: keyword.value
        for keyword in helper_assignment.value.keywords
        if keyword.arg is not None
    }
    production_module = ast.parse(
        inspect.getsource(slot_accounting.build_quota_slot_preview_for_decision)
    )
    production_calls = [
        node
        for node in ast.walk(production_module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == CURRENT_SETTLEMENT_HELPER
    ]
    assert len(production_calls) == 1
    production_keywords = {
        keyword.arg: keyword.value
        for keyword in production_calls[0].keywords
        if keyword.arg is not None
    }
    production_binding = production_keywords["settlement_effect_id"]

    assert any(
        isinstance(node, ast.Name) and node.id == "effect_ref"
        for node in ast.walk(production_binding)
    )
    assert ast.dump(course_keywords["settlement_effect_id"]) == ast.dump(
        production_binding
    )
