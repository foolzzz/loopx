from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from loopx.control_plane.agents.agent_scope_frontier import AgentScopeFrontierAction
from loopx.control_plane.scheduler import scheduler_hint as scheduler_hint_module
from loopx.control_plane.scheduler.execution_context import (
    scheduler_execution_context_for_runtime_profile,
)
from loopx.control_plane.scheduler.scheduler_hint import build_scheduler_hint

GOAL_ID = "scheduler-backoff-convergence"
AGENT_ID = "codex-fixture"
HOST_15 = "FREQ=MINUTELY;INTERVAL=15"
HOST_30 = "FREQ=MINUTELY;INTERVAL=30"
HOST_3 = "FREQ=MINUTELY;INTERVAL=3"
HOST_6 = "FREQ=MINUTELY;INTERVAL=6"
HOST_10 = "FREQ=MINUTELY;INTERVAL=10"
HOST_7 = "FREQ=MINUTELY;INTERVAL=7"
APP_CONTEXT = scheduler_execution_context_for_runtime_profile(
    "codex_app_heartbeat"
)
AGENT_SCOPE_ACTIONS = [action.value for action in AgentScopeFrontierAction]


def _monitor_decision(
    *,
    now: datetime,
    minutes_until_due: int,
    cadence: str = "3m",
    recommended_action: str = "Wait for material monitor evidence.",
) -> dict:
    return {
        "goal_id": GOAL_ID,
        "agent_identity": {"agent_id": AGENT_ID},
        "should_run": False,
        "effective_action": "monitor_quiet_skip",
        "recommended_action": recommended_action,
        "heartbeat_recommendation": {
            "recommended_mode": "monitor_quiet_until_material_transition",
            "notify": "DONT_NOTIFY",
        },
        "interaction_contract": {
            "schema_version": "loopx_interaction_contract_v0",
            "mode": "monitor_quiet_skip",
            "user_channel": {"action_required": False, "notify": "DONT_NOTIFY"},
            "agent_channel": {
                "must_attempt": False,
                "delivery_allowed": False,
                "quiet_noop_allowed": True,
            },
        },
        "agent_todo_summary": {
            "current_agent_claimed_monitor_items": [
                {
                    "todo_id": "todo_scheduler_convergence",
                    "task_class": "continuous_monitor",
                    "target_key": "scheduler-convergence",
                    "cadence": cadence,
                    "next_due_at": (
                        now + timedelta(minutes=minutes_until_due)
                    ).isoformat(),
                    "expires_at": (
                        now + timedelta(minutes=minutes_until_due + 60)
                    ).isoformat(),
                }
            ],
            "monitor_open_items": [],
        },
    }


def _agent_wait_decision() -> dict:
    return {
        "goal_id": GOAL_ID,
        "agent_identity": {"agent_id": AGENT_ID},
        "should_run": False,
        "effective_action": AgentScopeFrontierAction.AGENT_SCOPE_WAIT.value,
        "recommended_action": "Wait for reassignment.",
        "heartbeat_recommendation": {
            "recommended_mode": AgentScopeFrontierAction.AGENT_SCOPE_WAIT.value,
            "notify": "DONT_NOTIFY",
        },
        "interaction_contract": {
            "schema_version": "loopx_interaction_contract_v0",
            "mode": AgentScopeFrontierAction.AGENT_SCOPE_WAIT.value,
            "user_channel": {"action_required": False, "notify": "DONT_NOTIFY"},
            "agent_channel": {
                "must_attempt": False,
                "delivery_allowed": False,
                "quiet_noop_allowed": True,
            },
        },
    }


def _active_decision() -> dict:
    return {
        "goal_id": GOAL_ID,
        "agent_identity": {"agent_id": AGENT_ID},
        "should_run": True,
        "effective_action": "normal_run",
        "recommended_action": "Run bounded delivery work.",
        "heartbeat_recommendation": {
            "recommended_mode": "run_first_read_only_map",
            "notify": "DONT_NOTIFY",
        },
        "interaction_contract": {
            "schema_version": "loopx_interaction_contract_v0",
            "mode": "bounded_delivery",
            "user_channel": {"action_required": False, "notify": "DONT_NOTIFY"},
            "agent_channel": {
                "must_attempt": True,
                "delivery_allowed": True,
                "quiet_noop_allowed": False,
            },
        },
    }


def _capability_bridge_decision() -> dict:
    return {
        "goal_id": GOAL_ID,
        "agent_identity": {"agent_id": AGENT_ID},
        "should_run": True,
        "effective_action": "capability_bridge_repair",
        "recommended_action": "restore the missing network bridge capability",
        "heartbeat_recommendation": {
            "recommended_mode": "repair_capability_bridge",
            "notify": "NOTIFY",
        },
        "interaction_contract": {
            "schema_version": "loopx_interaction_contract_v0",
            "mode": "capability_bridge_repair",
            "user_channel": {
                "action_required": False,
                "notify": "NOTIFY",
                "non_blocking": True,
                "actions": [
                    "Refresh protected network authentication, then confirm access."
                ],
            },
            "agent_channel": {
                "must_attempt": True,
                "delivery_allowed": False,
                "quiet_noop_allowed": False,
            },
        },
    }


def _hint(
    monkeypatch,
    decision: dict,
    *,
    now: datetime,
    host_rrule: str | None = None,
) -> dict:
    monkeypatch.setattr(scheduler_hint_module, "now_utc", lambda: now)
    return build_scheduler_hint(
        decision,
        agent_scope_frontier_actions=AGENT_SCOPE_ACTIONS,
        codex_app_current_rrule=host_rrule,
        scheduler_execution_context=APP_CONTEXT,
    )


