"""Offline smoke for the public-safe Codex provider routing extension."""

from __future__ import annotations

import copy
import importlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

cli = importlib.import_module("loopx_codex_provider_routing.cli")
contract = importlib.import_module("loopx_codex_provider_routing.contract")
_run_request = cli._run_request
REQUEST_SCHEMA_VERSION = contract.REQUEST_SCHEMA_VERSION
compile_catalog = contract.compile_catalog
normalize_selector_request = contract.normalize_selector_request
project_runtime_status = contract.project_runtime_status
qualify_snapshot = contract.qualify_snapshot


def _source() -> dict[str, Any]:
    request = cast(
        dict[str, Any],
        json.loads((PACKAGE_ROOT / "examples" / "request.json").read_text()),
    )
    return cast(dict[str, Any], request["source"])


def _runtime_status() -> dict[str, Any]:
    return {
        "catalog_source": _source(),
        "host_identity": {
            "state": "retained",
            "projection": "not_projected",
            "route_binding": "none",
        },
        "execution_observation": {
            "route_slug": "auto/gpt-5.6-sol",
            "modality": "text",
            "observed_at": "2026-09-01T08:00:00Z",
            "attempted_profiles": ["codex-b"],
            "selected_profile": "codex-b",
            "outcome": "success",
        },
        "account_observations": [
            {
                "profile_id": "codex-a",
                "state": "ready",
                "quota": {
                    "observed_at": "2026-09-01T08:00:00Z",
                    "windows": [
                        {
                            "id": "primary",
                            "used_percent": 25,
                            "window_minutes": 300,
                            "reset_at": "2026-09-01T10:00:00Z",
                        }
                    ],
                },
                "recent_activity": {
                    "success": 3,
                    "failed": 1,
                    "window_minutes": 200,
                },
            },
            {
                "profile_id": "codex-b",
                "state": "ready",
                "quota": None,
                "recent_activity": {
                    "success": 2,
                    "failed": 0,
                    "window_minutes": 200,
                },
            },
        ],
    }


def expect_error(action: Callable[[], Any], message: str) -> None:
    try:
        action()
    except (TypeError, ValueError):
        return
    raise AssertionError(message)


