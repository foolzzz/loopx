from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

SCHEDULER_EXECUTION_CONTEXT_SCHEMA_VERSION = "scheduler_execution_context_v0"
GOAL_RUNTIME_CONTINUATION_SCHEMA_VERSION = "goal_runtime_continuation_v0"


class HostSurface(str, Enum):
    ARK_MANAGED_AGENT = "ark_managed_agent"
    CODEX_CLI = "codex_cli"
    GENERIC_CLI = "generic_cli"
    CLAUDE_CODE = "claude_code"
    LOCAL_SCHEDULER = "local_scheduler"


class SchedulerOwner(str, Enum):
    HOST_AUTOMATION = "host_automation"
    AGENT_CLI_LOOP = "agent_cli_loop"
    GOAL_RUNTIME = "goal_runtime"
    OUTER_CONTROLLER = "outer_controller"
    NONE = "none"


class ExecutionMode(str, Enum):
    INTERACTIVE = "interactive"
    ISOLATED_HEADLESS = "isolated_headless"
    HOSTED_AUTOMATION = "hosted_automation"


class SchedulerRuntimeProfile(str, Enum):
    ARK_MANAGED_AGENT_GOAL = "ark_managed_agent_goal"
    CODEX_CLI_VISIBLE = "codex_cli"
    CLAUDE_CODE_VISIBLE = "claude_code"
    GENERIC_CLI_AGENT_LOOP = "generic_cli"
    GENERIC_CLI_OUTER_CONTROLLER = "outer_controller"


class GoalRuntimeContinuationDisposition(str, Enum):
    CONTINUE_NOW = "continue_now"
    DEFER = "defer"
    COMPLETE = "complete"


GOAL_RUNTIME_DEFER_ACTIONS = frozenset(
    {
        "backoff_agent_monitor_only",
        "backoff_until_fresh_evidence",
        "backoff_until_material_transition",
        "backoff_until_reassigned",
        "backoff_until_state_change",
        "backoff_waiting_for_user",
        "repair_interaction_contract_projection",
    }
)


NATIVE_GOAL_RUNTIME_PROFILES = frozenset(
    {
        SchedulerRuntimeProfile.ARK_MANAGED_AGENT_GOAL,
        SchedulerRuntimeProfile.CODEX_CLI_VISIBLE,
    }
)

# Interactive hosts in this set can carry one explicit Turn identity through
# accountable writeback and visible-Goal quota settlement. Claude's ordinary
# Todo delivery remains MCP-owned; this shared contract also permits its
# Todo-less replan re-entry to finish once a Turn has been supplied.
VISIBLE_GOAL_SETTLEMENT_RUNTIME_PROFILES = frozenset(
    {
        *NATIVE_GOAL_RUNTIME_PROFILES,
        SchedulerRuntimeProfile.CLAUDE_CODE_VISIBLE,
    }
)

_SCHEDULER_RUNTIME_PROFILE_CONTEXTS = {
    SchedulerRuntimeProfile.ARK_MANAGED_AGENT_GOAL: (
        HostSurface.ARK_MANAGED_AGENT,
        SchedulerOwner.GOAL_RUNTIME,
        ExecutionMode.INTERACTIVE,
    ),
    SchedulerRuntimeProfile.CODEX_CLI_VISIBLE: (
        HostSurface.CODEX_CLI,
        SchedulerOwner.AGENT_CLI_LOOP,
        ExecutionMode.INTERACTIVE,
    ),
    SchedulerRuntimeProfile.CLAUDE_CODE_VISIBLE: (
        HostSurface.CLAUDE_CODE,
        SchedulerOwner.AGENT_CLI_LOOP,
        ExecutionMode.INTERACTIVE,
    ),
    SchedulerRuntimeProfile.GENERIC_CLI_AGENT_LOOP: (
        HostSurface.GENERIC_CLI,
        SchedulerOwner.AGENT_CLI_LOOP,
        ExecutionMode.INTERACTIVE,
    ),
    SchedulerRuntimeProfile.GENERIC_CLI_OUTER_CONTROLLER: (
        HostSurface.GENERIC_CLI,
        SchedulerOwner.OUTER_CONTROLLER,
        ExecutionMode.ISOLATED_HEADLESS,
    ),
}


GENERIC_CLI_OUTER_CONTROLLER_SCHEDULER_CONTEXT = {
    "host_surface": HostSurface.GENERIC_CLI.value,
    "scheduler_owner": SchedulerOwner.OUTER_CONTROLLER.value,
    "execution_mode": ExecutionMode.ISOLATED_HEADLESS.value,
}


