from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..effect_runtime import EffectRuntimeRejected, effect_runtime_result


MONITOR_SCHEDULE_REQUEST_SCHEMA = "loopx_monitor_schedule_request_v0"
MONITOR_SCHEDULE_RESULT_SCHEMA = "loopx_monitor_schedule_result_v0"


@dataclass(frozen=True)
class MonitorScheduleProjection:
    next_due_at: str | None
    schedule_source: str
    cadence_seconds: int | None


def project_monitor_todo_schedule(
    *,
    generated_at: str,
    cadence: Any = None,
    explicit_next_due_at: Any = None,
) -> MonitorScheduleProjection:
    """Project monitor due-time facts without App scheduler state."""

    try:
        result = effect_runtime_result(
            "monitor.schedule.project",
            {
                "schema_version": MONITOR_SCHEDULE_REQUEST_SCHEMA,
                "generated_at": generated_at,
                "cadence": None if cadence is None else str(cadence),
                "explicit_next_due_at": (
                    None
                    if explicit_next_due_at is None
                    else str(explicit_next_due_at)
                ),
            },
        )
    except EffectRuntimeRejected as exc:
        raise ValueError(str(exc)) from None
    if not isinstance(result, Mapping):
        raise RuntimeError("TypeScript monitor schedule result must be an object")
    next_due_at = result.get("next_due_at")
    schedule_source = result.get("schedule_source")
    cadence_seconds = result.get("cadence_seconds")
    if (
        result.get("schema_version") != MONITOR_SCHEDULE_RESULT_SCHEMA
        or (next_due_at is not None and not isinstance(next_due_at, str))
        or schedule_source not in {"explicit", "cadence", "none"}
        or (
            cadence_seconds is not None
            and (
                isinstance(cadence_seconds, bool)
                or not isinstance(cadence_seconds, int)
                or cadence_seconds <= 0
            )
        )
    ):
        raise RuntimeError("TypeScript monitor schedule result shape mismatch")
    return MonitorScheduleProjection(
        next_due_at=next_due_at,
        schedule_source=str(schedule_source),
        cadence_seconds=cadence_seconds,
    )