CADENCE_POLICY_CASES = [
    {
        "id": "agent_wait_projects_initial_interval",
        "decision": "agent_wait",
        "initial_rrule": "FREQ=MINUTELY;INTERVAL=10",
    },
    {
        "id": "monitor_wait_projects_initial_interval",
        "decision": "monitor_wait",
        "initial_rrule": HOST_15,
    },
    {
        "id": "active_work_projects_initial_interval",
        "decision": "active_work",
        "initial_rrule": HOST_3,
    },
]


@pytest.mark.parametrize(
    "case",
    CADENCE_POLICY_CASES,
    ids=[case["id"] for case in CADENCE_POLICY_CASES],
)
def test_scheduler_hint_cadence_policy_decision_table(
    monkeypatch, case: dict
) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    if case["decision"] == "agent_wait":
        decision = _agent_wait_decision()
    elif case["decision"] == "monitor_wait":
        decision = _monitor_decision(now=now, minutes_until_due=119)
    else:
        decision = _active_decision()

    initial = _hint(monkeypatch, decision, now=now)
    initial_app = initial["codex_app"]
    assert initial_app["recommended_rrule"] == case["initial_rrule"]
    backoff = initial_app["stateful_backoff"]
    assert backoff["state_policy"] == "ephemeral_no_app_scheduler_state"
    assert "example_progression_minutes" not in initial_app
    assert "state_key" not in backoff
    assert "identity_signature" not in backoff
    assert "progression_index" not in backoff
    assert "state_status" not in backoff

    settled = _hint(
        monkeypatch,
        decision,
        now=now + timedelta(minutes=60),
        host_rrule=case["initial_rrule"],
    )
    settled_app = settled["codex_app"]
    assert settled_app["stateful_backoff"]["apply_needed"] is False
    assert "recommended_rrule" not in settled_app


def test_monitor_identity_ignores_recommended_action_text_mutation(
    monkeypatch,
) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    original = _monitor_decision(
        now=now,
        minutes_until_due=119,
        recommended_action="Monitor the post-merge run.",
    )
    first = _hint(monkeypatch, original, now=now)

    mutated = _monitor_decision(
        now=now,
        minutes_until_due=119,
        recommended_action="Controller wording changed; the target did not.",
    )
    second = _hint(
        monkeypatch,
        mutated,
        now=now + timedelta(minutes=15),
        host_rrule=HOST_15,
    )

    assert (
        second["codex_app"]["stateful_backoff"]["reset_token"]
        == first["codex_app"]["stateful_backoff"]["reset_token"]
    )
    assert second["codex_app"]["stateful_backoff"]["apply_needed"] is False
    assert "recommended_rrule" not in second["codex_app"]
    assert "recommended_action" not in second["unchanged_identity_keys"]


def test_monitor_observed_drift_reapplies_profile_initial_interval(
    monkeypatch,
) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    decision = _monitor_decision(now=now, minutes_until_due=119)
    drifted = _hint(
        monkeypatch,
        decision,
        now=now,
        host_rrule=HOST_30,
    )
    app = drifted["codex_app"]
    assert app["recommended_rrule"] == HOST_15
    assert app["stateful_backoff"]["current_rrule"] == HOST_15
    assert app["stateful_backoff"]["apply_needed"] is True
    assert app["stateful_backoff"]["host_observation"]["status"] == "drift_detected"


def test_monitor_near_due_under_floor_uses_tight_cadence(monkeypatch) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    decision = _monitor_decision(now=now, minutes_until_due=7, cadence="30m")
    first = _hint(monkeypatch, decision, now=now)

    first_app = first["codex_app"]
    assert first_app["recommended_rrule"] == HOST_7
    assert first_app["recommended_interval_minutes"] == 7
    assert first["cadence_class"] == "monitor_wait"


def test_monitor_projection_does_not_advance_across_polls(
    monkeypatch,
) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    decision = _monitor_decision(now=now, minutes_until_due=119, cadence="30m")
    first = _hint(monkeypatch, decision, now=now)
    later = _hint(
        monkeypatch,
        decision,
        now=now + timedelta(minutes=60),
        host_rrule=HOST_15,
    )
    assert first["codex_app"]["recommended_rrule"] == HOST_15
    assert later["codex_app"]["stateful_backoff"]["current_rrule"] == HOST_15
    assert later["codex_app"]["stateful_backoff"]["apply_needed"] is False
    assert "recommended_rrule" not in later["codex_app"]


def test_material_work_change_projects_new_initial_interval(
    monkeypatch,
) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    bridge = _capability_bridge_decision()

    first = _hint(monkeypatch, bridge, now=now)
    first_app = first["codex_app"]
    assert first["action"] == "run_now"
    assert first["cadence_class"] == "active_work"
    assert first_app["recommended_rrule"] == HOST_3

    material_work = dict(bridge)
    material_work["effective_action"] = "normal_run"
    material_work["recommended_action"] = "run the matched benchmark arms"
    material_work["interaction_contract"] = {
        **bridge["interaction_contract"],
        "mode": "bounded_delivery",
        "agent_channel": {
            "must_attempt": True,
            "delivery_allowed": True,
            "quiet_noop_allowed": False,
        },
    }
    reset = _hint(
        monkeypatch,
        material_work,
        now=now + timedelta(minutes=3),
        host_rrule=HOST_10,
    )
    reset_app = reset["codex_app"]
    assert reset_app["recommended_rrule"] == HOST_3
    assert reset_app["stateful_backoff"]["state_policy"] == (
        "ephemeral_no_app_scheduler_state"
    )
