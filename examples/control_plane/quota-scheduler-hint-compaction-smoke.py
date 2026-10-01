#!/usr/bin/env python3
"""Smoke-test the compact quota scheduler_hint hot-path contract."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from loopx.control_plane.scheduler.execution_context import (  # noqa: E402
    SchedulerRuntimeProfile,
    scheduler_execution_context_for_runtime_profile,
)
from loopx.control_plane.scheduler.scheduler_hint import build_scheduler_hint  # noqa: E402
from loopx.control_plane.quota.should_run_packet import _scheduler_hint  # noqa: E402


RUNTIME_KEYS = (
    "local_scheduler",
    "codex_cli_tui",
    "claude_code_loop",
)
BASE_RUNTIME_KEYS = RUNTIME_KEYS
SCHEDULER_HOST_FACTS_CHUNK_FLAG = "--scheduler-host-facts-chunk"
APP_SCHEDULER_CONTEXT = scheduler_execution_context_for_runtime_profile(
    SchedulerRuntimeProfile.CODEX_APP_HEARTBEAT
)


def _load_quota_plan_fixture_module():
    module_path = REPO_ROOT / "examples" / "control_plane" / "quota_plan_fixtures.py"
    spec = importlib.util.spec_from_file_location("quota_plan_smoke_fixture", module_path)
    assert spec and spec.loader, module_path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def payload(*, should_run: bool, recommended_mode: str = "", user_required: bool = False) -> dict:
    return {
        "goal_id": "quota-scheduler-compaction",
        "agent_identity": {"agent_id": "codex-compact-agent"},
        "should_run": should_run,
        "effective_action": "operator_gate_notify" if user_required else "normal_run",
        "recommended_action": "Keep scheduler hints compact on the hot path.",
        "heartbeat_recommendation": {
            "recommended_mode": recommended_mode,
            "notify": "NOTIFY" if user_required else "DONT_NOTIFY",
            "spend_policy": "spend only after validated writeback",
        },
        "execution_obligation": {
            "must_attempt_work": should_run,
            "spend_policy": "execution obligation spend policy",
        },
        "automation_liveness": {
            "automation_action": "",
            "spend_policy": "automation liveness spend policy",
        },
        "interaction_contract": {
            "schema_version": "loopx_interaction_contract_v0",
            "mode": recommended_mode or "normal_run",
            "user_channel": {
                "action_required": user_required,
            },
            "agent_channel": {
                "must_attempt": should_run,
                "delivery_allowed": should_run,
                "quiet_noop_allowed": not user_required and not should_run,
            },
        },
    }


def json_size(value: dict) -> int:
    return len(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def assert_compact_runtime_policy_complete(name: str, compact: dict) -> None:
    app_automation = compact["app_automation"]
    unchanged_poll = compact["unchanged_poll"]
    stateful_backoff = app_automation["stateful_backoff"]
    assert app_automation["recommended_interval_minutes"], (name, compact)
    assert app_automation["recommended_rrule"], (name, compact)
    assert app_automation["max_interval_minutes"], (name, compact)
    assert "example_progression_minutes" not in app_automation, (name, compact)
    assert app_automation["host_tool"] == "automation_update", (name, compact)
    assert app_automation["host_action"] == "update_current_heartbeat_rrule", (name, compact)
    assert "automation_update" in app_automation["host_action_contract"], (name, compact)
    assert app_automation["rrule_source"] == "scheduler_hint.app_automation.recommended_rrule", (name, compact)
    assert stateful_backoff["schema_version"] == "app_automation_stateful_backoff_v0", (name, compact)
    assert stateful_backoff["state_policy"] == "ephemeral_no_app_scheduler_state", (name, compact)
    assert stateful_backoff["reset_token"] == compact["reset_policy"]["reset_token"], (name, compact)
    assert stateful_backoff["apply_needed"] is True, (name, compact)
    assert stateful_backoff["current_rrule"] == app_automation["recommended_rrule"], (name, compact)
    assert "ack_hint" not in app_automation, (name, compact)
    assert "failure_hint" not in app_automation, (name, compact)
    for omitted in (
        "state_key",
        "identity_signature",
        "progression_index",
        "state_status",
        "progression_minutes",
        "current_interval_minutes",
        "persist",
        "same_identity_action",
        "reset_action",
        "automation_update_scope",
    ):
        assert omitted not in stateful_backoff, (name, omitted, compact)
    expected_runtime_keys = (
        RUNTIME_KEYS
        if unchanged_poll["final_quota_replan_check_enabled"]
        else BASE_RUNTIME_KEYS
    )
    assert set(unchanged_poll["limits"]) == set(expected_runtime_keys), (name, compact)
    assert set(unchanged_poll["after_limits"]) == set(expected_runtime_keys), (
        name,
        compact,
    )
    assert "final_quota_replan_check_enabled" in unchanged_poll, (name, compact)
    assert "final_quota_replan_check_action" in unchanged_poll, (name, compact)
    assert unchanged_poll["spend_policy"], (name, compact)
    assert compact["reset_policy"]["reset_token"], (name, compact)
    assert compact["reset_policy"]["app_automation_initial_rrule"], (name, compact)
    for omitted in (
        "schema_version",
        "app_automation_tool",
        "app_automation_apply",
        "profile_signature",
        "identity_key_count",
        "reset_condition_summary",
    ):
        assert omitted not in compact["reset_policy"], (name, omitted, compact)
    detail_ref = compact["detail_ref"]
    assert detail_ref["omitted_by_default"] is True, (name, compact)
    assert detail_ref["execution_required"] is False, (name, compact)
    assert detail_ref["hot_path_runtime_fields"] == ["app_automation", "unchanged_poll", "reset_policy"], (
        name,
        compact,
    )


def assert_compact_scheduler(name: str, source_payload: dict) -> None:
    fixed_now = datetime(2026, 9, 12, 0, 0, tzinfo=timezone.utc)
    with patch(
        "loopx.control_plane.scheduler.scheduler_hint.now_utc",
        return_value=fixed_now,
    ):
        compact = build_scheduler_hint(
            deepcopy(source_payload),
            user_action_required=False,
            scheduler_execution_context=APP_SCHEDULER_CONTEXT,
        )
        wrapper = _scheduler_hint(
            deepcopy(source_payload),
            scheduler_execution_context=APP_SCHEDULER_CONTEXT,
        )
        detailed = build_scheduler_hint(
            deepcopy(source_payload),
            user_action_required=False,
            include_detail=True,
            scheduler_execution_context=APP_SCHEDULER_CONTEXT,
        )

    assert compact == wrapper, (name, compact, wrapper)
    assert compact["schema_version"] == "scheduler_hint_v0", (name, compact)
    assert "local_scheduler" not in compact, (name, compact)
    assert "codex_cli_tui" not in compact, (name, compact)
    assert "claude_code_loop" not in compact, (name, compact)
    assert "cold_path_detail" not in compact, (name, compact)
    assert compact["detail_ref"]["omitted_by_default"] is True, (name, compact)
    assert compact["detail_ref"]["execution_required"] is False, (name, compact)
    assert compact["detail_ref"]["request"] == "loopx quota should-run --include-detail scheduler", (name, compact)
    assert_compact_runtime_policy_complete(name, compact)
    assert compact["reset_policy"]["reset_token"], (name, compact)
    assert compact["reset_policy"]["app_automation_initial_rrule"] == compact["app_automation"]["recommended_rrule"], (
        name,
        compact,
    )
    assert "identity_snapshot" not in compact["reset_policy"], (name, compact)
    assert "profile_snapshot" not in compact["reset_policy"], (name, compact)

    unchanged_poll = compact["unchanged_poll"]
    assert isinstance(unchanged_poll["limits"], dict), (name, compact)
    assert isinstance(unchanged_poll["after_limits"], dict), (name, compact)
    assert "final_quota_replan_check" not in unchanged_poll, (name, compact)

    cold_path = detailed["cold_path_detail"]
    assert cold_path["schema_version"] == "scheduler_hint_detail_v0", (name, detailed)
    assert cold_path["local_scheduler"]["recommended_interval_minutes"], (name, detailed)
    assert cold_path["codex_cli_tui"]["final_quota_replan_check"], (name, detailed)
    assert cold_path["claude_code_loop"]["after_limit"], (name, detailed)
    stateful_detail = cold_path["stateful_backoff_detail"]
    assert "progression_minutes" not in stateful_detail, (name, detailed)
    assert stateful_detail["state_policy"] == "ephemeral_no_app_scheduler_state", (
        name,
        detailed,
    )
    reset_detail = cold_path["reset_policy_detail"]
    assert reset_detail["schema_version"] == "scheduler_reset_policy_v0", (name, detailed)
    assert reset_detail["app_automation_tool"] == "automation_update", (name, detailed)
    assert "automation_update" in reset_detail["app_automation_apply"], (name, detailed)
    assert len(reset_detail["profile_signature"]) == 12, (name, detailed)
    assert json_size(compact) < json_size(detailed), (name, json_size(compact), json_size(detailed))
    # The App packet and its Codex copy dominate the compact hot path.
    assert json_size(compact) <= 4_000, (name, json_size(compact))


def run_should_run_cli(
    *,
    include_detail: bool,
    registry_path: Path,
    runtime: Path,
    project: Path,
    agent_id: str,
) -> dict:
    args = [
        sys.executable,
        "-m",
        "loopx.cli",
        "--registry",
        str(registry_path),
        "--runtime-root",
        str(runtime),
        "--format",
        "json",
        "quota",
        "should-run",
        "--goal-id",
        "needs-operator",
        "--agent-id",
        agent_id,
        "--codex-app",
        "--scan-path",
        str(project),
    ]
    if include_detail:
        args.extend(["--include-detail", "scheduler"])
    result = subprocess.run(
        args,
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def assert_cli_compact_and_detail_contract() -> None:
    fixture = _load_quota_plan_fixture_module()
    with tempfile.TemporaryDirectory(prefix="loopx-quota-scheduler-detail-cli-") as tmp:
        registry_path, runtime, project = fixture.write_cli_fixture(Path(tmp), scoped_agents=True)
        compact = run_should_run_cli(
            include_detail=False,
            registry_path=registry_path,
            runtime=runtime,
            project=project,
            agent_id=fixture.SCOPED_AGENT_ID,
        )["scheduler_hint"]
        detailed = run_should_run_cli(
            include_detail=True,
            registry_path=registry_path,
            runtime=runtime,
            project=project,
            agent_id=fixture.SCOPED_AGENT_ID,
        )["scheduler_hint"]

    assert_compact_runtime_policy_complete("cli-default", compact)
    for key in RUNTIME_KEYS:
        assert key not in compact, (key, compact)
        assert key not in detailed, (key, detailed)
        assert detailed["cold_path_detail"][key], (key, detailed)
    assert "cold_path_detail" not in compact, compact
    assert compact["detail_ref"]["request"] == "loopx quota should-run --include-detail scheduler", compact
    assert detailed["cold_path_detail"]["schema_version"] == "scheduler_hint_detail_v0", detailed
    assert detailed["cold_path_detail"]["codex_cli_tui"]["unchanged_poll_limit"] == (
        compact["unchanged_poll"]["limits"]["codex_cli_tui"]
    ), detailed
    assert detailed["cold_path_detail"]["codex_cli_tui"]["final_quota_replan_check"], detailed
    assert detailed["cold_path_detail"]["codex_cli_tui"]["after_limit"] == (
        compact["unchanged_poll"]["after_limits"]["codex_cli_tui"]
    ), detailed
    assert detailed["cold_path_detail"]["claude_code_loop"]["after_limit"] == (
        compact["unchanged_poll"]["after_limits"]["claude_code_loop"]
    ), detailed
    assert detailed["cold_path_detail"]["reset_policy_detail"]["app_automation_tool"] == "automation_update", detailed
    assert "progression_minutes" not in detailed["cold_path_detail"]["stateful_backoff_detail"], detailed


def main() -> int:
    assert_compact_scheduler("active-work", payload(should_run=True))
    assert_compact_scheduler(
        "human-gate",
        payload(should_run=False, recommended_mode="ask_operator_gate", user_required=True),
    )
    assert_cli_compact_and_detail_contract()
    print("quota-scheduler-hint-compaction-smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
