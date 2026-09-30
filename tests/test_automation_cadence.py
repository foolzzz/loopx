"""Real CLI policy readback and App scheduling projections; no live automation mutation."""

from pathlib import Path

from examples.control_plane.quota_plan_fixtures import (
    SCOPED_AGENT_ID,
    write_cli_fixture,
)
from loopx.control_plane.testing.canary_harness import run_json_cli_result
from loopx.control_plane.scheduler.scheduler_hint import build_scheduler_hint
from loopx.control_plane.scheduler.execution_context import (
    scheduler_execution_context_for_runtime_profile,
)


def _payload(floor: int | None = None):
    result = {
        "goal_id": "fixture",
        "agent_identity": {"agent_id": "agent"},
        "should_run": True,
        "effective_action": "normal_run",
        "execution_obligation": {"must_attempt_work": True},
    }
    if floor is not None:
        result["automation_cadence"] = {
            "min_interval_minutes": floor,
            "configuration_revision": 2,
            "enforcement": "scheduler_recommendation",
            "sources": [],
        }
    return result


def test_codex_app_daily_floor_survives_reset_and_retains_honest_guarantee():
    for action in ("normal_run", "monitor_quiet_skip", "user_gate_blocked"):
        source = _payload(1440)
        source["effective_action"] = action
        hint = build_scheduler_hint(
            source,
            scheduler_execution_context=scheduler_execution_context_for_runtime_profile(
                "codex_app_heartbeat"
            ),
            include_detail=True,
            codex_app_current_rrule="FREQ=MINUTELY;INTERVAL=3",
        )
        app = hint["codex_app"]
        assert app["recommended_interval_minutes"] >= 1440
        assert app["recommended_rrule"] == "FREQ=MINUTELY;INTERVAL=1440"
        assert app["stateful_backoff"]["apply_needed"] is True
        assert hint["reset_policy"]["app_automation_initial_interval_minutes"] >= 1440
        assert app["guarantee"]["pre_model_atomic_admission"] == "not_qualified"
        assert app["guarantee"]["model_wakeup_tokens_prevented"] is False
        assert "fallback_hint" not in app
    off = build_scheduler_hint(
        _payload(),
        scheduler_execution_context=scheduler_execution_context_for_runtime_profile(
            "codex_app_heartbeat"
        ),
        include_detail=True,
    )["codex_app"]
    assert "execution_interval_policy" not in off
    assert "guarantee" not in off
    matched = build_scheduler_hint(
        _payload(1440),
        scheduler_execution_context=scheduler_execution_context_for_runtime_profile(
            "codex_app_heartbeat"
        ),
        codex_app_current_rrule="FREQ=MINUTELY;INTERVAL=1440",
    )["codex_app"]
    assert matched["stateful_backoff"]["apply_needed"] is False


def test_policy_configured_by_real_cli_reaches_quota_app_hint(tmp_path: Path):
    registry, runtime, project = write_cli_fixture(
        tmp_path / "fixture", scoped_agents=True
    )
    kwargs = dict(registry_path=registry, runtime_root=runtime, cwd=project)
    common = [
        "automation-cadence",
        "--goal-id",
        "needs-operator",
        "--agent-id",
        SCOPED_AGENT_ID,
    ]
    code, original = run_json_cli_result(*common, **kwargs)
    assert code == 0 and original["configuration_revision"] == 0
    code, configured = run_json_cli_result(
        *common,
        "--min-interval-minutes",
        "1440",
        "--expected-revision",
        "0",
        "--owner-reference",
        "fixture-owner-instruction",
        "--execute",
        **kwargs,
    )
    assert code == 0 and configured["written"] is True
    code, reread = run_json_cli_result(*common, **kwargs)
    assert code == 0 and reread["min_interval_minutes"] == 1440
    code, rejected = run_json_cli_result(
        *common,
        "--min-interval-minutes",
        "3",
        "--expected-revision",
        "1",
        "--owner-reference",
        "fixture",
        "--execute",
        **kwargs,
    )
    assert code != 0
    code, guard = run_json_cli_result(
        "quota",
        "should-run",
        "--goal-id",
        "needs-operator",
        "--agent-id",
        SCOPED_AGENT_ID,
        "--codex-app",
        "--turn-instance-id",
        "cadence-fixture",
        **kwargs,
    )
    assert code == 0, guard
    assert guard["scheduler_hint"]["codex_app"]["recommended_interval_minutes"] >= 1440
