from __future__ import annotations

import pytest

from loopx.control_plane.scheduler import monitor_schedule
from loopx.control_plane.scheduler.monitor_schedule import project_monitor_todo_schedule


def test_monitor_schedule_facade_sends_bounded_facts(monkeypatch) -> None:
    requests: list[tuple[str, dict]] = []

    def fake_runtime(method: str, params: dict) -> dict:
        requests.append((method, params))
        return {
            "schema_version": "loopx_monitor_schedule_result_v0",
            "next_due_at": "2026-08-25T02:30:00Z",
            "schedule_source": "cadence",
            "cadence_seconds": 1800,
        }

    monkeypatch.setattr(monitor_schedule, "effect_runtime_result", fake_runtime)
    result = project_monitor_todo_schedule(
        generated_at="2026-08-25T02:00:00Z",
        cadence="30m",
    )

    assert result.next_due_at == "2026-08-25T02:30:00Z"
    assert requests == [
        (
            "monitor.schedule.project",
            {
                "schema_version": "loopx_monitor_schedule_request_v0",
                "generated_at": "2026-08-25T02:00:00Z",
                "cadence": "30m",
                "explicit_next_due_at": None,
            },
        )
    ]


def test_monitor_schedule_facade_rejects_malformed_result(monkeypatch) -> None:
    monkeypatch.setattr(
        monitor_schedule,
        "effect_runtime_result",
        lambda _method, _params: {
            "schema_version": "loopx_monitor_schedule_result_v0",
            "next_due_at": None,
            "schedule_source": "cadence",
            "cadence_seconds": True,
        },
    )

    with pytest.raises(RuntimeError, match="monitor schedule result shape mismatch"):
        project_monitor_todo_schedule(
            generated_at="2026-08-25T02:00:00Z",
            cadence="30m",
        )
