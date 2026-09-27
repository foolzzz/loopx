"""Pilot v1 gap N6: an acceptor's reject feedback is not cut at the Turn summary bound.

The Turn summary was bounded at 400 characters, below review_feedback's
600-character contract, so the backend reject lost its last criterion and the
frontend reject ended in stray host glyphs.
"""
from __future__ import annotations

import copy

from loopx.control_plane.todos.contract import TODO_REVIEW_FEEDBACK_LIMIT
from loopx.control_plane.todos.review_feedback import fit_review_feedback, strip_trailing_host_glyphs
from loopx.control_plane.turn_driver import validate_loopx_turn_host_result
from loopx.control_plane.turn_driver.acceptor_verdict import ACCEPTOR_VERDICT_SUMMARY_LIMIT
from loopx.control_plane.turn_driver.codex_cli import codex_cli_result_schema
from loopx.todo_acceptance import reject_delivery_from_turn
from tests.control_plane.test_acceptor_verdicts_g12 import GOAL, _deliver_new_todo, _fixture, _todo
from tests.test_loopx_turn_codex_cli import _request
from tests.test_loopx_turn_executor import _host_result, _plan

PREFIX = "rejected by acc (#1): "
# The shape of the pilot's backend reject: detail first, the documentation criterion last.
PILOT_FEEDBACK = (
    "Failed criterion — overdue flag: GET /todos returns overdue=true for a todo due today because the "
    "comparison uses UTC midnight; compare local dates instead. The validator accepts 'TRUE' and mixed-case "
    "values for the flag, which the contract does not allow. The tests in test_due.py cover only the happy "
    "path and never exercise an invalid date string or a missing due field. The server log prints a stack "
    "trace for a malformed body instead of a 400 response. Failed criterion — documentation: README.md and "
    "the pre-existing CONTRACT.md do not describe the due field or its ISO format. Minor: the helper name "
    "is_overdue_now shadows the module-level function and the docstring still says 'deadline'."
)


def _review(plan_or_request: dict) -> dict:
    reviewed = copy.deepcopy(plan_or_request)
    reviewed["turn_envelope"]["action"]["selected_todo"]["status"] = "in_review"
    return reviewed


def test_review_turns_accept_a_longer_summary_and_other_turns_keep_400() -> None:
    long_summary = "criterion 1 fails: " + "x" * 1_200
    developer = _plan()
    result = _host_result(developer)
    result["summary"] = long_summary
    assert validate_loopx_turn_host_result(developer, result)["ok"] is False

    review = _review(developer)
    result = _host_result(review)
    result["summary"] = long_summary
    validation = validate_loopx_turn_host_result(review, result)
    assert validation["ok"] is True, validation
    assert validation["result"]["summary"] == long_summary

    assert codex_cli_result_schema(_request())["properties"]["summary"]["maxLength"] == 400
    review_schema = codex_cli_result_schema(_review(_request()))
    assert review_schema["properties"]["summary"]["maxLength"] == ACCEPTOR_VERDICT_SUMMARY_LIMIT


def test_fit_uses_the_whole_contract_and_keeps_failed_criteria_first() -> None:
    fitted = fit_review_feedback(PREFIX, PILOT_FEEDBACK)
    assert len(PREFIX + PILOT_FEEDBACK) > TODO_REVIEW_FEEDBACK_LIMIT
    assert TODO_REVIEW_FEEDBACK_LIMIT - 40 <= len(fitted) <= TODO_REVIEW_FEEDBACK_LIMIT
    assert "Failed criterion — overdue flag" in fitted
    assert "Failed criterion — documentation: README.md and the pre-existing CONTRACT.md" in fitted
    assert fitted.endswith("...")
    assert fit_review_feedback(PREFIX, "criterion 2 fails: no pagination") == PREFIX + "criterion 2 fails: no pagination"
    one_word_run = fit_review_feedback(PREFIX, "word " * 400)
    assert len(one_word_run) <= TODO_REVIEW_FEEDBACK_LIMIT and one_word_run.endswith("...")


def test_trailing_host_glyphs_are_stripped_but_real_text_is_kept() -> None:
    assert strip_trailing_host_glyphs("Fix the default using local年／") == "Fix the default using local"
    assert strip_trailing_host_glyphs("criterion 3 fails ─│\x07 ") == "criterion 3 fails"
    assert strip_trailing_host_glyphs("标准 2 未满足：缺少分页") == "标准 2 未满足：缺少分页"
    assert strip_trailing_host_glyphs("café menu is missing") == "café menu is missing"


def test_turn_reject_stores_the_fitted_feedback(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    todo_id, _sha = _deliver_new_todo(fx)
    # The failed criterion comes after 600 characters of detail; a plain cut would lose it.
    details = " ".join(f"Detail {index}: the handler for case {index} logs twice." for index in range(14))
    turn_feedback = details + " Failed criterion — documentation: README.md lacks the due field. 年／"
    assert len(PREFIX + details) > TODO_REVIEW_FEEDBACK_LIMIT
    assert reject_delivery_from_turn(registry_path=fx["registry"], goal_id=GOAL, todo_id=todo_id,
                                     agent_id="acc", feedback=turn_feedback) is True
    todo = _todo(fx, todo_id)
    feedback = todo["review_feedback"]
    assert todo["reject_count"] == 1
    assert feedback.startswith(PREFIX + "Failed criterion — documentation: README.md lacks the due field.")
    assert TODO_REVIEW_FEEDBACK_LIMIT - 40 <= len(feedback) <= TODO_REVIEW_FEEDBACK_LIMIT
    assert "年" not in feedback and feedback.endswith("...")