def main() -> int:
    catalog = compile_catalog(_source())
    assert catalog["credential_free"] is True
    auto = next(
        route for route in catalog["routes"] if route["slug"].startswith("auto/")
    )
    assert auto["eligible_candidates"]["image"] == ["codex-a", "codex-b"]
    assert auto["eligible_candidates"]["text"] == ["codex-a", "codex-b", "ark-text"]
    assert next(
        profile for profile in catalog["profiles"] if profile["id"] == "ark-text"
    )["tool_transports"] == ["function_call"]
    legacy_source = copy.deepcopy(_source())
    for profile in legacy_source["profiles"]:
        profile.pop("tool_transports")
    legacy_catalog = compile_catalog(legacy_source)
    assert next(
        profile for profile in legacy_catalog["profiles"] if profile["id"] == "codex-a"
    )["tool_transports"] == ["function_call"]
    legacy_standard = normalize_selector_request(
        {
            "catalog_source": legacy_source,
            "model_selector": "codex-a/gpt-5.6-sol",
            "required_tool_transport": "function_call",
        }
    )
    assert legacy_standard["eligible_candidates"] == [
        "codex-a",
        "codex-b",
        "ark-text",
    ]
    expect_error(
        lambda: normalize_selector_request(
            {
                "catalog_source": legacy_source,
                "model_selector": "codex-a/gpt-5.6-sol",
                "required_tool_transport": "custom_tool_call",
            }
        ),
        "legacy catalog inferred unproven custom_tool_call support",
    )
    assert auto["fast_candidates"] == ["codex-a", "codex-b"]
    assert auto["routing_policy"]["session_affinity"] == "hint_revalidated_per_attempt"
    assert auto["ring_id"] == "codex-accounts"
    assert auto["entrypoint"] == "affinity_then_first"
    assert auto["routing_policy"]["max_cycles"] == 1
    prefer_a = next(
        route for route in catalog["routes"] if route["slug"].startswith("codex-a/")
    )
    prefer_b = next(
        route for route in catalog["routes"] if route["slug"].startswith("codex-b/")
    )
    assert prefer_a["candidates"] == ["codex-a", "codex-b", "ark-text"]
    assert prefer_b["candidates"] == ["codex-b", "codex-a", "ark-text"]
    assert prefer_a["routing_mode"] == prefer_b["routing_mode"] == "preferred"
    luna = next(route for route in catalog["routes"] if route["slug"] == "gpt-5.6-luna")
    assert luna["candidates"] == ["codex-a", "codex-b"]
    assert luna["eligible_candidates"]["image"] == ["codex-a", "codex-b"]
    assert luna["reasoning_levels"] == ["low", "medium", "high", "xhigh", "max"]
    assert luna["fast_candidates"] == ["codex-a", "codex-b"]
    selector_rows = {row["slug"]: row for row in catalog["selector_rows"]}
    assert (
        len([row for row in selector_rows.values() if row["visibility"] == "visible"])
        == 8
    )
    assert selector_rows["auto/gpt-5.6-sol"]["default_service_tier"] == "default"
    assert selector_rows["fast/auto/gpt-5.6-sol"]["default_service_tier"] == "fast"
    assert selector_rows["fast/auto/gpt-5.6-sol"]["candidates"] == [
        "codex-a",
        "codex-b",
    ]
    assert "fast/gpt-5.6-luna" not in selector_rows

    normalized_fast = normalize_selector_request(
        {
            "catalog_source": _source(),
            "model_selector": "fast/codex-b/gpt-5.6-sol",
            "service_tier": "default",
        }
    )
    assert normalized_fast["normalized_model_selector"] == "codex-b/gpt-5.6-sol"
    assert normalized_fast["service_tier"] == {
        "action": "force_priority",
        "value": "priority",
    }
    assert normalized_fast["eligible_candidates"] == ["codex-b", "codex-a"]
    normalized_standard = normalize_selector_request(
        {
            "catalog_source": _source(),
            "model_selector": "codex-b/gpt-5.6-sol",
        }
    )
    assert normalized_standard["normalized_model_selector"] == ("codex-b/gpt-5.6-sol")
    assert normalized_standard["service_tier"] == {"action": "preserve"}
    normalized_code_mode = normalize_selector_request(
        {
            "catalog_source": _source(),
            "model_selector": "codex-b/gpt-5.6-sol",
            "required_tool_transport": "custom_tool_call",
        }
    )
    assert normalized_code_mode["eligible_candidates"] == ["codex-b", "codex-a"]
    assert normalized_code_mode["fallback_policy"] == "required_tool_transport_only"
    custom_preserving_source = copy.deepcopy(_source())
    custom_preserving_source["profiles"][2]["tool_transports"].append(
        "custom_tool_call"
    )
    normalized_custom_adapter = normalize_selector_request(
        {
            "catalog_source": custom_preserving_source,
            "model_selector": "auto/gpt-5.6-sol",
            "required_tool_transport": "custom_tool_call",
        }
    )
    assert normalized_custom_adapter["eligible_candidates"] == [
        "codex-a",
        "codex-b",
        "ark-text",
    ]
    normalized_standard_priority = normalize_selector_request(
        {
            "catalog_source": _source(),
            "model_selector": "auto/gpt-5.6-sol",
            "service_tier": "priority",
        }
    )
    assert normalized_standard_priority["service_tier"] == {
        "action": "preserve",
        "value": "priority",
    }
    assert normalized_standard_priority["fallback_policy"] == "fast_capable_only"
    assert normalized_standard_priority["eligible_candidates"] == [
        "codex-a",
        "codex-b",
    ]

    runtime = project_runtime_status(_runtime_status())
    assert runtime["credential_free"] is True
    assert runtime["host_identity"]["route_binding"] == "none"
    assert runtime["execution"]["selected_profile"] == "codex-b"
    assert runtime["execution"]["fallback_used"] is False
    assert runtime["accounts"][0]["quota"]["windows"][0]["remaining_percent"] == 75

    preferred_fallback = _runtime_status()
    preferred_fallback["execution_observation"].update(
        {
            "route_slug": "codex-b/gpt-5.6-sol",
            "attempted_profiles": ["codex-b", "codex-a"],
            "selected_profile": "codex-a",
        }
    )
    runtime = project_runtime_status(preferred_fallback)
    assert runtime["execution"]["fallback_used"] is True

    code_mode_to_ark = _runtime_status()
    code_mode_to_ark["execution_observation"].update(
        {
            "required_tool_transport": "custom_tool_call",
            "attempted_profiles": ["codex-a", "codex-b", "ark-text"],
            "selected_profile": "ark-text",
        }
    )
    expect_error(
        lambda: project_runtime_status(code_mode_to_ark),
        "custom_tool_call request was allowed to fall back to function-only provider",
    )

    fast_fallback = _runtime_status()
    fast_fallback["execution_observation"].update(
        {
            "route_slug": "fast/codex-b/gpt-5.6-sol",
            "attempted_profiles": ["codex-b", "codex-a"],
            "selected_profile": "codex-a",
        }
    )
    runtime = project_runtime_status(fast_fallback)
    assert runtime["route_intent"]["fast"] is True
    assert runtime["route_intent"]["selector_slug"] == ("fast/codex-b/gpt-5.6-sol")
    assert runtime["route_intent"]["legal_attempt_orders"] == [["codex-b", "codex-a"]]

    fast_to_ark = _runtime_status()
    fast_to_ark["execution_observation"].update(
        {
            "route_slug": "fast/auto/gpt-5.6-sol",
            "attempted_profiles": ["codex-a", "codex-b", "ark-text"],
            "selected_profile": "ark-text",
        }
    )
    expect_error(
        lambda: project_runtime_status(fast_to_ark),
        "Fast selector was allowed to fall back to a non-Fast provider",
    )

    ordinary_priority = _runtime_status()
    ordinary_priority["execution_observation"].update(
        {
            "fast": True,
            "attempted_profiles": ["codex-a", "codex-b"],
            "selected_profile": "codex-b",
        }
    )
    runtime = project_runtime_status(ordinary_priority)
    assert runtime["route_intent"]["fast"] is True
    assert runtime["route_intent"]["legal_attempt_orders"] == [
        ["codex-a", "codex-b"],
        ["codex-b", "codex-a"],
    ]

    ordinary_priority_to_ark = _runtime_status()
    ordinary_priority_to_ark["execution_observation"].update(
        {
            "fast": True,
            "attempted_profiles": ["codex-a", "codex-b", "ark-text"],
            "selected_profile": "ark-text",
        }
    )
    expect_error(
        lambda: project_runtime_status(ordinary_priority_to_ark),
        "ordinary selector with priority was allowed to fall back to Ark",
    )

    fast_selector_reported_standard = _runtime_status()
    fast_selector_reported_standard["execution_observation"].update(
        {
            "route_slug": "fast/auto/gpt-5.6-sol",
            "fast": False,
        }
    )
    expect_error(
        lambda: project_runtime_status(fast_selector_reported_standard),
        "Fast selector was allowed to report a non-Fast execution",
    )

    image_affinity = _runtime_status()
    image_affinity["execution_observation"]["modality"] = "image"
    runtime = project_runtime_status(image_affinity)
    assert runtime["route_intent"]["legal_attempt_orders"] == [
        ["codex-a", "codex-b"],
        ["codex-b", "codex-a"],
    ]

    host_bound_to_route = _runtime_status()
    host_bound_to_route["host_identity"]["route_binding"] = "codex-a"
    expect_error(
        lambda: project_runtime_status(host_bound_to_route),
        "host identity was allowed to bind a route",
    )

    luna_to_ark = _runtime_status()
    luna_to_ark["execution_observation"].update(
        {
            "route_slug": "gpt-5.6-luna",
            "attempted_profiles": ["codex-a", "codex-b", "ark-text"],
            "selected_profile": "ark-text",
        }
    )
    expect_error(
        lambda: project_runtime_status(luna_to_ark),
        "Luna was allowed to select Ark",
    )

    failed_execution = _runtime_status()
    failed_execution["execution_observation"]["outcome"] = "failed"
    failed_execution["execution_observation"].pop("selected_profile")
    runtime = project_runtime_status(failed_execution)
    assert "selected_profile" not in runtime["execution"]

    failed_with_selection = _runtime_status()
    failed_with_selection["execution_observation"].update(
        {"outcome": "failed", "selected_profile": None}
    )
    expect_error(
        lambda: project_runtime_status(failed_with_selection),
        "failed execution declared a selected profile",
    )

    identified_runtime = _runtime_status()
    identified_runtime["account_observations"][0]["email"] = (
        "operator" + "@" + "example.invalid"
    )
    expect_error(
        lambda: project_runtime_status(identified_runtime),
        "account identity leaked through runtime status",
    )

    repeated_ring = copy.deepcopy(_source())
    repeated_ring["rings"][0]["max_cycles"] = 2
    expect_error(
        lambda: compile_catalog(repeated_ring), "multi-cycle fallback ring was accepted"
    )

    overlapping_tail = copy.deepcopy(_source())
    overlapping_tail["routes"][1]["fallback_tail"] = ["codex-b"]
    expect_error(
        lambda: compile_catalog(overlapping_tail),
        "fallback tail overlapping the account ring was accepted",
    )

    leaked = copy.deepcopy(_source())
    leaked["profiles"][0]["access_token"] = "example-sensitive-value"
    expect_error(
        lambda: compile_catalog(leaked), "credential-shaped field was accepted"
    )

    identified = copy.deepcopy(_source())
    identified["profiles"][0]["id"] = "operator" + "@" + "example.invalid"
    expect_error(lambda: compile_catalog(identified), "account identity was accepted")

    ambiguous_boolean = copy.deepcopy(_source())
    ambiguous_boolean["profiles"][0]["supports_fast"] = "false"
    expect_error(
        lambda: compile_catalog(ambiguous_boolean), "string boolean was accepted"
    )

    unsafe_fast_fallback = copy.deepcopy(_source())
    unsafe_fast_fallback["routes"][0]["fast_selector"]["fallback_policy"] = (
        "route_default"
    )
    expect_error(
        lambda: compile_catalog(unsafe_fast_fallback),
        "Fast selector was allowed to retain a non-Fast fallback",
    )

    colliding_fast_slug = copy.deepcopy(_source())
    colliding_fast_slug["routes"].append(
        {
            "candidates": ["ark-text"],
            "display_name": "Collision",
            "input_modalities": ["text"],
            "mode": "manual",
            "reasoning_levels": ["low"],
            "slug": "fast/auto/gpt-5.6-sol",
            "supports_fast": False,
            "visible": False,
        }
    )
    expect_error(
        lambda: compile_catalog(colliding_fast_slug),
        "generated Fast selector collision was accepted",
    )

    no_image_provider = copy.deepcopy(_source())
    no_image_provider["profiles"][0]["input_modalities"] = ["text"]
    no_image_provider["profiles"][1]["input_modalities"] = ["text"]
    expect_error(
        lambda: compile_catalog(no_image_provider),
        "image route without an eligible image provider was accepted",
    )

    divergent_alias = copy.deepcopy(_source())
    divergent_alias["routes"][-1]["supports_fast"] = False
    expect_error(
        lambda: compile_catalog(divergent_alias), "divergent alias was accepted"
    )

    response = _run_request(
        {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "operation": "qualify_snapshot",
            "snapshot": json.loads((PACKAGE_ROOT / "examples/qualification-snapshot.json").read_text())["snapshot"],
        }
    )
    assert response["ok"] is True and response["result"]["qualified"] is True
    for example_name in (
        "request.json",
        "normalize-request.json",
        "runtime-status.json",
        "qualification-snapshot.json",
        "outage-recovery.json",
        "quota-recovery.json",
        "tool-transport.json",
        "integration-candidate.json",
        "upgrade-request.json",
        "cli-plan.json",
    ):
        example_request = json.loads(
            (PACKAGE_ROOT / "examples" / example_name).read_text()
        )
        example_response = _run_request(example_request)
        assert example_response["ok"] is True, example_name
    expect_error(
        lambda: _run_request(
            {
                "schema_version": REQUEST_SCHEMA_VERSION,
                "operation": "qualify_snapshot",
                "snapshot": json.loads((PACKAGE_ROOT / "examples/qualification-snapshot.json").read_text())["snapshot"],
                "source": {},
            }
        ),
        "operation accepted an unrelated payload field",
    )
    print("ok: codex provider routing extension smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
