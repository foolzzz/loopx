"""Offline qualification of public-safe upgrade and integration plans."""

from __future__ import annotations

import copy
import importlib
import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))
contract = importlib.import_module("loopx_codex_provider_routing.contract")
build_upgrade_plan = contract.build_upgrade_plan
reconcile_integration_candidate = contract.reconcile_integration_candidate


def expect_error(action, message):
    try:
        action()
    except (ValueError, TypeError):
        return
    raise AssertionError(message)


def main():
    plan = build_upgrade_plan(
        {
            "current_ref": "public-current-ref",
            "target_ref": "public-target-ref",
            "changed_seams": [
                "transport_pool",
                "cli_configuration",
                "modality_routing",
                "request_normalizer",
                "quota_recovery",
                "tool_transport",
            ],
        }
    )
    assert "h2_reuse" in plan["required_checks"]
    assert "profile_config" in plan["required_checks"]
    assert "no_eligible_fail_closed" in plan["required_checks"]
    assert "ordinary_selector_preserved" in plan["required_checks"]
    assert "effective_priority_admission" in plan["required_checks"]
    assert "stale_cooldown_invalidation" in plan["required_checks"]
    assert "custom_tool_item_preserved" in plan["required_checks"]

    integration_request = json.loads(
        (PACKAGE_ROOT / "examples" / "integration-candidate.json").read_text()
    )
    integration = reconcile_integration_candidate(integration_request["integration"])
    assert integration["status"] == "in_sync"
    assert integration["sync_required"] is False
    assert integration["core_integration_plan"]["source_refs"] == [
        "fork-provider-history-normalization",
        "fork-reusable-http2-transport",
        "operator-modality-routing",
        "fork-route-specific-fallback",
        "operator-compat-stream-repair",
        "fork-openai-compat-bounded-rate-limit-waits",
    ]
    assert integration["deployment_contract"]["session_store_policy"] == (
        "preserve_in_place_never_copy_or_delete"
    )

    moved_source = copy.deepcopy(integration_request["integration"])
    moved_source["observed"]["source_heads"]["transport-pool"] = (
        "9999999999999999999999999999999999999999"
    )
    moved_source["sources"][1]["head_sha"] = "9999999999999999999999999999999999999999"
    integration = reconcile_integration_candidate(moved_source)
    assert integration["sync_required"] is True
    assert integration["drift_reasons"] == [
        {
            "kind": "source_moved",
            "source_id": "transport-pool",
            "last_sync_sha": "3333333333333333333333333333333333333333",
            "observed_sha": "9999999999999999999999999999999999999999",
        }
    ]

    uncovered = copy.deepcopy(integration_request["integration"])
    uncovered["required_seams"].append("retry_policy")
    expect_error(
        lambda: reconcile_integration_candidate(uncovered),
        "integration candidate without a required seam was accepted",
    )

    print("ok: public upgrade and integration plans")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
