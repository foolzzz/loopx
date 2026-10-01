from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from ..effect_runtime import effect_runtime_result
from ..runtime.time import now_utc
from ..todos.frontier_deadline import build_frontier_recheck_plan
from .arbitration import (
    SchedulerArbitration,
    SchedulerDisposition,
    build_scheduler_arbitration,
)
from .execution_context import (
    SchedulerExecutionContextResolution,
    SchedulerOwner,
    SchedulerRuntimeProfile,
    apply_scheduler_execution_context,
    resolve_scheduler_execution_context,
)
from .monitor_wait import (
    MONITOR_WAIT_HOST_FLOOR_MINUTES,
    MONITOR_WAIT_PHASE_RANK,  # noqa: F401  # re-exported for compatibility
    MONITOR_WAIT_PROGRESSION_MINUTES,  # noqa: F401  # re-exported for compatibility
    MonitorWaitPhase,  # noqa: F401  # re-exported for compatibility
    _parse_monitor_timestamp,  # noqa: F401  # re-exported for compatibility
    build_monitor_wait_cadence_plan,
)
from .state import normalize_scheduler_rrule, rrule_for_minutes

SCHEDULER_HINT_SCHEMA_VERSION = "scheduler_hint_v0"
SCHEDULER_RESET_POLICY_SCHEMA_VERSION = "scheduler_reset_policy_v0"
SCHEDULER_HINT_DETAIL_SCHEMA_VERSION = "scheduler_hint_detail_v0"
APP_AUTOMATION_STATEFUL_BACKOFF_SCHEMA_VERSION = (
    "app_automation_stateful_backoff_v0"
)
CODEX_APP_MAX_INTERVAL_MINUTES = 60
SCHEDULER_BASE_IDENTITY_KEYS = (
    "goal_id",
    "agent_identity.agent_id",
    "effective_action",
    "heartbeat_recommendation.recommended_mode",
    "interaction_contract.mode",
)


SCHEDULER_FRONTIER_IDENTITY_KEYS = (
    "selected_todo.todo_id",
    "selected_todo.action_kind",
    "selected_todo.target_key",
    "selected_todo.claimed_by",
    "selected_todo.capability_binding_ref",
)
SCHEDULER_IDENTITY_KEYS = (
    *SCHEDULER_BASE_IDENTITY_KEYS,
    "recommended_action",
)
MONITOR_WAIT_IDENTITY_KEYS = SCHEDULER_BASE_IDENTITY_KEYS
CODEX_APP_SSH_GOAL_RUNTIME_KEY = SchedulerRuntimeProfile.CODEX_APP_SSH_VISIBLE.value
CODEX_NATIVE_GOAL_BLOCK_ACTION = "update_goal_blocked_keep_loopx_active"
CODEX_NATIVE_GOAL_RESUME_TRIGGER = "explicit_codex_goal_resume"


