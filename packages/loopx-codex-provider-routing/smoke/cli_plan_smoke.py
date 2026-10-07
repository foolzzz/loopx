"""Native launch declaration, conjunctive admission and no-effect regression."""

from __future__ import annotations

import copy
import importlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))
cli = importlib.import_module("loopx_codex_provider_routing.cli")
contract = importlib.import_module("loopx_codex_provider_routing.contract")


def rejects(payload):
    try:
        cli._run_request(payload)
    except (TypeError, ValueError):
        return
    raise AssertionError("invalid launch declaration was accepted")


def main():
    request = json.loads((PACKAGE_ROOT / "examples/cli-plan.json").read_text())
    result = cli._run_request(request)["result"]
    assert result["eligible_candidates"] == ["codex-a", "codex-b", "codex-c"]
    assert result["online_qualification"] == "held"
    assert "actual_slot" not in result
    assert result["provider_overrides"] == {}
    fast = copy.deepcopy(request)
    fast["cli_plan"]["model_selector"] = "fast/auto/gpt-5.6-sol"
    assert cli._run_request(fast)["result"]["service_tier"] == "priority"
    for field in (
        "route_id",
        "routing_revision",
        "deployment_ref",
        "provider_id",
        "model_selector",
    ):
        bad = copy.deepcopy(request)
        bad["cli_plan"][field] += "\n"
        rejects(bad)
    for field, value in (
        ("deployment_ref", "/private/synthetic"),
        ("provider_id", "../escape"),
        ("reasoning_effort", "unknown"),
    ):
        bad = copy.deepcopy(request)
        bad["cli_plan"][field] = value
        rejects(bad)
    for field in (
        "sandbox",
        "approval_policy",
        "mcp_servers",
        "hooks",
        "provider_overrides",
        "prompt",
        "raw_body",
        "actual_slot",
    ):
        bad = copy.deepcopy(request)
        bad["cli_plan"][field] = "synthetic"
        rejects(bad)
    # A fast-only text candidate and an image-only ordinary candidate cannot
    # jointly satisfy one image+priority request. All constraints are per slot.
    mismatched = copy.deepcopy(request)
    source = mismatched["cli_plan"]["catalog_source"]
    source["profiles"][0]["input_modalities"] = ["text"]
    source["profiles"][1]["supports_fast"] = False
    for route in source["routes"]:
        route["input_modalities"] = ["text"]
    mismatched["cli_plan"]["required_capabilities"]["modalities"] = ["image"]
    mismatched["cli_plan"]["service_tier"] = "priority"
    rejects(mismatched)
    # A text-compatible heterogeneous declaration never qualifies a CLI launch
    # while online history/failover acceptance is held.
    heterogeneous = copy.deepcopy(request)
    heterogeneous["cli_plan"]["catalog_source"] = json.loads(
        (PACKAGE_ROOT / "examples/request.json").read_text()
    )["source"]
    heterogeneous["cli_plan"]["required_capabilities"] = {
        "modalities": ["text"],
        "tool_transport": "function_call",
    }
    rejects(heterogeneous)
    snapshot = json.loads(
        (PACKAGE_ROOT / "examples/qualification-snapshot.json").read_text()
    )
    assert cli._run_request(snapshot)["result"]["qualified"]
    snapshot["snapshot"]["provider_readback_matches"] = False
    assert not cli._run_request(snapshot)["result"]["qualified"]
    # After imports and caller input loading, pure operations must neither read
    # files/env secrets nor open sockets or start a subprocess.
    examples = [
        json.loads(path.read_text())
        for path in (PACKAGE_ROOT / "examples").glob("*.json")
    ]
    with (
        patch("builtins.open", side_effect=AssertionError("filesystem effect")),
        patch("pathlib.Path.open", side_effect=AssertionError("filesystem effect")),
        patch("socket.socket", side_effect=AssertionError("network effect")),
        patch("subprocess.Popen", side_effect=AssertionError("process effect")),
        patch("os.getenv", side_effect=AssertionError("environment credential access")),
    ):
        for payload in examples:
            assert cli._run_request(payload)["ok"]
    assert "loopx_codex_provider_routing.operator" not in sys.modules
    print("ok: native CLI plan, offline qualification and pure effects (11 operations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
