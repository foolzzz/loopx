"""Closed public wire schemas and a dependency-free validator for that subset.

The definitions describe declared contracts, never inferred runtime output.
Semantic route eligibility and recovery transitions remain with their owners.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

REQUEST_SCHEMA_VERSION = "loopx_codex_provider_routing_request_v1"
RESPONSE_SCHEMA_VERSION = "loopx_codex_provider_routing_response_v1"
EXTENSION_ID = "loopx-codex-provider-routing"


def obj(properties: dict[str, Any], optional: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": [key for key in properties if key not in optional],
            "additionalProperties": False}


def enum(*values: Any) -> dict[str, Any]:
    return {"enum": list(values)}


def fixed(value: Any) -> dict[str, Any]:
    return {"const": value}


def ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/$defs/{name}"}


def array(items: dict[str, Any], minimum: int = 0, unique: bool = False) -> dict[str, Any]:
    return {"type": "array", "items": items, "minItems": minimum,
            "uniqueItems": unique}


def nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


BOOL = {"type": "boolean"}
COUNT = {"type": "integer", "minimum": 0}
POSITIVE = {"type": "integer", "minimum": 1}
ID = {"type": "string", "pattern": r"^(?!.*\bsk-[A-Za-z0-9_-]{12,})[a-z0-9][a-z0-9-]{0,63}$"}
# Keep identifiers symbolic: no path roots, traversal, credentials or free text.
MODEL = {"type": "string", "pattern": r"^(?!.*\bsk-[A-Za-z0-9_-]{12,})(?!.*\.\.)[a-z0-9][a-z0-9./-]{0,127}$"}
REF = {"type": "string", "pattern": r"^(?!.*\bsk-[A-Za-z0-9_-]{12,})(?!.*\.\.)(?!.*(?:\.lock|\.)$)[A-Za-z0-9][A-Za-z0-9._-]{0,191}$"}
SHA = {"type": "string", "pattern": r"^[0-9a-f]{40}$"}
CODE = {"type": "string", "pattern": r"^(?!.*\bsk-[A-Za-z0-9_-]{12,})[a-z][a-z0-9_]{0,95}$"}
LABEL = {"type": "string", "minLength": 1, "maxLength": 120,
         "pattern": r"^(?! +$)(?!.*(?:@|[Bb][Ee][Aa][Rr][Ee][Rr] |\bsk-[A-Za-z0-9_-]{12,}|/|\\|\n|\r))[A-Za-z0-9 ·→—()_.:+-]+$"}
TIMESTAMP = {"type": "string", "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$"}
PERCENT = {"type": "number", "minimum": 0, "maximum": 100}
MODALITIES = array(enum("text", "image"), 1, True)
TRANSPORT = enum("function_call", "custom_tool_call")
IDS = array(ID, unique=True)
MODELS = array(MODEL, unique=True)
REASONING = enum("none", "minimal", "low", "medium", "high", "xhigh")
SEAMS = enum("history_projection", "integration_candidate", "modality_routing",
             "model_catalog", "quota_recovery", "request_normalizer", "retry_policy",
             "route_fallback", "sse_lifecycle", "transport_pool", "tool_transport",
             "cli_configuration")
SEAM_LIST = array(SEAMS, unique=True)


def keyed(key: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "propertyNames": key, "additionalProperties": value}


DEFS: dict[str, Any] = {
    "symbolic_id": ID, "model_selector": MODEL, "public_ref": REF, "git_sha": SHA,
    "code": CODE, "display_label": LABEL, "timestamp": TIMESTAMP,
    "percent": PERCENT, "count": COUNT, "positive_integer": POSITIVE,
    "modalities": MODALITIES, "tool_transport": TRANSPORT,
    "reasoning_effort": REASONING, "change_seam": SEAMS,
    "catalog_reasoning": enum("low", "medium", "high", "xhigh", "max", "ultra"),
}
ID, MODEL, REF, SHA = map(ref, ("symbolic_id", "model_selector", "public_ref", "git_sha"))
CODE, LABEL, TIMESTAMP = map(ref, ("code", "display_label", "timestamp"))
PERCENT, COUNT, POSITIVE = map(ref, ("percent", "count", "positive_integer"))
MODALITIES, TRANSPORT, REASONING, SEAMS = map(
    ref, ("modalities", "tool_transport", "reasoning_effort", "change_seam"))
IDS, MODELS, SEAM_LIST = array(ID, unique=True), array(MODEL, unique=True), array(SEAMS, unique=True)
DEFS["profile"] = obj({"id": ID, "provider": enum("codex", "openai_compatibility"),
                       "priority": {"type": "integer"}, "input_modalities": MODALITIES,
                       "tool_transports": array(TRANSPORT, 1, True), "supports_fast": BOOL},
                      ("tool_transports", "supports_fast"))
DEFS["compiled_profile"] = obj(DEFS["profile"]["properties"])
DEFS["ring"] = obj({"id": ID, "members": array(ID, 2, True), "max_cycles": fixed(1)})
DEFS["fast_selector"] = obj({"display_name": LABEL,
                            "fallback_policy": fixed("fast_capable_only")})
DEFS["source_route"] = obj({
    "slug": MODEL, "display_name": LABEL, "mode": enum("auto", "preferred", "manual", "alias"),
    "visible": BOOL, "input_modalities": MODALITIES,
    "reasoning_levels": array(ref("catalog_reasoning"), unique=True), "supports_fast": BOOL,
    "ring": ID, "entrypoint": ID, "fallback_tail": IDS, "candidates": IDS,
    "alias_for": MODEL, "fast_selector": ref("fast_selector"),
}, ("visible", "reasoning_levels", "supports_fast", "ring", "entrypoint", "fallback_tail",
    "candidates", "alias_for", "fast_selector"))
DEFS["source_route"]["properties"]["entrypoint"] = {
    "anyOf": [ID, fixed("affinity_then_first")]}
DEFS["catalog_source"] = obj({"profiles": array(ref("profile"), 1),
                              "rings": array(ref("ring")),
                              "routes": array(ref("source_route"), 1)}, ("rings",))
DEFS["required_capabilities"] = obj({"modalities": MODALITIES, "tool_transport": TRANSPORT})
PLAN_FIELDS = {"route_id": ID, "routing_revision": ID, "deployment_ref": ID,
               "provider_id": ID, "model_selector": MODEL, "reasoning_effort": REASONING,
               "service_tier": enum("default", "priority"),
               "required_capabilities": ref("required_capabilities"),
               "codex_version_requirement": fixed("0.160.0")}
DEFS["cli_plan_input"] = obj({"catalog_source": ref("catalog_source"), **PLAN_FIELDS})
DEFS["cli_plan_result"] = obj({"schema_version": fixed("codex_cli_route_launch_plan_v1"),
                               **PLAN_FIELDS, "eligible_candidates": array(ID, 1, True),
                               "qualification": fixed("declaration_only"),
                               "online_qualification": fixed("held"),
                               "provider_overrides": obj({})})
DEFS["normalization_input"] = obj({"catalog_source": ref("catalog_source"),
                                   "model_selector": MODEL, "service_tier": enum("default", "priority"),
                                   "required_tool_transport": TRANSPORT,
                                   "required_modalities": MODALITIES},
                                  ("service_tier", "required_tool_transport", "required_modalities"))
DEFS["normalization_result"] = obj({
    "original_model_selector": MODEL, "normalized_model_selector": MODEL,
    "default_service_tier": enum("default", "fast"),
    "service_tier": obj({"action": enum("preserve", "force_priority"),
                         "value": enum("default", "priority")}, ("value",)),
    "fallback_policy": enum("route_default", "fast_capable_only", "required_tool_transport_only",
                            "fast_and_required_tool_transport_only"),
    "eligible_candidates": array(ID, 1, True), "required_tool_transport": nullable(TRANSPORT),
    "required_modalities": array(enum("text", "image"), unique=True),
})
DEFS["host_identity"] = obj({"state": enum("not_applicable", "unknown", "retained"),
                             "projection": fixed("not_projected"), "route_binding": fixed("none")})
DEFS["quota_window"] = obj({"id": ID, "used_percent": PERCENT,
                            "window_minutes": POSITIVE, "reset_at": nullable(TIMESTAMP)}, ("reset_at",))
DEFS["quota_window_result"] = obj({**DEFS["quota_window"]["properties"], "remaining_percent": PERCENT})
DEFS["quota"] = obj({"observed_at": TIMESTAMP, "windows": array(ref("quota_window"))}, ("windows",))
DEFS["quota_result"] = obj({"observed_at": TIMESTAMP, "windows": array(ref("quota_window_result"))})
DEFS["activity"] = obj({"success": COUNT, "failed": COUNT, "window_minutes": POSITIVE})
ACCOUNT_FIELDS = {"profile_id": ID, "state": enum("ready", "degraded", "unavailable", "unknown"),
                  "recent_activity": ref("activity")}
DEFS["account"] = obj({**ACCOUNT_FIELDS, "quota": nullable(ref("quota"))}, ("quota",))
DEFS["account_result"] = obj({**ACCOUNT_FIELDS, "quota": nullable(ref("quota_result"))})
DEFS["execution_observation"] = obj({
    "route_slug": MODEL, "modality": enum("text", "image"), "fast": BOOL,
    "observed_at": TIMESTAMP, "attempted_profiles": array(ID, 1, True),
    "selected_profile": ID, "outcome": enum("success", "failed"),
    "required_tool_transport": TRANSPORT,
}, ("fast", "selected_profile", "required_tool_transport"))
_execution_fields = DEFS["execution_observation"]["properties"]
DEFS["execution_observation"] = {"oneOf": [
    obj({**_execution_fields, "outcome": fixed("success")}, ("fast", "required_tool_transport")),
    obj({key: value for key, value in {**_execution_fields, "outcome": fixed("failed")}.items()
         if key != "selected_profile"}, ("fast", "required_tool_transport")),
]}
DEFS["status_input"] = obj({"schema_version": fixed("codex_provider_routing_runtime_status_v1"),
                            "credential_free": fixed(True), "catalog_source": ref("catalog_source"),
                            "host_identity": ref("host_identity"),
                            "execution_observation": ref("execution_observation"),
                            "account_observations": array(ref("account"))},
                           ("schema_version", "credential_free"))
DEFS["status_result"] = obj({
    "schema_version": fixed("codex_provider_routing_runtime_status_v1"), "credential_free": fixed(True),
    "host_identity": ref("host_identity"),
    "route_intent": obj({"selector_slug": MODEL, "route_slug": MODEL,
                         "routing_mode": enum("auto", "preferred", "manual"),
                         "modality": enum("text", "image"), "fast": BOOL,
                         "required_tool_transport": nullable(TRANSPORT),
                         "legal_attempt_orders": array(IDS, 1)}),
    "execution": obj({"observed_at": TIMESTAMP, "attempted_profiles": array(ID, 1, True),
                      "outcome": enum("success", "failed"), "fallback_used": BOOL,
                      "selected_profile": ID}, ("selected_profile",)),
    "accounts": array(ref("account_result")),
})
_execution_fields = DEFS["status_result"]["properties"]["execution"]["properties"]
DEFS["execution_result"] = {"oneOf": [
    obj({**_execution_fields, "outcome": fixed("success")}),
    obj({key: value for key, value in {**_execution_fields, "outcome": fixed("failed")}.items()
         if key != "selected_profile"}),
]}
DEFS["status_result"]["properties"]["execution"] = ref("execution_result")
DEFS["snapshot_input"] = obj({"codex_cli_version": fixed("0.160.0"), "profile_config_parsed": BOOL,
                              "catalog_readback_matches": BOOL, "provider_readback_matches": BOOL})
DEFS["snapshot_result"] = obj({
    "schema_version": fixed("codex_cli_configuration_qualification_v1"), "qualified": BOOL,
    "checks": array(obj({"id": enum("cli_version", "profile_config", "catalog_readback", "provider_readback"),
                         "passed": BOOL}), 4),
    "scope": fixed("offline_configuration_only"), "online_qualification": fixed("held"),
    "effect_boundary": fixed("content_free_observation_only"),
})
DEFS["snapshot_result"]["properties"]["checks"]["maxItems"] = 4
DEFS["outage_input"] = obj({
    "outage_ended": BOOL, "outage_ended_observed_at": nullable(TIMESTAMP),
    "cooldown_source_observed_at": TIMESTAMP, "cooldown_expires_at": TIMESTAMP,
    "cooldown_invalidated": BOOL,
    "post_recovery_probe": enum("not_attempted", "success", "still_outage", "transport_failed"),
    "degraded_fallback_binding_cleared": BOOL, "native_capability_requested": BOOL,
    "fallback_attempted": BOOL,
}, ("outage_ended_observed_at",))
_outage_fields = DEFS["outage_input"]["properties"]
DEFS["outage_input"] = {"oneOf": [
    obj({**_outage_fields, "outage_ended": fixed(True), "outage_ended_observed_at": TIMESTAMP}),
    obj({**_outage_fields, "outage_ended": fixed(False)}, ("outage_ended_observed_at",)),
]}
DEFS["quota_input"] = obj({
    "reset_outcome": enum("applied", "not_applied"), "reset_observed_at": TIMESTAMP,
    "cooldown_source_observed_at": TIMESTAMP, "cooldown_expires_at": TIMESTAMP,
    "cooldown_invalidated": BOOL, "fallback_attempted": BOOL,
    "post_reset_probe": enum("not_attempted", "success", "quota_limited", "transport_failed"),
})
DEFS["tool_input"] = obj({"requested_transport": TRANSPORT, "observed_transport": TRANSPORT,
                          "dispatch_outcome": enum("completed", "not_dispatched", "rejected_incompatible_payload")})
STREAM_BOOLEANS = ("events_forwarded_incrementally", "same_session", "same_home", "history_preserved",
                   "text_response_completed", "tool_round_trip_completed", "settings_readback_matches")
DEFS["stream_input"] = obj({"failure_kind": fixed("sse_idle_timeout"),
                            "previous_idle_timeout_ms": POSITIVE, "effective_idle_timeout_ms": POSITIVE,
                            "observed_idle_gap_ms": POSITIVE, "previous_stream_max_retries": COUNT,
                            "effective_stream_max_retries": COUNT,
                            **{key: BOOL for key in STREAM_BOOLEANS},
                            "terminal_event_source": enum("upstream", "adapter", "missing")})
DEFS["check"] = obj({"id": CODE, "passed": BOOL, "failure_code": CODE})
QUALIFICATION = {"qualified": BOOL, "failure_codes": array(CODE), "checks": array(ref("check")),
                 "effect_boundary": fixed("content_free_observation_only")}
DEFS["outage_result"] = obj({
    **QUALIFICATION, "schema_version": fixed("codex_outage_recovery_qualification_v0"),
    "outage_ended": BOOL, "expected_action": enum("invalidate_cooldown_and_revalidate_affinity",
                                                "retain_cooldown_until_recovery_observed"),
    "responsible_layer": fixed("cpa_provider_health_state_and_selector"),
    "required_contract": obj({"incident_cooldown": fixed("outage_end_newer_than_source_invalidates_cooldown"),
                              "recovery_gate": fixed("bounded_probe_before_fallback_admission"),
                              "degraded_affinity": fixed("fallback_binding_revalidated_before_native_capability_request"),
                              "native_capability": fixed("fail_closed_when_no_eligible_native_provider")}),
})
DEFS["quota_recovery_result"] = obj({
    **QUALIFICATION, "schema_version": fixed("codex_quota_recovery_qualification_v0"),
    "reset_supersedes_cooldown": BOOL, "expected_action": enum("invalidate_and_probe", "retain_cooldown"),
    "responsible_layer": fixed("cpa_provider_health_state"),
    "required_contract": obj({"reset_receipt_ordering": fixed("newer_reset_invalidates_older_cooldown"),
                              "fallback_gate": fixed("probe_recovered_account_before_fallback"),
                              "new_quota_limit": fixed("a_new_probe_may_create_a_new_cooldown")}),
})
DEFS["tool_result"] = obj({
    **QUALIFICATION, "schema_version": fixed("codex_tool_transport_qualification_v0"),
    "responsible_layer": enum("provider_response_adapter", "codex_cli_tool_dispatch"),
    "required_contract": obj({"admission_filter": fixed("required_tool_transport"),
                              "custom_tool_call": fixed("preserve_raw_code_payload_and_item_type"),
                              "on_no_eligible_provider": fixed("fail_closed_before_first_output")}),
})
DEFS["stream_result"] = obj({
    **QUALIFICATION, "schema_version": fixed("codex_stream_recovery_qualification_v0"),
    "responsible_layer": fixed("provider_stream_transport"),
    "required_contract": obj({"idle_timeout_setting": fixed("stream_idle_timeout_ms"),
                              "retry_setting": fixed("stream_max_retries"), "retry_only_repair": fixed(False),
                              "completion_must_come_from_upstream": fixed(True),
                              "session_store_mutation": fixed("none"), "provider_internal_cause": fixed("unverified")}),
})
DEFS["upgrade_input"] = obj({"current_ref": REF, "target_ref": REF, "changed_seams": SEAM_LIST})
DEFS["upgrade_result"] = obj({**DEFS["upgrade_input"]["properties"], "steps": array(CODE, 1, True),
                              "required_checks": array(CODE, 1, True),
                              "rollback_trigger": fixed("any_failed_check_or_unexplained_regression")})
DEFS["integration_source"] = obj({"id": ID, "kind": enum("pull_request", "public_safe_patch"),
                                  "ref": REF, "head_sha": SHA, "changed_seams": array(SEAMS, 1, True)})
DEFS["integration_receipt"] = obj({"base_sha": SHA, "integration_sha": SHA,
                                   "source_heads": keyed(ID, SHA)})
DEFS["integration_input"] = obj({"base_ref": REF, "integration_branch": REF,
                                 "required_seams": array(SEAMS, 1, True),
                                 "sources": array(ref("integration_source"), 2),
                                 "observed": ref("integration_receipt"), "last_sync": ref("integration_receipt")})
DEFS["drift"] = {"oneOf": [
    obj({"kind": enum("base_moved", "integration_head_moved"), "ref": REF,
         "last_sync_sha": SHA, "observed_sha": SHA}),
    obj({"kind": fixed("source_moved"), "source_id": ID, "last_sync_sha": SHA, "observed_sha": SHA}),
]}
DEFS["integration_result"] = obj({
    "schema_version": fixed("codex_provider_integration_candidate_v0"),
    "status": enum("sync_required", "in_sync"), "sync_required": BOOL,
    "drift_reasons": array(ref("drift")), "required_seams": array(SEAMS, 1, True),
    "covered_seams": array(SEAMS, 1, True), "source_order": array(ref("integration_source"), 2),
    "core_integration_plan": obj({"base_ref": REF, "integration_branch": REF,
                                  "source_refs": array(REF, 2, True)}),
    "reconcile_steps": array(CODE, 1, True),
    "deployment_contract": obj({"artifact": fixed("content_addressed_binary_with_sha256"),
                                 "sequence": array(CODE, 1, True),
                                 "rollback_trigger": fixed("any_failed_readback_or_unexplained_regression"),
                                 "session_store_policy": fixed("preserve_in_place_never_copy_or_delete")}),
    "effect_boundary": fixed("read_only_public_safe_plan"),
})
DEFS["routing_policy"] = obj({
    "candidate_filter": fixed("required_modalities_service_tier_and_tool_transport"),
    "on_no_eligible_provider": fixed("fail_closed_before_first_output"),
    "session_affinity": enum("hint_revalidated_per_attempt", "disabled"),
    "traversal": enum("one_ring_pass_then_tail", "ordered_candidates_once"), "max_cycles": fixed(1),
    "commit_barrier": fixed("before_first_visible_output_or_tool_call"),
    "foreign_history": fixed("normalize_or_quarantine"),
})
DEFS["route_result"] = obj({
    "slug": MODEL, "display_name": LABEL, "visibility": enum("visible", "hidden"),
    "routing_mode": enum("auto", "preferred", "manual", "alias"), "alias_for": nullable(MODEL),
    "ring_id": nullable(ID), "entrypoint": nullable({"anyOf": [ID, fixed("affinity_then_first")]}),
    "fallback_tail": IDS, "input_modalities": MODALITIES,
    "reasoning_levels": array(ref("catalog_reasoning"), unique=True), "candidates": IDS,
    "eligible_candidates": keyed(enum("text", "image"), IDS), "supports_fast": BOOL,
    "fast_selector": nullable(ref("fast_selector")), "fast_candidates": IDS,
    "routing_policy": ref("routing_policy"),
})
DEFS["selector_result"] = obj({
    "slug": MODEL, "route_slug": MODEL, "display_name": LABEL, "visibility": enum("visible", "hidden"),
    "input_modalities": MODALITIES, "reasoning_levels": array(ref("catalog_reasoning"), unique=True),
    "candidates": IDS, "default_service_tier": enum("default", "fast"),
    "request_service_tier_action": enum("preserve", "force_priority"),
    "fallback_policy": enum("route_default", "fast_capable_only"),
})
DEFS["catalog_result"] = obj({
    "schema_version": fixed("codex_provider_routing_catalog_v1"), "credential_free": fixed(True),
    "default_service_tier": fixed("default"), "fast_selector_prefix": fixed("fast/"),
    "fast_request_service_tier": fixed("priority"), "profiles": array(ref("compiled_profile"), 1),
    "rings": array(ref("ring")), "routes": array(ref("route_result"), 1),
    "selector_rows": array(ref("selector_result"), 1),
})

OPERATIONS = {
    "compile_catalog": ("source", "catalog_source", "catalog_result"),
    "compile_cli_plan": ("cli_plan", "cli_plan_input", "cli_plan_result"),
    "normalize_selector_request": ("normalization", "normalization_input", "normalization_result"),
    "project_runtime_status": ("status", "status_input", "status_result"),
    "qualify_snapshot": ("snapshot", "snapshot_input", "snapshot_result"),
    "qualify_outage_recovery": ("outage_recovery", "outage_input", "outage_result"),
    "qualify_quota_recovery": ("quota_recovery", "quota_input", "quota_recovery_result"),
    "qualify_tool_transport": ("tool_transport", "tool_input", "tool_result"),
    "qualify_stream_recovery": ("stream_recovery", "stream_input", "stream_result"),
    "reconcile_integration_candidate": ("integration", "integration_input", "integration_result"),
    "upgrade_plan": ("upgrade", "upgrade_input", "upgrade_result"),
}


def _document(branches: list[dict[str, Any]]) -> dict[str, Any]:
    # Publish only reachable definitions; request and response keep the same owner.
    reachable: dict[str, Any] = {}

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].rsplit("/", 1)[-1]
                if name not in reachable:
                    reachable[name] = DEFS[name]
                    collect(DEFS[name])
            for child in node.values():
                collect(child)
        elif isinstance(node, list):
            for child in node:
                collect(child)

    collect(branches)
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": deepcopy(reachable), "oneOf": branches}


def request_schema() -> dict[str, Any]:
    return _document([obj({"schema_version": fixed(REQUEST_SCHEMA_VERSION), "operation": fixed(operation),
                           field: ref(input_name)})
                      for operation, (field, input_name, _) in OPERATIONS.items()])


def response_schema() -> dict[str, Any]:
    common = {"schema_version": fixed(RESPONSE_SCHEMA_VERSION), "extension_id": fixed(EXTENSION_ID)}
    branches = [obj({**common, "ok": fixed(True), "request_schema_version": fixed(REQUEST_SCHEMA_VERSION),
                     "operation": fixed(operation), "result": ref(output_name)})
                for operation, (_, _, output_name) in OPERATIONS.items()]
    branches += [obj({**common, "ok": fixed(False), "error": fixed("invalid_request")}),
                 obj({**common, "ok": fixed(True), "doctor": fixed("ready"),
                      "operations": array(enum(*OPERATIONS), len(OPERATIONS), True),
                      "effect_boundary": fixed("read_only_public_safe")})]
    branches[-1]["properties"]["operations"]["maxItems"] = len(OPERATIONS)
    return _document(branches)


def _equal(left: Any, right: Any) -> bool:
    # JSON booleans are distinct from numbers (Python's True == 1 is not).
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return left == right


def _validate(value: Any, schema: Any, path: str, definitions: Mapping[str, Any]) -> None:
    def fail() -> None:
        raise ValueError(f"schema mismatch at {path}")

    if schema is False:
        fail()
    if schema is True:
        return
    if "$ref" in schema:
        _validate(value, definitions[schema["$ref"].rsplit("/", 1)[-1]], path, definitions)
    for keyword in ("oneOf", "anyOf"):
        if keyword in schema:
            matches = 0
            for branch in schema[keyword]:
                try:
                    _validate(value, branch, path, definitions)
                    matches += 1
                except ValueError:
                    pass
            if (keyword == "oneOf" and matches != 1) or (keyword == "anyOf" and not matches):
                fail()
    if "const" in schema and not _equal(value, schema["const"]):
        fail()
    if "enum" in schema and not any(_equal(value, item) for item in schema["enum"]):
        fail()
    expected = schema.get("type")
    valid_types = {"object": isinstance(value, Mapping), "array": isinstance(value, list),
                   "string": isinstance(value, str), "boolean": type(value) is bool,
                   "integer": type(value) is int or (type(value) is float and math.isfinite(value) and value.is_integer()),
                   "number": type(value) is int or (type(value) is float and math.isfinite(value)), "null": value is None}
    if expected and not valid_types[expected]:
        fail()
    if isinstance(value, Mapping):
        if any(key not in value for key in schema.get("required", ())):
            fail()
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, child in value.items():
            if "propertyNames" in schema:
                _validate(key, schema["propertyNames"], f"{path}.<key>", definitions)
            child_schema = properties.get(key, extra)
            # Never interpolate untrusted keys into errors.
            child_path = f"{path}.{key}" if key in properties else f"{path}.<field>"
            _validate(child, child_schema, child_path, definitions)
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            fail()
        if schema.get("uniqueItems"):
            for index, item in enumerate(value):
                if any(_equal(item, previous) for previous in value[:index]):
                    fail()
        for index, child in enumerate(value):
            _validate(child, schema.get("items", True), f"{path}[{index}]", definitions)
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            fail()
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            fail()
    if type(value) in (int, float):
        if (type(value) is float and not math.isfinite(value)) or value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            fail()


_REQUEST_SCHEMA = request_schema()
_RESPONSE_SCHEMA = response_schema()


def validate_payload(operation: str, payload: Any) -> None:
    if operation not in OPERATIONS:
        raise ValueError("unsupported operation")
    field, input_name, _ = OPERATIONS[operation]
    _validate(payload, ref(input_name), f"$.{field}", DEFS)


def validate_request(value: Any) -> None:
    _validate(value, _REQUEST_SCHEMA, "$", DEFS)


def validate_response(value: Any) -> None:
    _validate(value, _RESPONSE_SCHEMA, "$", DEFS)
