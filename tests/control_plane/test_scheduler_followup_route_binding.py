"""Scheduler follow-up hints are bound to the registry, runtime and Turn that built them."""

from __future__ import annotations

from pathlib import Path

from loopx.control_plane.quota.live_decision import (
    bind_scheduler_followup_cli_routes,
)


def test_scheduler_followup_routes_preserve_turn_lineage(tmp_path: Path) -> None:
    payload = {
        "scheduler_hint": {
            "app_automation": {
                "ack_hint": {
                    "cli_args": [
                        "quota",
                        "scheduler-ack-current",
                        "--execute",
                    ],
                    "args": {},
                },
                "failure_hint": {
                    "cli_args": [
                        "quota",
                        "scheduler-fail-current",
                        "--execute",
                    ]
                },
            }
        }
    }
    turn_instance_id = "turn-scheduler-followup-001"

    bind_scheduler_followup_cli_routes(
        payload,
        registry_path=tmp_path / "registry.json",
        runtime_root=tmp_path / "runtime",
        turn_instance_id=turn_instance_id,
    )

    app_automation = payload["scheduler_hint"]["app_automation"]
    for hint_name in ("ack_hint", "failure_hint"):
        hint = app_automation[hint_name]
        assert hint["cli_args"][-3:] == [
            "--turn-instance-id",
            turn_instance_id,
            "--execute",
        ]
        assert hint["route_binding"]["turn_instance_bound"] is True
    assert app_automation["ack_hint"]["args"]["turn_instance_id"] == turn_instance_id
