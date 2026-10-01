"""The differential harness reproduces from recordings and refuses loose input."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

from loopx_jev.sentinel_compare import (
    COMPARISON_SCHEMA,
    _aggregate,
    compare,
    recording_key,
    recording_transport,
)
from loopx_jev.sentinel_matrix import MAX_CASES, load_sentinel_matrix
from loopx_jev.transport import TransportFailure

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sentinel"


def _matrix_document(**overrides):
    document = json.loads((FIXTURES / "matrix.json").read_text(encoding="utf-8"))
    document.update(overrides)
    return document


def _write(tmp_path: Path, document) -> Path:
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_committed_matrix_loads_with_frozen_gold_labels() -> None:
    matrix = load_sentinel_matrix(FIXTURES / "matrix.json")
    assert len(matrix["cases"]) == 16
    drift = [case for case in matrix["cases"] if case["gold"]["drift_from_round"] is not None]
    assert len(drift) == 9
    assert {case["kind"] for case in matrix["cases"]} == {
        "constructed",
        "real_commit",
        "sanitized_real_commit",
    }
    real = [case for case in matrix["cases"] if case["kind"] != "constructed"]
    assert all(case["provenance"]["repository"] == "loopx-project/loopx" for case in real)
    assert all(case["gold"]["drift_from_round"] is None for case in real)
    sanitized = [case for case in real if case["kind"] == "sanitized_real_commit"]
    assert [case["provenance"]["derivation"] for case in sanitized] == [
        "retired_command_removal"
    ]
    assert all(round_item["self_report"]["result_class"] == "advanced" for case in matrix["cases"] for round_item in case["rounds"])


def test_committed_snapshots_do_not_teach_retired_app_scheduler_commands() -> None:
    retired_commands = {
        "scheduler-ack",
        "scheduler-ack-current",
        "scheduler-fail-current",
    }
    violations = {}
    for path in FIXTURES.rglob("*.txt"):
        content = path.read_text(encoding="utf-8")
        matches = sorted(command for command in retired_commands if command in content)
        if matches:
            violations[str(path.relative_to(FIXTURES))] = matches

    assert violations == {}


def test_committed_constructed_snapshots_match_generator_byte_for_byte(tmp_path: Path) -> None:
    """The frozen replay inputs must stay identical to their authored recipe."""

    generator = runpy.run_path(str(FIXTURES / "build_constructed.py"))["write_snapshots"]
    generator(tmp_path)
    frozen = FIXTURES / "constructed"
    generated_paths = {path.relative_to(tmp_path) for path in tmp_path.rglob("*.txt")}
    frozen_paths = {path.relative_to(frozen) for path in frozen.rglob("*.txt")}
    assert generated_paths == frozen_paths
    assert generated_paths
    for relative_path in sorted(generated_paths):
        assert (tmp_path / relative_path).read_bytes() == (frozen / relative_path).read_bytes(), relative_path


def test_matrix_loader_rejects_loose_input(tmp_path: Path) -> None:
    fixtures_link = tmp_path / "constructed"
    fixtures_link.symlink_to(FIXTURES / "constructed", target_is_directory=True)
    (tmp_path / "real").symlink_to(FIXTURES / "real", target_is_directory=True)
    base = _matrix_document()
    load_sentinel_matrix(_write(tmp_path, base))
    too_many = _matrix_document(cases=base["cases"] + [dict(base["cases"][0], case_id=f"dup-{i}") for i in range(MAX_CASES)])
    with pytest.raises(ValueError, match="at most"):
        load_sentinel_matrix(_write(tmp_path, too_many))
    # Stay within the case budget so the duplicate check, not the size check, fires.
    duplicate = _matrix_document(cases=base["cases"][:15] + [base["cases"][0]])
    with pytest.raises(ValueError, match="duplicate case id"):
        load_sentinel_matrix(_write(tmp_path, duplicate))
    escaped = json.loads(json.dumps(base))
    escaped["cases"][0]["baseline"][escaped["cases"][0]["paths"][0]] = "../outside.txt"
    with pytest.raises(ValueError, match="escapes"):
        load_sentinel_matrix(_write(tmp_path, escaped))
    prose_gold = json.loads(json.dumps(base))
    prose_gold["cases"][0]["gold"]["drift_from_round"] = "soon"
    with pytest.raises(ValueError, match="drift_from_round"):
        load_sentinel_matrix(_write(tmp_path, prose_gold))
    bad_report = json.loads(json.dumps(base))
    bad_report["cases"][0]["rounds"][0]["self_report"]["result_class"] = "looked busy"
    with pytest.raises(ValueError, match="not typed"):
        load_sentinel_matrix(_write(tmp_path, bad_report))
    with pytest.raises(ValueError, match="must use"):
        load_sentinel_matrix(_write(tmp_path, _matrix_document(schema_version="other")))


def test_replay_reproduces_the_committed_live_summary(tmp_path: Path) -> None:
    matrix = load_sentinel_matrix(FIXTURES / "matrix.json")
    expected = json.loads((FIXTURES / "expected_summary.json").read_text(encoding="utf-8"))
    assert expected["current_replay"]["matrix_digest"] == matrix["matrix_digest"], (
        "matrix changed after the replay summary; refresh it"
    )
    comparison = compare(
        matrix,
        responses=FIXTURES / "responses",
        live=False,
        model=expected["model"],
        deadline_ms=5000,
        drift_threshold=2,
    )
    assert comparison["schema_version"] == COMPARISON_SCHEMA
    assert comparison["execution"] == "recorded_replay"
    view = {
        case["case_id"]: {
            "first_flag_round": case["first_flag_round"],
            "first_obligation_round": case["first_obligation_round"],
            "typed_repeat_first_round": case["baseline"]["typed_repeat_first_round"],
            "statuses": [row["status"] for row in case["rounds"]],
        }
        for case in comparison["cases"]
    }
    assert view == expected["deterministic_view"]
    aggregate = comparison["aggregate"]
    assert aggregate["baseline"]["typed_repeat_fired_cases"] == 0
    for signal in ("noul", "choice"):
        results = aggregate["signals"][signal]
        assert results["on_goal_cases_with_false_flag"] == "0/5"
        assert results["on_goal_cases_without_full_verdict"] == 2
        assert results["on_goal_cases_without_full_verdict_with_flag"] == 0
    assert aggregate["signals"] == expected["current_replay"]["aggregate"]["signals"]
    assert aggregate["round_status_counts"] == expected["current_replay"]["aggregate"][
        "round_status_counts"
    ]
    assert all(
        row["execution_kind"] == "recorded_replay"
        for case in comparison["cases"]
        for row in case["rounds"]
        if row["status"] != "not_captured"
    )
    # Replay must not reach the network: a request without a recording fails closed.
    replay = recording_transport(tmp_path / "empty", live=False)
    with pytest.raises(TransportFailure, match="no_recorded_response"):
        replay({"model": "x", "state": {}, "questions": {}}, None, "key")


def test_on_goal_summary_separates_failed_and_partial_evaluations() -> None:
    def case(statuses: list[str], signals: list[bool | None]) -> dict:
        rounds = [
            {
                "status": status,
                "drift_signal": {"noul": signal, "choice": signal},
                "latency_ms": {"assessment_total": None},
                "input_tokens": None,
                "execution_kind": "recorded_replay",
            }
            for status, signal in zip(statuses, signals, strict=True)
        ]
        return {
            "gold": {"drift_from_round": None},
            "rounds": rounds,
            "false_flag_rounds": {
                signal: [index for index, value in enumerate(signals, start=1) if value is True]
                for signal in ("noul", "choice")
            },
            "baseline": {"typed_repeat_first_round": None},
        }

    aggregate = _aggregate(
        [
            case(["completed"], [False]),
            case(["failed"], [None]),
            case(["completed", "failed"], [True, None]),
        ],
        drift_threshold=2,
    )
    assert aggregate["on_goal_cases"] == 3
    for signal in ("noul", "choice"):
        results = aggregate["signals"][signal]
        assert results["on_goal_cases_with_false_flag"] == "0/1"
        assert results["on_goal_cases_without_full_verdict"] == 2
        assert results["on_goal_cases_without_full_verdict_with_flag"] == 1


def test_recording_key_ignores_nothing_but_the_request() -> None:
    request = {"model": "m", "state": {"a": 1}, "questions": {"q": {"type": "noul", "instructions": "i"}}}
    assert recording_key(request) == recording_key(json.loads(json.dumps(request)))
    assert recording_key(request) != recording_key({**request, "model": "n"})


def test_recording_transport_rejects_response_rekeyed_for_another_request(
    tmp_path: Path,
) -> None:
    request = {"model": "m", "state": {}, "questions": {}}
    key = recording_key(request)
    (tmp_path / f"{key}.json").write_text(
        json.dumps(
            {
                "schema": "loopx_jev_recorded_response_v0",
                "request_key": "0" * 64,
                "response": {},
            }
        ),
        encoding="utf-8",
    )

    replay = recording_transport(tmp_path, live=False)
    with pytest.raises(TransportFailure, match="invalid_recorded_response"):
        replay(request, None, "key")
