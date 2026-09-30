from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..effect_runtime import EffectRuntimeRejected, effect_runtime_result


SCHEDULER_STATE_OPERATION_REQUEST_SCHEMA = (
    "loopx_scheduler_state_operation_request_v0"
)
SCHEDULER_STATE_OPERATION_RESULT_SCHEMA = (
    "loopx_scheduler_state_operation_result_v0"
)
APP_AUTOMATION_STATEFUL_BACKOFF_STATE_KEY = (
    "scheduler_hint.app_automation.stateful_backoff"
)


def _operation_result(operation: str, **params: Any) -> Any:
    try:
        result = effect_runtime_result(
            "scheduler.state.evaluate",
            {
                "schema_version": SCHEDULER_STATE_OPERATION_REQUEST_SCHEMA,
                "operation": operation,
                **params,
            },
        )
    except EffectRuntimeRejected as exc:
        raise ValueError(str(exc)) from None
    if (
        not isinstance(result, Mapping)
        or result.get("schema_version") != SCHEDULER_STATE_OPERATION_RESULT_SCHEMA
        or result.get("operation") != operation
    ):
        raise RuntimeError("TypeScript scheduler state result shape mismatch")
    return result.get("value")


def rrule_for_minutes(minutes: int) -> str:
    value = _operation_result("rrule_for_minutes", value=minutes)
    if not isinstance(value, str):
        raise RuntimeError("TypeScript scheduler RRULE result must be a string")
    return value


def normalize_scheduler_rrule(value: Any) -> str:
    normalized = _operation_result("normalize_rrule", value=value)
    if not isinstance(normalized, str):
        raise RuntimeError("TypeScript normalized RRULE must be a string")
    return normalized
