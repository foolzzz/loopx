from __future__ import annotations

import json
from pathlib import Path

import pytest

from loopx.canary.runner import build_canary_smoke_suite_run
from loopx.canary.smoke_health import build_smoke_fleet_health
from loopx.canary.smoke_profiles import list_smoke_suite_profiles


def test_retired_full_public_workflow_has_no_receipt_producer() -> None:
    root = Path(__file__).resolve().parents[2]
    assert not (root / ".github/workflows/full-public-smokes.yml").exists()
    assert all(
        "full-public-smokes" not in profile["modules"]
        for profile in list_smoke_suite_profiles()
    )


@pytest.mark.parametrize("profiles", [[], ["public-smoke-watch"]])
def test_public_inventory_does_not_require_retired_workflow(profiles: list[str]) -> None:
    preview = build_canary_smoke_suite_run(
        suite="full-public", profiles=profiles, execute=False
    )
    scripts = {check["normalized"]["script"] for check in preview["selected_checks"]}

    assert preview["ok"] is True
    assert "examples/control_plane/cli-output-budget-regression-smoke.py" in scripts
    assert "examples/full-public-smokes-workflow-smoke.py" not in scripts


def _passing_receipt(scripts: list[str]) -> dict[str, object]:
    return {
        "schema_version": "canary_smoke_suite_run_v0",
        "suite": "full-public",
        "timeout_seconds": 120.0,
        "failure_count": 0,
        "timeout_count": 0,
        "selected_checks": [
            {
                "normalized": {"script": script},
                "status": "passed",
                "ok": True,
                "duration_seconds": float((index % 7) + 1),
            }
            for index, script in enumerate(scripts)
        ],
    }


def test_static_health_is_compact_and_classifies_cadence() -> None:
    payload = build_smoke_fleet_health()

    assert payload["ok"] is True
    assert payload["ready"] is False
    assert payload["inventory_count"] > 0
    assert payload["cadence_counts"]["daily_full_public"] == payload["inventory_count"]
    assert payload["cadence_counts"]["pr_fast"] == 1
    assert payload["cadence_counts"]["catalog_canary"] > 0
    assert payload["cadence_counts"]["release_gate"] > 0
    assert payload["targeted_owner_count"] > 0
    assert payload["owner_gap_count"] > 0
    assert payload["workflow_contract"]["missing_scripts"] == []
    assert payload["contract_reuse"]["semantic_duplicate_inference"] == "manual_review_required"
    assert "inventory" not in payload
    assert len(json.dumps(payload, ensure_ascii=False)) < 30_000


def test_receipts_prove_complete_health_without_copying_raw_output(tmp_path: Path) -> None:
    inventory_payload = build_smoke_fleet_health(include_inventory=True)
    scripts = [entry["script"] for entry in inventory_payload["inventory"]]
    receipt = _passing_receipt(scripts)
    receipt["selected_checks"][0]["stdout_tail"] = "private-looking raw output"
    receipt["selected_checks"][0]["stderr_tail"] = "/tmp/local-path"
    receipt_path = tmp_path / "full-public-shard.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    payload = build_smoke_fleet_health(receipt_paths=[receipt_path])

    assert payload["ok"] is True
    assert payload["ready"] is True
    assert payload["receipt_health"]["observed_script_count"] == len(scripts)
    assert payload["receipt_health"]["missing_script_count"] == 0
    assert payload["receipt_health"]["failure_count"] == 0
    rendered = json.dumps(payload, ensure_ascii=False)
    assert "private-looking raw output" not in rendered
    assert "/tmp/local-path" not in rendered
    assert str(tmp_path) not in rendered


def test_failed_and_invalid_receipts_remain_distinct(tmp_path: Path) -> None:
    inventory_payload = build_smoke_fleet_health(include_inventory=True)
    scripts = [entry["script"] for entry in inventory_payload["inventory"]]
    receipt = _passing_receipt(scripts)
    receipt["failure_count"] = 1
    receipt["selected_checks"][0].update({"status": "failed", "ok": False})
    failed_path = tmp_path / "failed.json"
    failed_path.write_text(json.dumps(receipt), encoding="utf-8")

    failed = build_smoke_fleet_health(receipt_paths=[failed_path])
    assert failed["ok"] is True
    assert failed["ready"] is False
    assert failed["receipt_health"]["failure_count"] == 1

    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text("{}", encoding="utf-8")
    invalid = build_smoke_fleet_health(receipt_paths=[invalid_path])
    assert invalid["ok"] is False
    assert invalid["ready"] is False
    assert invalid["warnings"][0]["kind"] == "unsupported_receipt"