@dataclass(frozen=True)
class SchedulerExecutionContext:
    host_surface: HostSurface
    scheduler_owner: SchedulerOwner
    execution_mode: ExecutionMode
    source: str

    def projection(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEDULER_EXECUTION_CONTEXT_SCHEMA_VERSION,
            "host_surface": self.host_surface.value,
            "scheduler_owner": self.scheduler_owner.value,
            "execution_mode": self.execution_mode.value,
            "source": self.source,
            "valid": True,
        }


@dataclass(frozen=True)
class SchedulerExecutionContextResolution:
    context: SchedulerExecutionContext | None
    errors: tuple[str, ...] = ()
    supplied: Mapping[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.context is not None and not self.errors

    def projection(self) -> dict[str, Any]:
        if self.context is not None:
            return self.context.projection()
        supplied = dict(self.supplied or {})
        return {
            "schema_version": SCHEDULER_EXECUTION_CONTEXT_SCHEMA_VERSION,
            "host_surface": supplied.get("host_surface"),
            "scheduler_owner": supplied.get("scheduler_owner"),
            "execution_mode": supplied.get("execution_mode"),
            "source": supplied.get("source") or "explicit",
            "valid": False,
            "errors": list(self.errors),
        }


def _validation_errors(context: SchedulerExecutionContext) -> list[str]:
    errors: list[str] = []
    cli_surfaces = {
        HostSurface.CODEX_CLI,
        HostSurface.GENERIC_CLI,
        HostSurface.CLAUDE_CODE,
    }
    if context.host_surface is HostSurface.ARK_MANAGED_AGENT:
        if context.scheduler_owner is not SchedulerOwner.GOAL_RUNTIME:
            errors.append("ark_managed_agent requires scheduler_owner=goal_runtime")
        if context.execution_mode is not ExecutionMode.INTERACTIVE:
            errors.append("ark_managed_agent requires execution_mode=interactive")
    if context.host_surface is HostSurface.LOCAL_SCHEDULER:
        if context.scheduler_owner is not SchedulerOwner.HOST_AUTOMATION:
            errors.append("local_scheduler requires scheduler_owner=host_automation")
        if context.execution_mode is not ExecutionMode.HOSTED_AUTOMATION:
            errors.append("local_scheduler requires execution_mode=hosted_automation")
    if (
        context.host_surface in cli_surfaces
        and context.scheduler_owner is SchedulerOwner.HOST_AUTOMATION
    ):
        errors.append("CLI host surfaces cannot be owned by host_automation")
    if context.scheduler_owner is SchedulerOwner.OUTER_CONTROLLER:
        if context.host_surface not in cli_surfaces:
            errors.append("outer_controller requires a CLI host surface")
        if context.execution_mode is not ExecutionMode.ISOLATED_HEADLESS:
            errors.append("outer_controller requires execution_mode=isolated_headless")
    if context.scheduler_owner is SchedulerOwner.NONE:
        if context.host_surface not in cli_surfaces:
            errors.append("scheduler_owner=none requires a CLI host surface")
        if context.execution_mode is not ExecutionMode.INTERACTIVE:
            errors.append("scheduler_owner=none requires execution_mode=interactive")
    if context.scheduler_owner is SchedulerOwner.AGENT_CLI_LOOP:
        if context.host_surface not in cli_surfaces:
            errors.append("agent_cli_loop requires a CLI host surface")
        if context.execution_mode is ExecutionMode.HOSTED_AUTOMATION:
            errors.append("agent_cli_loop cannot use execution_mode=hosted_automation")
    if context.scheduler_owner is SchedulerOwner.GOAL_RUNTIME:
        if context.host_surface is not HostSurface.ARK_MANAGED_AGENT:
            errors.append("goal_runtime requires host_surface=ark_managed_agent")
        if context.execution_mode is not ExecutionMode.INTERACTIVE:
            errors.append("goal_runtime requires execution_mode=interactive")
    if (
        context.execution_mode is ExecutionMode.HOSTED_AUTOMATION
        and context.scheduler_owner is not SchedulerOwner.HOST_AUTOMATION
    ):
        errors.append("hosted_automation requires scheduler_owner=host_automation")
    return errors


def resolve_scheduler_execution_context(
    value: Mapping[str, Any] | SchedulerExecutionContextResolution | None,
) -> SchedulerExecutionContextResolution:
    if isinstance(value, SchedulerExecutionContextResolution):
        return value
    if value is None:
        return SchedulerExecutionContextResolution(
            context=None,
            errors=(
                "missing required field: host_surface",
                "missing required field: scheduler_owner",
                "missing required field: execution_mode",
            ),
            supplied=None,
        )

    supplied = dict(value)
    missing = [
        field
        for field in ("host_surface", "scheduler_owner", "execution_mode")
        if not str(supplied.get(field) or "").strip()
    ]
    errors = [f"missing required field: {field}" for field in missing]
    try:
        host_surface = HostSurface(str(supplied.get("host_surface") or ""))
    except ValueError:
        host_surface = None
        if "host_surface" not in missing:
            errors.append("unsupported host_surface")
    try:
        scheduler_owner = SchedulerOwner(str(supplied.get("scheduler_owner") or ""))
    except ValueError:
        scheduler_owner = None
        if "scheduler_owner" not in missing:
            errors.append("unsupported scheduler_owner")
    try:
        execution_mode = ExecutionMode(str(supplied.get("execution_mode") or ""))
    except ValueError:
        execution_mode = None
        if "execution_mode" not in missing:
            errors.append("unsupported execution_mode")
    if errors:
        return SchedulerExecutionContextResolution(
            context=None,
            errors=tuple(errors),
            supplied=supplied,
        )
    context = SchedulerExecutionContext(
        host_surface=host_surface,
        scheduler_owner=scheduler_owner,
        execution_mode=execution_mode,
        source=str(supplied.get("source") or "explicit"),
    )
    errors = _validation_errors(context)
    return SchedulerExecutionContextResolution(
        context=context if not errors else None,
        errors=tuple(errors),
        supplied=supplied,
    )


def scheduler_execution_context_for_runtime_profile(
    value: str | SchedulerRuntimeProfile,
) -> SchedulerExecutionContextResolution:
    try:
        profile = SchedulerRuntimeProfile(value)
    except ValueError:
        return SchedulerExecutionContextResolution(
            context=None,
            errors=("unsupported scheduler runtime profile",),
            supplied={"source": f"runtime_profile:{value}"},
        )

    host_surface, scheduler_owner, execution_mode = (
        _SCHEDULER_RUNTIME_PROFILE_CONTEXTS[profile]
    )
    return resolve_scheduler_execution_context(
        {
            "host_surface": host_surface.value,
            "scheduler_owner": scheduler_owner.value,
            "execution_mode": execution_mode.value,
            "source": f"runtime_profile:{profile.value}",
        }
    )


def scheduler_runtime_profile_for_execution_context(
    value: Mapping[str, Any] | SchedulerExecutionContextResolution | None,
) -> SchedulerRuntimeProfile | None:
    resolution = resolve_scheduler_execution_context(value)
    if not resolution.ok or resolution.context is None:
        return None
    signature = (
        resolution.context.host_surface,
        resolution.context.scheduler_owner,
        resolution.context.execution_mode,
    )
    return next(
        (
            profile
            for profile, profile_signature in _SCHEDULER_RUNTIME_PROFILE_CONTEXTS.items()
            if signature == profile_signature
        ),
        None,
    )


def _render_scheduler_runtime_profile_args(
    profile: SchedulerRuntimeProfile,
) -> str:
    return f" --runtime-profile {shlex.quote(profile.value)}"


def render_scheduler_execution_args(
    *,
    runtime_profile: str | None = None,
    scheduler_execution_context: (
        Mapping[str, Any] | SchedulerExecutionContextResolution | None
    ) = None,
) -> str:
    if runtime_profile and scheduler_execution_context:
        raise ValueError(
            "runtime_profile and scheduler_execution_context are mutually exclusive"
        )
    if runtime_profile:
        try:
            profile = SchedulerRuntimeProfile(runtime_profile)
        except ValueError as exc:
            raise ValueError(
                f"unsupported scheduler runtime profile: {runtime_profile}"
            ) from exc
        return _render_scheduler_runtime_profile_args(profile)
    if scheduler_execution_context is None:
        return ""
    resolution = resolve_scheduler_execution_context(scheduler_execution_context)
    if not resolution.ok or resolution.context is None:
        raise ValueError(
            "invalid scheduler_execution_context: " + "; ".join(resolution.errors)
        )
    profile = scheduler_runtime_profile_for_execution_context(resolution)
    if profile is not None:
        return _render_scheduler_runtime_profile_args(profile)
    context = resolution.context
    return "".join(
        (
            f" -H {shlex.quote(context.host_surface.value)}",
            f" -O {shlex.quote(context.scheduler_owner.value)}",
            f" -M {shlex.quote(context.execution_mode.value)}",
        )
    )


def scheduler_execution_context_for_turn(
    *,
    host: str,
    execution_mode: str,
    scheduler_owner: str | None = None,
) -> SchedulerExecutionContextResolution:
    host_surface = {
        "codex-cli": HostSurface.CODEX_CLI.value,
        "generic-cli": HostSurface.GENERIC_CLI.value,
        "claude-code": HostSurface.CLAUDE_CODE.value,
        "pi": HostSurface.GENERIC_CLI.value,
        # The built-in dsh host runs one bounded in-process attempt under the
        # generic-cli scheduling surface, same as the pi visible alias.
        "dsh": HostSurface.GENERIC_CLI.value,
    }.get(host, host)
    normalized_mode = {
        "interactive-visible": ExecutionMode.INTERACTIVE.value,
        "isolated-headless": ExecutionMode.ISOLATED_HEADLESS.value,
    }.get(execution_mode, execution_mode)
    owner = scheduler_owner or (
        SchedulerOwner.OUTER_CONTROLLER.value
        if host_surface == HostSurface.GENERIC_CLI.value
        else SchedulerOwner.AGENT_CLI_LOOP.value
    )
    return resolve_scheduler_execution_context(
        {
            "host_surface": host_surface,
            "scheduler_owner": owner,
            "execution_mode": normalized_mode,
            "source": "loopx_turn",
        }
    )


def build_goal_runtime_continuation(
    scheduler_hint: Mapping[str, Any],
    *,
    frontier_recheck_after_seconds: int | None = None,
    default_recheck_after_seconds: int | None = None,
) -> dict[str, Any]:
    action = str(scheduler_hint.get("action") or "")
    if action == "run_now":
        disposition = GoalRuntimeContinuationDisposition.CONTINUE_NOW
    elif action == "stop_until_explicit_resume":
        disposition = GoalRuntimeContinuationDisposition.COMPLETE
    elif action in GOAL_RUNTIME_DEFER_ACTIONS:
        disposition = GoalRuntimeContinuationDisposition.DEFER
    else:
        raise ValueError(f"unsupported Goal runtime scheduler action: {action or 'missing'}")

    continuation = {
        "schema_version": GOAL_RUNTIME_CONTINUATION_SCHEMA_VERSION,
        "disposition": disposition.value,
    }
    if disposition is GoalRuntimeContinuationDisposition.DEFER:
        if (
            isinstance(frontier_recheck_after_seconds, int)
            and frontier_recheck_after_seconds > 0
        ):
            continuation["recheck_after_seconds"] = frontier_recheck_after_seconds
            continuation["recheck_source"] = "frontier_earliest_material_transition"
        else:
            if (
                not isinstance(default_recheck_after_seconds, int)
                or default_recheck_after_seconds <= 0
            ):
                raise ValueError(
                    "deferred Goal runtime continuation requires a positive recheck interval"
                )
            continuation["recheck_after_seconds"] = default_recheck_after_seconds
        continuation["wake_policy"] = "state_change_or_deadline"
    return continuation


def apply_scheduler_execution_context(
    result: dict[str, Any],
    resolution: SchedulerExecutionContextResolution,
    *,
    frontier_recheck_after_seconds: int | None = None,
    default_recheck_after_seconds: int | None = None,
) -> dict[str, Any]:
    """Scope a generic cadence hint to the selected runtime owner."""

    if not resolution.ok or resolution.context is None:
        raise ValueError("cannot apply an invalid scheduler execution context")
    context = resolution.context

    goal_runtime_continuation = (
        build_goal_runtime_continuation(
            result,
            frontier_recheck_after_seconds=frontier_recheck_after_seconds,
            default_recheck_after_seconds=default_recheck_after_seconds,
        )
        if context.scheduler_owner is SchedulerOwner.GOAL_RUNTIME
        else None
    )

    result["execution_context"] = resolution.projection()
    cold_path = result.get("cold_path_detail")
    if isinstance(cold_path, dict):
        cold_path["execution_context"] = resolution.projection()
    owner = context.scheduler_owner.value
    result["execution_phase"] = {
        "schema_version": "scheduler_execution_phase_v0",
        "host_surface": context.host_surface.value,
        "scheduler_owner": owner,
        "disposition": f"{owner}_owned",
        "completed": True,
        "apply_needed": False,
        "completion_reason": "selected scheduler owner requires no host action",
    }
    if isinstance(cold_path, dict):
        cold_path["execution_phase"] = result["execution_phase"]
    if goal_runtime_continuation is not None:
        result["goal_runtime_continuation"] = goal_runtime_continuation
    return result