def _stable_digest(value: Any, *, length: int) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _scheduler_profile_digest_snapshot(
    profile_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep the persisted v0 reset identity stable across the App rename.

    These legacy key names are part of the reset-token hash protocol, not the
    public scheduler projection. Changing them would reset an existing Codex
    App backoff even when the cadence semantics are unchanged.
    """

    key_aliases = {
        "app_automation_initial_interval_minutes": (
            "codex_app_initial_interval_minutes"
        ),
        "app_automation_initial_rrule": "codex_app_initial_rrule",
        "app_automation_max_interval_minutes": (
            "codex_app_max_interval_minutes"
        ),
    }
    return {key_aliases.get(key, key): value for key, value in profile_snapshot.items()}


def _scheduler_reset_identity_digests(
    *,
    action: str,
    identity_snapshot: Mapping[str, Any],
    profile_snapshot: Mapping[str, Any],
    reset_profile_snapshot: Mapping[str, Any],
) -> tuple[str, str, str, str]:
    """Build the stable v0 reset token and its component signatures."""

    profile_digest_snapshot = _scheduler_profile_digest_snapshot(profile_snapshot)
    reset_profile_digest_snapshot = _scheduler_profile_digest_snapshot(
        reset_profile_snapshot
    )
    return (
        _stable_digest(
            {
                "action": action,
                "identity_snapshot": identity_snapshot,
                "profile_snapshot": reset_profile_digest_snapshot,
            },
            length=16,
        ),
        _stable_digest(identity_snapshot, length=12),
        _stable_digest(profile_digest_snapshot, length=12),
        _stable_digest(reset_profile_digest_snapshot, length=12),
    )


def _scheduler_identity_keys(
    *,
    cadence_class: str,
    execution_context: SchedulerExecutionContextResolution,
) -> tuple[str, ...]:
    base_keys = (
        MONITOR_WAIT_IDENTITY_KEYS
        if cadence_class == "monitor_wait"
        else SCHEDULER_IDENTITY_KEYS
    )
    context = execution_context.context if execution_context.ok else None
    if context is None or context.scheduler_owner is not SchedulerOwner.GOAL_RUNTIME:
        return base_keys
    if cadence_class == "monitor_wait":
        return (*base_keys, *SCHEDULER_FRONTIER_IDENTITY_KEYS)
    return (
        *base_keys[:-1],
        *SCHEDULER_FRONTIER_IDENTITY_KEYS,
        base_keys[-1],
    )


def _build_scheduler_stop_hint(
    *,
    execution_context: SchedulerExecutionContextResolution,
    action: str,
    cadence_class: str,
    reason_code: str,
    reason: str,
    spend_policy: str,
    resume_trigger: str,
    ssh_goal_runtime_action: str,
    unchanged_spend_policy: str,
) -> dict[str, Any]:
    return apply_scheduler_execution_context(
        {
            "schema_version": SCHEDULER_HINT_SCHEMA_VERSION,
            "source": "quota.should-run",
            "action": action,
            "cadence_class": cadence_class,
            "reason_code": reason_code,
            "reason": reason,
            "spend_policy": spend_policy,
            "app_automation": {
                "apply": "pause_or_delete_current_heartbeat_if_possible",
                "host_tool": "automation_update",
                "host_action": "pause_or_delete_current_heartbeat",
                "host_action_required": True,
                "attempt_limit": 1,
                "verify_host_result": True,
                "resume_trigger": resume_trigger,
                "no_spend_for_host_action": True,
            },
            "unchanged_poll": {
                "local_scheduler": "stop",
                "codex_cli_tui": "exit",
                CODEX_APP_SSH_GOAL_RUNTIME_KEY: ssh_goal_runtime_action,
                "claude_code_loop": "stop",
                "final_quota_replan_check_enabled": False,
                "spend_policy": unchanged_spend_policy,
            },
            "unchanged_identity_keys": list(
                _scheduler_identity_keys(
                    cadence_class=cadence_class,
                    execution_context=execution_context,
                )
            ),
        },
        execution_context,
    )


@dataclass(frozen=True)
class _SchedulerHintBuilder:
    payload: dict[str, Any]
    execution_context: SchedulerExecutionContextResolution
    arbitration: SchedulerArbitration
    spend_policy: Any
    codex_app_current_rrule: Any
    include_detail: bool

    def _identity_value(self, path: str) -> Any:
        current: Any = self.payload
        for part in path.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    def _cadence_projections(
        self, interval: int, maximum: int, multiplier: int, override: list[int] | None,
    ) -> dict[str, Any]:
        """Keep legacy profile selection separate from the owner constraint projection."""
        local = override or [min(interval * multiplier**step, maximum) for step in range(3)]
        app_max = min(max(1, maximum), CODEX_APP_MAX_INTERVAL_MINUTES)
        app: list[int] = []
        for value in local:
            bounded = min(max(1, int(value)), app_max)
            if not app or app[-1] != bounded:
                app.append(bounded)
        projections = {"local": local, "app": app, "app_max": app_max, "local_max": maximum, "floor": 0}
        policy = _dict_or_empty(self.payload.get("automation_cadence"))
        floor = policy.get("min_interval_minutes")
        return effect_runtime_result("quota.automation_cadence.schedule", {
            **projections, "min_interval_minutes": floor,
        }) if floor else projections

    def build(
        self,
        *,
        action: str,
        cadence_class: str,
        reason: str,
        codex_interval: int,
        codex_max: int,
        cli_limit: int | None,
        claude_limit: int | None,
        multiplier: int = 2,
        cadence_progression_override: list[int] | None = None,
        reset_profile_snapshot_override: dict[str, Any] | None = None,
        cadence_context_detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cadence = self._cadence_projections(codex_interval, codex_max, multiplier, cadence_progression_override)
        local_cadence_progression, app_cadence_progression = cadence["local"], cadence["app"]
        app_host_max, codex_max, floor = cadence["app_max"], cadence["local_max"], cadence["floor"]
        app_initial_interval = app_cadence_progression[0]
        local_initial_interval = local_cadence_progression[0]
        final_replan_check = {
            "enabled": cli_limit is not None or claude_limit is not None,
            "trigger": "before_unchanged_poll_after_limit",
            "action": "rerun_quota_should_run_once",
            "if_changed": "follow_new_scheduler_hint",
            "if_run_now": "execute_new_quota_contract",
            "if_unchanged": "apply_after_limit_without_spend",
            "spend_policy": "no quota spend for final replan check or loop stop",
        }
        identity_keys = list(
            _scheduler_identity_keys(
                cadence_class=cadence_class,
                execution_context=self.execution_context,
            )
        )
        identity_snapshot = {key: self._identity_value(key) for key in identity_keys}
        app_rrule = rrule_for_minutes(app_initial_interval)
        profile_snapshot = {
            "cadence_class": cadence_class,
            "app_automation_initial_interval_minutes": app_initial_interval,
            "app_automation_initial_rrule": app_rrule,
            "app_automation_max_interval_minutes": app_host_max,
            "unchanged_poll_backoff_multiplier": multiplier,
            "local_scheduler_unchanged_poll_limit": cli_limit,
            "claude_code_loop_unchanged_poll_limit": claude_limit,
        }
        reset_profile_snapshot = reset_profile_snapshot_override or profile_snapshot
        (
            reset_token,
            identity_signature,
            profile_signature,
            reset_profile_signature,
        ) = _scheduler_reset_identity_digests(
            action=action,
            identity_snapshot=identity_snapshot,
            profile_snapshot=profile_snapshot,
            reset_profile_snapshot=reset_profile_snapshot,
        )
        reset_policy_detail = {
            "schema_version": SCHEDULER_RESET_POLICY_SCHEMA_VERSION,
            "source": "quota.should-run",
            "reset_to": "profile_initial_interval",
            "profile_action": action,
            "reset_token": reset_token,
            "app_automation_initial_interval_minutes": app_initial_interval,
            "app_automation_initial_rrule": app_rrule,
            "local_scheduler_initial_interval_minutes": local_initial_interval,
            "clear_unchanged_poll_state": True,
            "identity_key_count": len(identity_keys),
            "identity_signature": identity_signature,
            "profile_signature": profile_signature,
            "reset_profile_signature": reset_profile_signature,
            "reset_condition_summary": "token_changed|user_feedback|new_or_reassigned_todo|gate_or_material_transition|active_work_projected",
            "after_reset": "apply_initial_interval_before_backoff",
            "app_automation_tool": "automation_update",
            "app_automation_apply": "call_automation_update_when_observed_rrule_differs_from_initial",
            "no_spend_for_reset": True,
        }
        reset_policy = {
            "reset_token": reset_token,
            "app_automation_initial_interval_minutes": app_initial_interval,
            "app_automation_initial_rrule": app_rrule,
            "identity_signature": identity_signature,
        }
        local_scheduler = {
            "recommended_interval_minutes": local_initial_interval,
            "max_interval_minutes": codex_max,
            "unchanged_poll_backoff_multiplier": multiplier,
            "example_progression_minutes": local_cadence_progression,
            "unchanged_poll_limit": cli_limit,
            "after_limit": "stop_tick_loop" if cli_limit is not None else "continue",
            "final_quota_replan_check": final_replan_check,
            "no_spend_for_cadence_change": True,
        }
        codex_goal_loop = {
            "unchanged_poll_limit": cli_limit,
            "after_limit": (
                CODEX_NATIVE_GOAL_BLOCK_ACTION if cli_limit is not None else "continue"
            ),
            "final_quota_replan_check": final_replan_check,
            "loopx_goal_state": "remains_active",
            "resume_trigger": CODEX_NATIVE_GOAL_RESUME_TRIGGER,
            "no_spend_for_block": True,
        }
        codex_cli_tui = {
            **codex_goal_loop,
            "no_spend_for_exit": True,
        }
        codex_app_ssh_goal = dict(codex_goal_loop)
        claude_code_loop = {
            "unchanged_poll_limit": claude_limit,
            "after_limit": "stop_loop" if claude_limit is not None else "continue",
            "final_quota_replan_check": final_replan_check,
            "no_spend_for_stop": True,
        }
        current_interval = app_initial_interval
        current_rrule = app_rrule
        observed_host_rrule = normalize_scheduler_rrule(
            self.codex_app_current_rrule
        )
        current_rrule_already_applied = bool(
            observed_host_rrule
            and observed_host_rrule == normalize_scheduler_rrule(current_rrule)
        )
        apply_needed = not current_rrule_already_applied
        stateful_backoff_detail = {
            "current_interval_minutes": current_interval,
            "host_max_interval_minutes": app_host_max,
            "coarser_wait_fallback": "hold_affected_automation" if floor else "local_scheduler_only",
            "state_policy": "ephemeral_no_app_scheduler_state",
            "reset_action": "apply_profile_initial_rrule_when_observed_rrule_differs",
            "automation_update_scope": "rrule_only_preserve_body_name_status",
        }
        app_automation = {
            "recommended_interval_minutes": current_interval,
            "max_interval_minutes": app_host_max,
            "unchanged_poll_backoff_multiplier": multiplier,
            "apply": (
                "update_automation_cadence_if_possible"
                if apply_needed
                else "none_already_applied"
            ),
            "host_tool": "automation_update",
            "host_action": (
                "update_current_heartbeat_rrule"
                if apply_needed
                else "none"
            ),
            "host_action_contract": (
                "automation_update_rrule_once"
                if apply_needed
                else "skip_automation_update_when_apply_needed_false"
            ),
            "rrule_source": (
                "scheduler_hint.app_automation.recommended_rrule"
                if apply_needed
                else None
            ),
            "stateful_backoff": {
                "schema_version": APP_AUTOMATION_STATEFUL_BACKOFF_SCHEMA_VERSION,
                "reset_token": reset_token,
                "current_rrule": current_rrule,
                "apply_needed": apply_needed,
                "state_policy": "ephemeral_no_app_scheduler_state",
            },
            "no_spend_for_cadence_change": True,
        }
        if floor:
            app_automation["execution_interval_policy"] = self.payload["automation_cadence"]
            app_automation["guarantee"] = cadence["guarantee"]
        stateful_backoff = app_automation["stateful_backoff"]
        if observed_host_rrule:
            stateful_backoff["host_observation"] = {
                "source": "quota_should_run_host_observation",
                "current_rrule": observed_host_rrule,
                "status": (
                    "matches_recommended"
                    if current_rrule_already_applied
                    else "drift_detected"
                ),
            }
        if apply_needed:
            app_automation["recommended_rrule"] = current_rrule
        unchanged_poll_limits = {
            "local_scheduler": cli_limit,
            "codex_cli_tui": cli_limit,
            "claude_code_loop": claude_limit,
        }
        unchanged_poll_after_limits = {
            "local_scheduler": local_scheduler["after_limit"],
            "codex_cli_tui": codex_cli_tui["after_limit"],
            "claude_code_loop": claude_code_loop["after_limit"],
        }
        detail_contains = [
            "local_scheduler",
            "codex_cli_tui",
            "claude_code_loop",
            "final_quota_replan_check",
            "reset_policy_detail",
            "stateful_backoff_detail",
        ]
        if cli_limit is not None:
            unchanged_poll_limits[CODEX_APP_SSH_GOAL_RUNTIME_KEY] = cli_limit
            unchanged_poll_after_limits[CODEX_APP_SSH_GOAL_RUNTIME_KEY] = (
                codex_app_ssh_goal["after_limit"]
            )
            detail_contains.insert(2, CODEX_APP_SSH_GOAL_RUNTIME_KEY)
        scheduler_hint = {
            "schema_version": SCHEDULER_HINT_SCHEMA_VERSION,
            "source": "quota.should-run",
            "action": action,
            "cadence_class": cadence_class,
            "reason_code": self.arbitration.reason_code,
            "reason": reason,
            "spend_policy": self.spend_policy,
            "app_automation": app_automation,
            "unchanged_poll": {
                "limits": unchanged_poll_limits,
                "after_limits": unchanged_poll_after_limits,
                "final_quota_replan_check_enabled": final_replan_check["enabled"],
                "final_quota_replan_check_action": (
                    final_replan_check["action"]
                    if final_replan_check["enabled"]
                    else None
                ),
                "spend_policy": final_replan_check["spend_policy"],
            },
            "unchanged_identity_keys": identity_keys,
            "reset_policy": reset_policy,
            "detail_ref": {
                "schema_version": SCHEDULER_HINT_DETAIL_SCHEMA_VERSION,
                "omitted_by_default": True,
                "execution_required": False,
                "request": "loopx quota should-run --include-detail scheduler",
                "hot_path_runtime_fields": [
                    "app_automation",
                    "unchanged_poll",
                    "reset_policy",
                ],
                "contains": detail_contains,
            },
        }
        frontier_recheck = build_frontier_recheck_plan(
            self.payload,
            current_time=now_utc(),
        )
        if self.include_detail:
            scheduler_hint["cold_path_detail"] = {
                "schema_version": SCHEDULER_HINT_DETAIL_SCHEMA_VERSION,
                "source": "quota.should-run",
                "local_scheduler": local_scheduler,
                "codex_cli_tui": codex_cli_tui,
                CODEX_APP_SSH_GOAL_RUNTIME_KEY: codex_app_ssh_goal,
                "claude_code_loop": claude_code_loop,
                "final_quota_replan_check": final_replan_check,
                "reset_policy_detail": reset_policy_detail,
                "stateful_backoff_detail": stateful_backoff_detail,
            }
            if cadence_context_detail:
                scheduler_hint["cold_path_detail"]["cadence_context"] = (
                    cadence_context_detail
                )
            if frontier_recheck:
                scheduler_hint["cold_path_detail"]["frontier_recheck"] = (
                    frontier_recheck
                )
        return apply_scheduler_execution_context(
            scheduler_hint,
            self.execution_context,
            frontier_recheck_after_seconds=(
                frontier_recheck.get("frontier_recheck_after_seconds")
                if frontier_recheck
                else None
            ),
        )


def _monitor_bounded_wait_profile(
    payload: dict[str, Any],
    *,
    cadence_class: str,
    default_interval_minutes: int,
    max_interval_minutes: int,
) -> dict[str, Any]:
    """Cap a wait profile to the tightest continuous-monitor wakeup."""

    monitor_plan = build_monitor_wait_cadence_plan(
        payload,
        current_time=now_utc(),
    )
    monitor_progression = (
        monitor_plan.get("progression_minutes")
        if isinstance(monitor_plan, dict)
        else None
    )
    progression = (
        monitor_progression
        if isinstance(monitor_progression, list) and monitor_progression
        else None
    )
    initial_interval = int(progression[0]) if progression else default_interval_minutes
    monitor_reset_profile = (
        {
            "cadence_class": cadence_class,
            "app_automation_initial_interval_minutes": initial_interval,
            "app_automation_initial_rrule": rrule_for_minutes(initial_interval),
            "app_automation_max_interval_minutes": max_interval_minutes,
            "unchanged_poll_backoff_multiplier": 2,
            "local_scheduler_unchanged_poll_limit": 3,
            "claude_code_loop_unchanged_poll_limit": 3,
            **monitor_plan["reset_profile"],
        }
        if isinstance(monitor_plan, dict)
        and isinstance(monitor_plan.get("reset_profile"), dict)
        else None
    )
    return {
        "codex_interval": initial_interval,
        "codex_max": max_interval_minutes,
        "cadence_progression_override": progression,
        "reset_profile_snapshot_override": monitor_reset_profile,
        "cadence_context_detail": monitor_plan,
    }


def build_scheduler_hint(
    payload: dict[str, Any],
    *,
    user_action_required: bool = False,
    agent_scope_frontier_actions: Collection[str] = (),
    include_detail: bool = False,
    codex_app_current_rrule: Any = None,
    scheduler_execution_context: (
        Mapping[str, Any] | SchedulerExecutionContextResolution | None
    ) = None,
) -> dict[str, Any]:
    """Project host-runtime cadence/backoff policy from a quota decision.

    This helper is intentionally pure: callers provide the few quota-local
    classification facts it needs, and it returns the public scheduler contract
    without reading files, mutating state, or depending on the full quota module.
    """

    execution_context = resolve_scheduler_execution_context(scheduler_execution_context)
    if not execution_context.ok:
        blocked_app_automation = {
            "applicability": "blocked_invalid_context",
            "apply": "none",
            "host_action": "none",
        }
        blocked_hint = {
            "schema_version": SCHEDULER_HINT_SCHEMA_VERSION,
            "source": "quota.should-run",
            "action": "repair_scheduler_execution_context",
            "cadence_class": "control_plane_repair",
            "reason_code": "invalid_scheduler_execution_context",
            "reason": (
                "scheduler ownership is missing or contradictory; repair the "
                "typed execution context before applying cadence"
            ),
            "spend_policy": "no quota spend for scheduler context repair",
            "execution_context": execution_context.projection(),
            "execution_phase": {
                "schema_version": "scheduler_execution_phase_v0",
                "disposition": "contract_error",
                "completed": False,
                "apply_needed": False,
            },
            "unchanged_poll": {
                "local_scheduler": "stop_until_context_repaired",
                "codex_cli_tui": "stop_until_context_repaired",
                CODEX_APP_SSH_GOAL_RUNTIME_KEY: "stop_until_context_repaired",
                "claude_code_loop": "stop_until_context_repaired",
                "final_quota_replan_check_enabled": False,
                "spend_policy": "no quota spend for scheduler context repair",
            },
            "consistency_error": {
                "source": "scheduler_execution_context",
                "errors": list(execution_context.errors),
            },
        }
        supplied_host_surface = str(
            (execution_context.supplied or {}).get("host_surface") or ""
        ).strip()
        if supplied_host_surface == "trae_app":
            blocked_hint["app_automation"] = blocked_app_automation
        else:
            blocked_hint["codex_app"] = blocked_app_automation
        return blocked_hint

    heartbeat_recommendation = _dict_or_empty(payload.get("heartbeat_recommendation"))
    pause_mode = str(heartbeat_recommendation.get("recommended_mode") or "")
    if pause_mode in {"goal_stopped", "quota_paused"}:
        goal_stopped = pause_mode == "goal_stopped"
        cadence_class = "goal_stopped" if goal_stopped else "quota_paused"
        resume_trigger = (
            "explicit Goal lifecycle resume"
            if goal_stopped
            else "explicit quota resume with quota.compute > 0"
        )
        return apply_scheduler_execution_context(
            {
                "schema_version": SCHEDULER_HINT_SCHEMA_VERSION,
                "source": "quota.should-run",
                "action": "stop_until_explicit_resume",
                "cadence_class": cadence_class,
                "reason_code": cadence_class,
                "reason": (
                    "Goal lifecycle is stopped by owner; recurring host automation "
                    "must stop until the Goal is explicitly resumed"
                    if goal_stopped
                    else "Goal-level compute quota is paused; recurring host automation "
                    "must stop until quota.compute is explicitly raised above 0"
                ),
                "spend_policy": (
                    "no quota spend for stopped-Goal automation shutdown"
                    if goal_stopped
                    else "no quota spend for paused automation shutdown"
                ),
                "app_automation": {
                    "apply": "pause_or_delete_current_heartbeat_if_possible",
                    "host_tool": "automation_update",
                    "host_action": "pause_or_delete_current_heartbeat",
                    "host_action_required": True,
                    "attempt_limit": 1,
                    "verify_host_result": True,
                    "resume_trigger": resume_trigger,
                    "no_spend_for_host_action": True,
                },
                "unchanged_poll": {
                    "local_scheduler": "stop",
                    "codex_cli_tui": "exit",
                    CODEX_APP_SSH_GOAL_RUNTIME_KEY: "complete_host_goal",
                    "claude_code_loop": "stop",
                    "final_quota_replan_check_enabled": False,
                    "spend_policy": (
                        "no quota spend while the Goal lifecycle is stopped"
                        if goal_stopped
                        else "no quota spend while compute quota is paused"
                    ),
                },
                "unchanged_identity_keys": list(
                    _scheduler_identity_keys(
                        cadence_class=cadence_class,
                        execution_context=execution_context,
                    )
                ),
            },
            execution_context,
        )

    execution_obligation = _dict_or_empty(payload.get("execution_obligation"))
    automation_liveness = _dict_or_empty(payload.get("automation_liveness"))
    spend_policy = (
        automation_liveness.get("spend_policy")
        or execution_obligation.get("spend_policy")
        or heartbeat_recommendation.get("spend_policy")
    )
    agent_scope_action_set = {str(value) for value in agent_scope_frontier_actions}
    arbitration = build_scheduler_arbitration(
        payload,
        agent_scope_frontier_actions=agent_scope_action_set,
    )

    if arbitration.disposition == SchedulerDisposition.TERMINAL_STOP:
        return _build_scheduler_stop_hint(
            execution_context=execution_context,
            action="stop_until_explicit_resume",
            cadence_class="terminal_no_followup",
            reason_code=arbitration.reason_code,
            reason=(
                "validated closure evidence derives no-follow-up and confirms no "
                "remaining frontier; recurring polling must stop until resume"
            ),
            spend_policy="no quota spend for terminal automation shutdown",
            resume_trigger="explicit goal resume or newly projected work",
            ssh_goal_runtime_action="complete_host_goal",
            unchanged_spend_policy="no quota spend for terminal loop stop",
        )

    builder = _SchedulerHintBuilder(
        payload=payload,
        execution_context=execution_context,
        arbitration=arbitration,
        spend_policy=spend_policy,
        codex_app_current_rrule=codex_app_current_rrule,
        include_detail=include_detail,
    )
    if arbitration.disposition == SchedulerDisposition.PEER_COORDINATION_WAIT:
        return builder.build(
            action="backoff_until_reassigned",
            cadence_class="peer_coordination_wait",
            reason=(
                "explicit peer coordination has no executable peer lane or local "
                "fallback; keep a bounded no-spend observer because peer readiness, "
                "configuration, or the local frontier can change asynchronously"
            ),
            codex_interval=10,
            codex_max=60,
            cli_limit=3,
            claude_limit=3,
            cadence_progression_override=[10, 20, 30, 60],
        )
    if arbitration.disposition == SchedulerDisposition.AGENT_MONITOR_ONLY_WAIT:
        return builder.build(
            action="backoff_agent_monitor_only",
            cadence_class="agent_monitor_only",
            reason=(
                "agent monitor-only mode blocks advancement while a quiet poll keeps "
                "due monitors and verified direct replies responsive"
            ),
            codex_interval=15,
            codex_max=60,
            cli_limit=3,
            claude_limit=3,
        )

    if arbitration.disposition == SchedulerDisposition.CONSISTENCY_REPAIR:
        result = builder.build(
            action="repair_interaction_contract_projection",
            cadence_class="control_plane_repair",
            reason=(
                "scheduler inputs disagree with the final interaction contract; "
                "repair the projection before applying delivery or wait cadence"
            ),
            codex_interval=3,
            codex_max=10,
            cli_limit=None,
            claude_limit=None,
        )
        result["consistency_error"] = arbitration.consistency_error()
        return result

    if arbitration.disposition == SchedulerDisposition.HUMAN_GATE:
        human_gate_profile = _monitor_bounded_wait_profile(
            payload,
            cadence_class="human_gate",
            default_interval_minutes=30,
            max_interval_minutes=120,
        )
        return builder.build(
            action="backoff_waiting_for_user",
            cadence_class="human_gate",
            reason=(
                "user/controller action is the next unlock; surface the concrete "
                "gate once, then stop repeating the same quiet poll"
            ),
            cli_limit=3,
            claude_limit=3,
            **human_gate_profile,
        )

    if arbitration.disposition == SchedulerDisposition.ACTIVE_WORK:
        return builder.build(
            action="run_now",
            cadence_class="active_work",
            reason=(
                "the interaction contract requires an agent attempt; keep the active "
                "scheduler cadence until the turn validates or blocks"
            ),
            codex_interval=3,
            codex_max=10,
            cli_limit=None,
            claude_limit=None,
        )

    if arbitration.disposition == SchedulerDisposition.UNCHANGED_WAIT:
        return builder.build(
            action="backoff_until_fresh_evidence",
            cadence_class="unchanged_noop",
            reason=(
                "the current mapped or post-handoff source is unchanged; do not "
                "keep a tight loop while waiting for fresh evidence or a concrete handoff"
            ),
            codex_interval=60,
            codex_max=240,
            cli_limit=3,
            claude_limit=3,
        )

    if arbitration.disposition == SchedulerDisposition.AGENT_SCOPE_WAIT:
        return builder.build(
            action="backoff_until_reassigned",
            cadence_class="agent_scope_wait",
            reason=(
                "this registered agent has no in-scope advancement candidate; "
                "agent-to-agent handoffs may change quickly, so stay closer to "
                "the prior scheduler cadence while waiting for handoff owner "
                "progress, reassignment, or a current-agent todo"
            ),
            codex_interval=10,
            codex_max=60,
            cli_limit=3,
            claude_limit=3,
            cadence_progression_override=[10, 20, 30, 60],
        )

    if arbitration.disposition == SchedulerDisposition.MONITOR_WAIT:
        monitor_profile = _monitor_bounded_wait_profile(
            payload,
            cadence_class="monitor_wait",
            default_interval_minutes=MONITOR_WAIT_HOST_FLOOR_MINUTES,
            max_interval_minutes=60,
        )
        return builder.build(
            action="backoff_until_material_transition",
            cadence_class="monitor_wait",
            reason=(
                "monitor-only quiet polls should remain alive but use a slower "
                "cadence until material evidence, a blocker, or replan obligation appears"
            ),
            cli_limit=3,
            claude_limit=3,
            **monitor_profile,
        )

    if arbitration.disposition == SchedulerDisposition.QUIET_WAIT:
        return builder.build(
            action="backoff_until_state_change",
            cadence_class="quiet_wait",
            reason=(
                "quota blocks delivery and no immediate user/monitor-specific path "
                "is projected; poll at a slower cadence until the status changes"
            ),
            codex_interval=30,
            codex_max=120,
            cli_limit=3,
            claude_limit=3,
        )

    raise AssertionError(f"unhandled scheduler disposition: {arbitration.disposition}")
