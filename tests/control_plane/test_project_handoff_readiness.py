"""Project-asset handoff state and readiness through the status wrappers.

Handoff state anchors on the newest handoff-ready run, falls back to the
project asset's ``latest_validation`` only when no run anchors it, and counts
later non-neutral runs as post-handoff work. Runs without a parseable
``generated_at`` never anchor or count. Readiness requires a project asset and
reports which handoff checks hold.
"""

from __future__ import annotations

import json

from loopx import status as status_module

PROFILE = {"outcome_floor": {"outcome_markers": ["validated"], "surface_only_hints": ["doc_only"]}}
READY_CLASSIFICATION = "controller_opted_in_waiting_for_run"


def _run(generated_at: str, classification: str) -> dict[str, str]:
    return {"generated_at": generated_at, "classification": classification}


def _state(*, ready: bool, runs: list[dict[str, str]], **asset: object) -> dict[str, object]:
    return status_module.project_asset_handoff_state(
        ready=ready,
        project_asset={"execution_profile": PROFILE, **asset},
        latest_runs=runs,
    )


def test_newest_ready_run_anchors_and_later_work_counts() -> None:
    state = _state(
        ready=False,
        runs=[
            _run("2026-07-04T00:04:00+00:00", "custom_delivery"),
            _run("2026-07-04T00:03:00+00:00", "quota_monitor_poll"),
            _run("2026-07-04T00:02:00+00:00", READY_CLASSIFICATION),
            _run("2026-07-04T00:01:00+00:00", "earlier_work"),
            _run("not-a-date", "late_work_with_bad_timestamp"),
        ],
    )

    assert state["handoff_status"] == "post_handoff_run_seen"
    assert state["post_handoff_run_seen"] is True
    assert state["handoff_ready_at"] == "2026-07-04T00:02:00+00:00"
    assert state["handoff_ready_classification"] == READY_CLASSIFICATION
    recent = [run["classification"] for run in state["post_handoff_recent_runs"]]
    assert recent == ["custom_delivery"]
    assert "late_work_with_bad_timestamp" not in json.dumps(state)
    assert "earlier_work" not in json.dumps(state)


def test_bad_timestamp_cannot_anchor_a_handoff() -> None:
    state = _state(ready=False, runs=[_run("not-a-date", READY_CLASSIFICATION)])

    assert state == {"handoff_status": "not_ready", "post_handoff_run_seen": False}


def test_ready_without_anchor_waits_or_takes_newest_custom_run() -> None:
    assert _state(ready=True, runs=[]) == {
        "handoff_status": "ready_waiting_for_run",
        "post_handoff_run_seen": False,
    }

    state = _state(
        ready=True,
        runs=[
            _run("2026-07-04T00:04:00+00:00", "feedback_adapter_slice"),
            _run("2026-07-04T00:05:00+00:00", "custom_small_note"),
        ],
    )
    assert state["handoff_status"] == "post_handoff_run_seen"
    assert state["post_handoff_latest_run"]["classification"] == "custom_small_note"
    assert "handoff_ready_at" not in state


def test_latest_validation_is_post_handoff_work_when_custom() -> None:
    state = _state(
        ready=True,
        runs=[],
        latest_validation=_run("2026-07-04T00:06:00+00:00", "adapter_validated"),
    )

    assert state["handoff_status"] == "post_handoff_run_seen"
    assert state["post_handoff_latest_run"]["classification"] == "adapter_validated"
    assert "handoff_ready_at" not in state


def test_latest_validation_anchors_the_handoff_when_not_custom() -> None:
    validation = _run("2026-07-04T00:06:00+00:00", "inspect_result")

    waiting = _state(ready=True, runs=[_run("2026-07-04T00:05:00+00:00", "quota_monitor_poll")],
                     latest_validation=validation)
    assert waiting["handoff_status"] == "ready_waiting_for_run"
    assert waiting["handoff_ready_at"] == "2026-07-04T00:06:00+00:00"
    assert waiting["handoff_ready_classification"] == "inspect_result"

    neutral_after = _state(ready=True, runs=[_run("2026-07-04T00:07:00+00:00", "quota_monitor_poll")],
                  latest_validation=validation)
    assert neutral_after["handoff_status"] == "ready_waiting_for_run"

    advanced = _state(
        ready=True,
        runs=[_run("2026-07-04T00:07:00+00:00", "inspect_result")],
        latest_validation=validation,
    )
    assert advanced["handoff_status"] == "post_handoff_run_seen"
    assert advanced["post_handoff_latest_run"]["generated_at"] == "2026-07-04T00:07:00+00:00"


def test_readiness_requires_a_project_asset() -> None:
    assert status_module.project_asset_handoff_readiness({"goal_id": "loopx-demo"}) is None
    assert status_module.project_asset_handoff_readiness({"goal_id": "loopx-demo", "project_asset": "x"}) is None


def test_complete_project_asset_is_ready_and_names_the_next_probe() -> None:
    item = {
        "goal_id": "loopx-demo",
        "waiting_on": "codex",
        "recommended_action": "continue the current control-plane slice",
        "project_asset": {
            "next_action": "continue the current control-plane slice",
            "stop_condition": "stop before private material",
            "quota": {"state": "eligible"},
            "execution_profile": PROFILE,
        },
    }
    readiness = status_module.project_asset_handoff_readiness(
        item, latest_runs=[_run("2026-07-04T00:08:00+00:00", "controller_adapter_validated")],
    )

    assert readiness is not None
    assert readiness["ready"] is True
    assert readiness["codex_ready"] is True
    assert readiness["source"] == "project_asset"
    assert readiness["quota_state"] == "eligible"
    assert all(readiness["checks"].values()), readiness["checks"]
    assert readiness["handoff_status"] == "post_handoff_run_seen"
    assert readiness["next_probe"] == "loopx review-packet --goal-id loopx-demo --handoff-only"
    assert readiness["handoff_interface_budget"] == status_module.handoff_budget_contract()


def test_incomplete_project_asset_is_not_ready_and_does_not_trace_state() -> None:
    item = {
        "goal_id": "loopx-demo",
        "waiting_on": "codex",
        "project_asset": {"quota": {"state": "eligible"}, "execution_profile": PROFILE},
    }
    readiness = status_module.project_asset_handoff_readiness(
        item, latest_runs=[_run("2026-07-04T00:08:00+00:00", "custom_delivery")],
    )

    assert readiness is not None
    assert readiness["ready"] is False
    assert readiness["codex_ready"] is True
    assert readiness["checks"]["handoff_has_next_action"] is False
    assert readiness["checks"]["handoff_has_stop_condition"] is False
    assert readiness["checks"]["same_source_should_run"] is False
    assert readiness["handoff_status"] == "not_ready"
    assert readiness["post_handoff_run_seen"] is False
