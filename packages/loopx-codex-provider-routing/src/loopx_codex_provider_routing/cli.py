from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from . import contract
from .schema_contract import validate_request, validate_response
from .stream_recovery import qualify_stream_recovery

EXTENSION_ID = "loopx-codex-provider-routing"
OPERATIONS = {
    "compile_catalog": ("source", contract.compile_catalog),
    "compile_cli_plan": ("cli_plan", contract.compile_cli_plan),
    "normalize_selector_request": (
        "normalization",
        contract.normalize_selector_request,
    ),
    "project_runtime_status": ("status", contract.project_runtime_status),
    "qualify_outage_recovery": ("outage_recovery", contract.qualify_outage_recovery),
    "qualify_quota_recovery": ("quota_recovery", contract.qualify_quota_recovery),
    "qualify_snapshot": ("snapshot", contract.qualify_snapshot),
    "qualify_stream_recovery": ("stream_recovery", qualify_stream_recovery),
    "qualify_tool_transport": ("tool_transport", contract.qualify_tool_transport),
    "reconcile_integration_candidate": (
        "integration",
        contract.reconcile_integration_candidate,
    ),
    "upgrade_plan": ("upgrade", contract.build_upgrade_plan),
}


def _emit(payload: Mapping[str, Any]) -> None:
    validate_response(payload)
    json.dump(payload, sys.stdout, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")


def _doctor() -> int:
    _emit(
        {
            "ok": True,
            "schema_version": contract.RESPONSE_SCHEMA_VERSION,
            "extension_id": EXTENSION_ID,
            "doctor": "ready",
            "operations": sorted(OPERATIONS),
            "effect_boundary": "read_only_public_safe",
        }
    )
    return 0


def _run_request(request: Any) -> dict[str, Any]:
    validate_request(request)
    contract.reject_private_material(request)
    field, execute = OPERATIONS[request["operation"]]
    result = execute(request[field])
    response = {
        "ok": True,
        "schema_version": contract.RESPONSE_SCHEMA_VERSION,
        "extension_id": EXTENSION_ID,
        "request_schema_version": contract.REQUEST_SCHEMA_VERSION,
        "operation": request["operation"],
        "result": result,
    }
    validate_response(response)
    return response


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=EXTENSION_ID)
    parser.add_argument(
        "--doctor", action="store_true", help="side-effect-free readiness"
    )
    args = parser.parse_args(argv)
    if args.doctor:
        return _doctor()
    try:
        _emit(_run_request(json.load(sys.stdin)))
        return 0
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        # Unknown keys and invalid values are caller-controlled: never echo them.
        _emit(
            {
                "ok": False,
                "schema_version": contract.RESPONSE_SCHEMA_VERSION,
                "extension_id": EXTENSION_ID,
                "error": "invalid_request",
            }
        )
        return 1


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
