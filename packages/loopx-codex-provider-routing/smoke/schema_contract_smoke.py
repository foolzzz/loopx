#!/usr/bin/env python3
"""Public examples, nested boundary mutations and independent schema parity."""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))

from loopx_codex_provider_routing.cli import OPERATIONS, _run_request, run  # noqa: E402
from loopx_codex_provider_routing.schema_contract import (  # noqa: E402
    EXTENSION_ID,
    REQUEST_SCHEMA_VERSION,
    RESPONSE_SCHEMA_VERSION,
    request_schema,
    response_schema,
    validate_payload,
    validate_request,
    validate_response,
)


def nodes(value: Any, path: tuple[str | int, ...] = ()):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from nodes(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from nodes(child, (*path, index))


def replace(value: Any, path: tuple[str | int, ...], new: Any) -> Any:
    result = deepcopy(value)
    if not path:
        return new
    parent = result
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = new
    return result



def field_schema(validator: Draft202012Validator, value: Any,
                 path: tuple[str | int, ...]) -> dict[str, Any]:
    schema = validator.schema
    current = value

    def resolve(schema: dict[str, Any], current: Any) -> dict[str, Any]:
        while True:
            if "$ref" in schema:
                schema = validator.schema["$defs"][schema["$ref"].rsplit("/", 1)[-1]]
            elif "oneOf" in schema or "anyOf" in schema:
                branches = schema.get("oneOf", schema.get("anyOf"))
                schema = next(branch for branch in branches
                              if validator.evolve(schema=branch).is_valid(current))
            else:
                return schema

    for key in path:
        schema = resolve(schema, current)
        schema = schema["items"] if isinstance(key, int) else schema["properties"].get(
            key, schema.get("additionalProperties"))
        current = current[key]
    return resolve(schema, current)


def rejected(validate, value: Any) -> bool:
    try:
        validate(value)
    except (TypeError, ValueError):
        return True
    return False


def main() -> None:
    schemas = {}
    for name, build in (("request", request_schema), ("response", response_schema)):
        public = json.loads((PACKAGE / "schemas" / f"{name}.schema.json").read_text())
        assert public == build(), f"stale public {name} schema"
        Draft202012Validator.check_schema(public)
        schemas[name] = Draft202012Validator(public)

    examples = [json.loads(path.read_text()) for path in sorted((PACKAGE / "examples").glob("*.json"))]
    assert {value["operation"] for value in examples} == set(OPERATIONS), "example coverage missing an operation"
    cases = 0
    for request in examples:
        schemas["request"].validate(request)
        validate_request(request)
        operation = request["operation"]
        field, execute = OPERATIONS[operation]
        validate_payload(operation, request[field])
        response = _run_request(request)
        schemas["response"].validate(response)
        validate_response(response)
        cases += 1

        # The same malformed object must fail independently in the public schema,
        # wire runtime and direct operation boundary. Dynamic key maps are tested
        # with an invalid symbolic name, not an otherwise legal new map member.
        for path, node in nodes(request):
            if isinstance(node, dict):
                malformed = replace(request, path, {**node, "raw_body": "synthetic"})
                assert not schemas["request"].is_valid(malformed), (operation, path)
                assert rejected(validate_request, malformed), (operation, path)
                assert rejected(_run_request, malformed), (operation, path)
                if path and path[0] == field:
                    assert rejected(execute, malformed[field]), (operation, path)
                cases += 1
            elif not isinstance(node, list):
                if type(node) is int:
                    expected = field_schema(schemas["request"], request, path)
                    if expected.get("type") == "integer" or type(expected.get("const")) is int:
                        # JSON Schema integer is mathematical, not a Python
                        # representation constraint. 300 and 300.0 are both legal.
                        integral = replace(request, path, float(node))
                        schemas["request"].validate(integral)
                        validate_request(integral)
                        schemas["response"].validate(_run_request(integral))
                        execute(integral[field])
                        for invalid in (True, float(node) + 0.25):
                            malformed = replace(request, path, invalid)
                            assert not schemas["request"].is_valid(malformed), (operation, path)
                            assert rejected(validate_request, malformed), (operation, path)
                            assert rejected(_run_request, malformed), (operation, path)
                        cases += 3
                malformed = replace(request, path, {})
                assert not schemas["request"].is_valid(malformed), (operation, path)
                assert rejected(validate_request, malformed), (operation, path)
                assert rejected(_run_request, malformed), (operation, path)
                cases += 1
                if isinstance(node, str):
                    # All reference and label positions reject every absolute
                    # filesystem root, including paths outside known HOME roots.
                    for unsafe in ("/opt/synthetic-private", r"C:\synthetic-private", "person@example.invalid",
                                   "Bearer synthetic-secret", "bearer synthetic-secret", "sk-synthetic-private-key"):
                        malformed = replace(request, path, unsafe)
                        assert not schemas["request"].is_valid(malformed), (operation, path)
                        assert rejected(validate_request, malformed), (operation, path)
                        cases += 1

        for path, node in nodes(response):
            if isinstance(node, dict):
                malformed = replace(response, path, {**node, "raw_body": "synthetic"})
                assert not schemas["response"].is_valid(malformed), (operation, path)
                assert rejected(validate_response, malformed), (operation, path)
                cases += 1
            elif not isinstance(node, list):
                malformed = replace(response, path, {})
                assert not schemas["response"].is_valid(malformed), (operation, path)
                assert rejected(validate_response, malformed), (operation, path)
                cases += 1

    # Negative business outcomes are valid observations; do not confuse a failed
    # qualification with malformed input. Exercise the alternate shape branches.
    failed_execution = deepcopy(next(value for value in examples
                                    if value["operation"] == "project_runtime_status"))
    observation = failed_execution["status"]["execution_observation"]
    observation["outcome"] = "failed"
    observation.pop("selected_profile")
    ongoing_outage = deepcopy(next(value for value in examples
                                  if value["operation"] == "qualify_outage_recovery"))
    observation = ongoing_outage["outage_recovery"]
    observation.update({"outage_ended": False, "outage_ended_observed_at": None,
                        "cooldown_invalidated": False})
    for request in (failed_execution, ongoing_outage):
        schemas["request"].validate(request)
        validate_request(request)
        response = _run_request(request)
        schemas["response"].validate(response)
        validate_response(response)
        cases += 1
    missing_selection = deepcopy(failed_execution)
    missing_selection["status"]["execution_observation"]["outcome"] = "success"
    ended_without_time = deepcopy(ongoing_outage)
    ended_without_time["outage_recovery"]["outage_ended"] = True
    for request in (missing_selection, ended_without_time):
        assert not schemas["request"].is_valid(request)
        assert rejected(validate_request, request)
        assert rejected(_run_request, request)
        cases += 1

    # Both non-operation branches are closed contracts as well.
    doctor = {"ok": True, "schema_version": RESPONSE_SCHEMA_VERSION,
              "extension_id": EXTENSION_ID, "doctor": "ready",
              "operations": sorted(OPERATIONS), "effect_boundary": "read_only_public_safe"}
    error = {"ok": False, "schema_version": RESPONSE_SCHEMA_VERSION,
             "extension_id": EXTENSION_ID, "error": "invalid_request"}
    for response in (doctor, error):
        schemas["response"].validate(response)
        validate_response(response)
        assert rejected(validate_response, {**response, "prompt": "synthetic"})
        cases += 1

    # Failure formatting cannot leak a caller-supplied unknown key or value.
    previous_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps({"schema_version": REQUEST_SCHEMA_VERSION,
                                      "operation": "compile_catalog", "source": {},
                                      "synthetic-private-key": "synthetic-private-value"}))
    output = io.StringIO()
    try:
        with redirect_stdout(output):
            assert run([]) == 1
    finally:
        sys.stdin = previous_stdin
    assert json.loads(output.getvalue()) == error
    cases += 1
    print(f"schema contract smoke: {len(examples)} operations, {cases} checks passed")


if __name__ == "__main__":
    main()
