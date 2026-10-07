from __future__ import annotations

import re
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from itertools import pairwise
from typing import Any

from .schema_contract import REQUEST_SCHEMA_VERSION as REQUEST_SCHEMA_VERSION
from .schema_contract import RESPONSE_SCHEMA_VERSION as RESPONSE_SCHEMA_VERSION

CATALOG_SCHEMA_VERSION = "codex_provider_routing_catalog_v1"
RUNTIME_STATUS_SCHEMA_VERSION = "codex_provider_routing_runtime_status_v1"
INTEGRATION_CANDIDATE_SCHEMA_VERSION = "codex_provider_integration_candidate_v0"
QUOTA_RECOVERY_SCHEMA_VERSION = "codex_quota_recovery_qualification_v0"
TOOL_TRANSPORT_SCHEMA_VERSION = "codex_tool_transport_qualification_v0"
OUTAGE_RECOVERY_SCHEMA_VERSION = "codex_outage_recovery_qualification_v0"

FORBIDDEN_KEYS = {
    "account_id",
    "api_key",
    "access_token",
    "auth_file",
    "auth_index",
    "refresh_token",
    "authorization",
    "cookie",
    "email",
    "filename",
    "password",
    "project_id",
    "secret",
    "session_id",
    "task_id",
    "token",
}
ALLOWED_MODALITIES = {"text", "image"}
ALLOWED_PROVIDERS = {"codex", "openai_compatibility"}
ALLOWED_TOOL_TRANSPORTS = {"function_call", "custom_tool_call"}
ALLOWED_REASONING_LEVELS = {"low", "medium", "high", "xhigh", "max", "ultra"}
ALLOWED_CHANGE_SEAMS = {
    "cli_configuration",
    "history_projection",
    "integration_candidate",
    "modality_routing",
    "model_catalog",
    "quota_recovery",
    "request_normalizer",
    "retry_policy",
    "route_fallback",
    "sse_lifecycle",
    "transport_pool",
    "tool_transport",
}
SYMBOLIC_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MODEL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9./-]{0,127}$")
GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE),
    re.compile(r"(?:^|\s)/(?:Users|home|var/folders)/"),
    re.compile("codex" + r"://threads/", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{8,}", re.IGNORECASE),
    re.compile(r"\b" + "sk-" + r"[A-Za-z0-9_-]{12,}"),
)


def reject_private_material(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            if key.lower() in FORBIDDEN_KEYS:
                raise ValueError(f"credential-like field forbidden at {path}.{key}")
            reject_private_material(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_private_material(child, f"{path}[{index}]")
    elif isinstance(value, str):
        for pattern in SENSITIVE_VALUE_PATTERNS:
            if pattern.search(value):
                raise ValueError(f"private-looking string forbidden at {path}")


def _non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a string list")
    if len(value) != len(set(value)):
        raise ValueError(f"{field} must not contain duplicates")
    return list(value)


def _integer(value: Any, field: str) -> int:
    # JSON Schema integers include integral JSON numbers such as 300.0.
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value) and value.is_integer():
        return int(value)
    raise TypeError(f"{field} must be an integer")


def _boolean(value: Any, field: str, *, default: bool | None = None) -> bool:
    if value is None and default is not None:
        return default
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be a boolean")
    return value


def _utc_datetime(value: Any, field: str) -> datetime:
    timestamp = _timestamp(value, field)
    return datetime.fromisoformat(timestamp)


def qualify_outage_recovery(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Qualify state transitions after a provider-wide outage ends.

    A provider incident (for example repeated 4xx/5xx across every native
    profile) creates two kinds of state: a cooldown on native profiles and a
    degraded affinity to a text-only fallback. Once a recovery signal newer
    than the cooldown source is observed, both must be revalidated before a
    request that needs native capabilities is admitted.
    """

    from .schema_contract import validate_payload

    validate_payload("qualify_outage_recovery", observation)
    reject_private_material(observation)
    _reject_unexpected_keys(
        observation,
        {
            "outage_ended",
            "outage_ended_observed_at",
            "cooldown_source_observed_at",
            "cooldown_expires_at",
            "cooldown_invalidated",
            "post_recovery_probe",
            "degraded_fallback_binding_cleared",
            "native_capability_requested",
            "fallback_attempted",
        },
        "outage_recovery",
    )
    outage_ended = _boolean(
        observation.get("outage_ended"), "outage_recovery.outage_ended"
    )
    cooldown_source_at = _utc_datetime(
        observation.get("cooldown_source_observed_at"),
        "outage_recovery.cooldown_source_observed_at",
    )
    cooldown_expires_at = _utc_datetime(
        observation.get("cooldown_expires_at"),
        "outage_recovery.cooldown_expires_at",
    )
    if cooldown_expires_at <= cooldown_source_at:
        raise ValueError("outage_recovery cooldown expiry must follow its source")
    invalidated = _boolean(
        observation.get("cooldown_invalidated"),
        "outage_recovery.cooldown_invalidated",
    )
    probe = _non_empty_string(
        observation.get("post_recovery_probe"),
        "outage_recovery.post_recovery_probe",
    )
    if probe not in {"not_attempted", "success", "still_outage", "transport_failed"}:
        raise ValueError("outage_recovery.post_recovery_probe is unsupported")
    degraded_cleared = _boolean(
        observation.get("degraded_fallback_binding_cleared"),
        "outage_recovery.degraded_fallback_binding_cleared",
    )
    native_requested = _boolean(
        observation.get("native_capability_requested"),
        "outage_recovery.native_capability_requested",
    )
    fallback_attempted = _boolean(
        observation.get("fallback_attempted"),
        "outage_recovery.fallback_attempted",
    )

    if outage_ended:
        outage_ended_at = _utc_datetime(
            observation.get("outage_ended_observed_at"),
            "outage_recovery.outage_ended_observed_at",
        )
        if outage_ended_at <= cooldown_source_at:
            raise ValueError("outage end must follow the cooldown source")
        checks = [
            {
                "id": "stale_cooldown_invalidated",
                "passed": invalidated,
                "failure_code": "stale_outage_cooldown_retained",
            },
            {
                "id": "recovery_probe_performed",
                "passed": probe in {"success", "still_outage"},
                "failure_code": "recovery_probe_missing",
            },
            {
                "id": "fallback_gated_by_probe",
                "passed": not fallback_attempted or probe == "still_outage",
                "failure_code": "fallback_selected_after_recovery_probe",
            },
        ]
        expected_action = "invalidate_cooldown_and_revalidate_affinity"
    else:
        checks = [
            {
                "id": "cooldown_retained_without_outage_end",
                "passed": not invalidated,
                "failure_code": "cooldown_invalidated_without_outage_end",
            }
        ]
        expected_action = "retain_cooldown_until_recovery_observed"
    checks.append(
        {
            "id": "degraded_binding_not_used_for_native",
            "passed": not native_requested
            or (not fallback_attempted and (not outage_ended or degraded_cleared)),
            "failure_code": "degraded_fallback_binding_used_for_native_request",
        }
    )
    failure_codes = [check["failure_code"] for check in checks if not check["passed"]]
    return {
        "schema_version": OUTAGE_RECOVERY_SCHEMA_VERSION,
        "qualified": not failure_codes,
        "failure_codes": failure_codes,
        "outage_ended": outage_ended,
        "expected_action": expected_action,
        "checks": checks,
        "required_contract": {
            "incident_cooldown": "outage_end_newer_than_source_invalidates_cooldown",
            "recovery_gate": "bounded_probe_before_fallback_admission",
            "degraded_affinity": (
                "fallback_binding_revalidated_before_native_capability_request"
            ),
            "native_capability": "fail_closed_when_no_eligible_native_provider",
        },
        "responsible_layer": "cpa_provider_health_state_and_selector",
        "effect_boundary": "content_free_observation_only",
    }


def qualify_quota_recovery(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Ensure an applied account reset invalidates older provider cooldown state."""

    from .schema_contract import validate_payload

    validate_payload("qualify_quota_recovery", observation)
    reject_private_material(observation)
    _reject_unexpected_keys(
        observation,
        {
            "reset_outcome",
            "reset_observed_at",
            "cooldown_source_observed_at",
            "cooldown_expires_at",
            "cooldown_invalidated",
            "post_reset_probe",
            "fallback_attempted",
        },
        "quota_recovery",
    )
    reset_outcome = _non_empty_string(
        observation.get("reset_outcome"), "quota_recovery.reset_outcome"
    )
    if reset_outcome not in {"applied", "not_applied"}:
        raise ValueError("quota_recovery.reset_outcome is unsupported")
    reset_at = _utc_datetime(
        observation.get("reset_observed_at"), "quota_recovery.reset_observed_at"
    )
    cooldown_source_at = _utc_datetime(
        observation.get("cooldown_source_observed_at"),
        "quota_recovery.cooldown_source_observed_at",
    )
    cooldown_expires_at = _utc_datetime(
        observation.get("cooldown_expires_at"),
        "quota_recovery.cooldown_expires_at",
    )
    if cooldown_expires_at <= cooldown_source_at:
        raise ValueError("quota_recovery cooldown expiry must follow its source")
    invalidated = _boolean(
        observation.get("cooldown_invalidated"),
        "quota_recovery.cooldown_invalidated",
    )
    probe = _non_empty_string(
        observation.get("post_reset_probe"), "quota_recovery.post_reset_probe"
    )
    if probe not in {"not_attempted", "success", "quota_limited", "transport_failed"}:
        raise ValueError("quota_recovery.post_reset_probe is unsupported")
    fallback_attempted = _boolean(
        observation.get("fallback_attempted"),
        "quota_recovery.fallback_attempted",
    )

    reset_supersedes_cooldown = (
        reset_outcome == "applied" and reset_at > cooldown_source_at
    )
    if reset_supersedes_cooldown:
        checks = [
            {
                "id": "cooldown_invalidated",
                "passed": invalidated,
                "failure_code": "stale_quota_cooldown_retained",
            },
            {
                "id": "account_reprobed",
                "passed": probe in {"success", "quota_limited"},
                "failure_code": "post_reset_probe_missing",
            },
            {
                "id": "fallback_gated_by_probe",
                "passed": not fallback_attempted or probe == "quota_limited",
                "failure_code": "fallback_selected_before_recovered_account_probe",
            },
        ]
        expected_action = "invalidate_and_probe"
    else:
        checks = [
            {
                "id": "cooldown_retained_without_newer_reset",
                "passed": not invalidated,
                "failure_code": "cooldown_invalidated_without_newer_reset",
            }
        ]
        expected_action = "retain_cooldown"
    failure_codes = [check["failure_code"] for check in checks if not check["passed"]]
    return {
        "schema_version": QUOTA_RECOVERY_SCHEMA_VERSION,
        "qualified": not failure_codes,
        "failure_codes": failure_codes,
        "reset_supersedes_cooldown": reset_supersedes_cooldown,
        "expected_action": expected_action,
        "checks": checks,
        "required_contract": {
            "reset_receipt_ordering": "newer_reset_invalidates_older_cooldown",
            "fallback_gate": "probe_recovered_account_before_fallback",
            "new_quota_limit": "a_new_probe_may_create_a_new_cooldown",
        },
        "responsible_layer": "cpa_provider_health_state",
        "effect_boundary": "content_free_observation_only",
    }


def qualify_tool_transport(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Qualify the response item shape used to dispatch one tool call."""

    from .schema_contract import validate_payload

    validate_payload("qualify_tool_transport", observation)
    reject_private_material(observation)
    _reject_unexpected_keys(
        observation,
        {"requested_transport", "observed_transport", "dispatch_outcome"},
        "tool_transport",
    )
    requested = _non_empty_string(
        observation.get("requested_transport"),
        "tool_transport.requested_transport",
    )
    observed = _non_empty_string(
        observation.get("observed_transport"),
        "tool_transport.observed_transport",
    )
    if requested not in ALLOWED_TOOL_TRANSPORTS:
        raise ValueError("tool_transport.requested_transport is unsupported")
    if observed not in ALLOWED_TOOL_TRANSPORTS:
        raise ValueError("tool_transport.observed_transport is unsupported")
    outcome = _non_empty_string(
        observation.get("dispatch_outcome"), "tool_transport.dispatch_outcome"
    )
    if outcome not in {"completed", "not_dispatched", "rejected_incompatible_payload"}:
        raise ValueError("tool_transport.dispatch_outcome is unsupported")
    checks = [
        {
            "id": "transport_preserved",
            "passed": requested == observed,
            "failure_code": "tool_transport_downgraded",
        },
        {
            "id": "dispatch_completed",
            "passed": outcome == "completed",
            "failure_code": "tool_dispatch_incomplete",
        },
    ]
    failure_codes = [check["failure_code"] for check in checks if not check["passed"]]
    return {
        "schema_version": TOOL_TRANSPORT_SCHEMA_VERSION,
        "qualified": not failure_codes,
        "failure_codes": failure_codes,
        "checks": checks,
        "required_contract": {
            "admission_filter": "required_tool_transport",
            "custom_tool_call": "preserve_raw_code_payload_and_item_type",
            "on_no_eligible_provider": "fail_closed_before_first_output",
        },
        "responsible_layer": (
            "provider_response_adapter"
            if requested != observed
            else "codex_cli_tool_dispatch"
        ),
        "effect_boundary": "content_free_observation_only",
    }


def _reject_unexpected_keys(
    value: Mapping[str, Any], allowed: set[str], field: str
) -> None:
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise ValueError(f"{field} has unsupported fields: {unexpected}")


def _git_ref(value: Any, field: str) -> str:
    ref = _non_empty_string(value, field)
    if (
        GIT_REF_RE.fullmatch(ref) is None
        or ".." in ref
        or "@{" in ref
        or ref.endswith(("/", ".", ".lock"))
    ):
        raise ValueError(f"{field} must be a bounded public Git ref")
    return ref


def _git_sha(value: Any, field: str) -> str:
    sha = _non_empty_string(value, field)
    if GIT_SHA_RE.fullmatch(sha) is None:
        raise ValueError(f"{field} must be a full lowercase Git SHA")
    return sha


def _compile_profiles(raw_profiles: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise ValueError("profiles must be a non-empty list")
    profiles: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_profiles):
        if not isinstance(raw, Mapping):
            raise TypeError(f"profiles[{index}] must be an object")
        profile_id = _non_empty_string(raw.get("id"), f"profiles[{index}].id")
        if SYMBOLIC_ID_RE.fullmatch(profile_id) is None:
            raise ValueError(f"profile id must be a public symbolic id: {profile_id}")
        if profile_id in profiles:
            raise ValueError(f"duplicate profile id: {profile_id}")
        provider = _non_empty_string(raw.get("provider"), f"profiles[{index}].provider")
        if provider not in ALLOWED_PROVIDERS:
            raise ValueError(f"unsupported provider for {profile_id}: {provider}")
        priority = _integer(raw.get("priority"), "profile.priority")
        modalities = _string_list(
            raw.get("input_modalities"), f"profiles[{index}].input_modalities"
        )
        if not modalities or not set(modalities) <= ALLOWED_MODALITIES:
            raise ValueError(f"profile {profile_id} has unsupported input modalities")
        tool_transports = _string_list(
            raw.get("tool_transports", ["function_call"]),
            f"profiles[{index}].tool_transports",
        )
        if not tool_transports or not set(tool_transports) <= ALLOWED_TOOL_TRANSPORTS:
            raise ValueError(f"profile {profile_id} has unsupported tool transports")
        profiles[profile_id] = {
            "id": profile_id,
            "provider": provider,
            "priority": priority,
            "input_modalities": modalities,
            "tool_transports": tool_transports,
            "supports_fast": _boolean(
                raw.get("supports_fast"),
                f"profiles[{index}].supports_fast",
                default=False,
            ),
        }
    return profiles


def _compile_rings(
    raw_rings: Any, profiles: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    if raw_rings is None:
        return {}
    if not isinstance(raw_rings, list):
        raise TypeError("rings must be a list")
    rings: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_rings):
        if not isinstance(raw, Mapping):
            raise TypeError(f"rings[{index}] must be an object")
        ring_id = _non_empty_string(raw.get("id"), f"rings[{index}].id")
        if SYMBOLIC_ID_RE.fullmatch(ring_id) is None:
            raise ValueError(f"ring id must be a public symbolic id: {ring_id}")
        if ring_id in rings:
            raise ValueError(f"duplicate ring id: {ring_id}")
        members = _string_list(raw.get("members"), f"rings[{index}].members")
        if len(members) < 2:
            raise ValueError(f"ring {ring_id} needs at least two members")
        for profile_id in members:
            if profile_id not in profiles:
                raise ValueError(
                    f"ring {ring_id} references unknown profile: {profile_id}"
                )
        max_cycles = raw.get("max_cycles")
        if max_cycles != 1 or isinstance(max_cycles, bool):
            raise ValueError(f"ring {ring_id} must use exactly one cycle")
        rings[ring_id] = {
            "id": ring_id,
            "members": members,
            "max_cycles": max_cycles,
        }
    return rings


def _rotate_members(members: Sequence[str], entrypoint: str) -> list[str]:
    index = members.index(entrypoint)
    return list(members[index:]) + list(members[:index])


def _eligible_profiles(
    candidates: Sequence[str],
    profiles: Mapping[str, Mapping[str, Any]],
    *,
    required_modalities: set[str],
    require_fast: bool = False,
    required_tool_transport: str | None = None,
) -> list[str]:
    eligible: list[str] = []
    for profile_id in candidates:
        profile = profiles[profile_id]
        if not required_modalities <= set(profile["input_modalities"]):
            continue
        if require_fast and not profile["supports_fast"]:
            continue
        if (
            required_tool_transport is not None
            and required_tool_transport not in profile["tool_transports"]
        ):
            continue
        eligible.append(profile_id)
    return eligible


def compile_catalog(source: Mapping[str, Any]) -> dict[str, Any]:
    from .schema_contract import validate_payload

    validate_payload("compile_catalog", source)
    reject_private_material(source)
    profiles = _compile_profiles(source.get("profiles"))
    rings = _compile_rings(source.get("rings"), profiles)
    raw_routes = source.get("routes")
    if not isinstance(raw_routes, list) or not raw_routes:
        raise ValueError("routes must be a non-empty list")

    routes: list[dict[str, Any]] = []
    route_ids: set[str] = set()
    for index, raw in enumerate(raw_routes):
        if not isinstance(raw, Mapping):
            raise TypeError(f"routes[{index}] must be an object")
        slug = _non_empty_string(raw.get("slug"), f"routes[{index}].slug")
        if MODEL_SLUG_RE.fullmatch(slug) is None:
            raise ValueError(f"route slug must be a public model id: {slug}")
        if slug in route_ids:
            raise ValueError(f"duplicate route slug: {slug}")
        route_ids.add(slug)
        mode = _non_empty_string(raw.get("mode"), f"routes[{index}].mode")
        if mode not in {"auto", "preferred", "manual", "alias"}:
            raise ValueError(f"route {slug} has unsupported mode: {mode}")
        visible = _boolean(raw.get("visible"), f"routes[{index}].visible", default=True)
        display_name = _non_empty_string(
            raw.get("display_name"), f"routes[{index}].display_name"
        )
        declared_modalities = _string_list(
            raw.get("input_modalities"), f"routes[{index}].input_modalities"
        )
        if (
            not declared_modalities
            or not set(declared_modalities) <= ALLOWED_MODALITIES
        ):
            raise ValueError(f"route {slug} has unsupported input modalities")
        reasoning = _string_list(
            raw.get("reasoning_levels", []), f"routes[{index}].reasoning_levels"
        )
        if not set(reasoning) <= ALLOWED_REASONING_LEVELS:
            raise ValueError(f"route {slug} has unsupported reasoning levels")

        ring_id = raw.get("ring")
        uses_ring = ring_id is not None
        fallback_tail = _string_list(
            raw.get("fallback_tail", []), f"routes[{index}].fallback_tail"
        )
        entrypoint = raw.get("entrypoint")
        if uses_ring:
            ring_id = _non_empty_string(ring_id, f"routes[{index}].ring")
            if ring_id not in rings:
                raise ValueError(f"route {slug} references unknown ring: {ring_id}")
            if mode not in {"auto", "preferred"}:
                raise ValueError(f"route {slug} cannot use a ring in {mode} mode")
            if "candidates" in raw:
                raise ValueError(
                    f"ring route must derive candidates instead of declaring them: {slug}"
                )
            members = rings[ring_id]["members"]
            if entrypoint is None and mode == "auto":
                entrypoint = "affinity_then_first"
            else:
                entrypoint = _non_empty_string(
                    entrypoint, f"routes[{index}].entrypoint"
                )
            if entrypoint == "affinity_then_first":
                if mode != "auto":
                    raise ValueError(
                        f"preferred route needs an explicit ring member: {slug}"
                    )
                ring_candidates = list(members)
            elif entrypoint in members:
                ring_candidates = _rotate_members(members, entrypoint)
            else:
                raise ValueError(
                    f"route {slug} entrypoint is not a member of ring {ring_id}"
                )
            if set(fallback_tail) & set(members):
                raise ValueError(f"route {slug} fallback tail overlaps its ring")
            candidates = ring_candidates + fallback_tail
        else:
            if fallback_tail or entrypoint is not None:
                raise ValueError(
                    f"non-ring route must not declare entrypoint or fallback tail: {slug}"
                )
            candidates = _string_list(
                raw.get("candidates", []), f"routes[{index}].candidates"
            )
        for profile_id in candidates:
            if profile_id not in profiles:
                raise ValueError(
                    f"route {slug} references unknown profile: {profile_id}"
                )
        if mode == "manual" and len(candidates) != 1:
            raise ValueError(f"manual route must pin exactly one profile: {slug}")
        if mode in {"auto", "preferred"} and len(candidates) < 2:
            raise ValueError(f"resilient route needs at least two candidates: {slug}")
        if mode == "preferred" and not uses_ring:
            raise ValueError(f"preferred route must reference a ring: {slug}")
        if mode == "alias" and candidates:
            raise ValueError(f"alias route must not declare candidates: {slug}")

        alias_for = raw.get("alias_for")
        if mode == "alias":
            alias_for = _non_empty_string(alias_for, f"routes[{index}].alias_for")
        elif alias_for is not None:
            raise ValueError(f"non-alias route must not set alias_for: {slug}")

        if mode != "alias":
            priorities = [profiles[profile_id]["priority"] for profile_id in candidates]
            if (
                mode == "auto"
                and not uses_ring
                and any(
                    current <= following for current, following in pairwise(priorities)
                )
            ):
                raise ValueError(f"auto route priorities must strictly descend: {slug}")
            for modality in declared_modalities:
                if not _eligible_profiles(
                    candidates, profiles, required_modalities={modality}
                ):
                    raise ValueError(
                        f"route {slug} has no eligible candidate for modality {modality}"
                    )

        supports_fast = _boolean(
            raw.get("supports_fast"),
            f"routes[{index}].supports_fast",
            default=False,
        )
        if (
            supports_fast
            and mode != "alias"
            and not _eligible_profiles(
                candidates,
                profiles,
                required_modalities=set(declared_modalities),
                require_fast=True,
            )
        ):
            raise ValueError(f"route {slug} has no fast-eligible candidate")
        fast_selector = raw.get("fast_selector")
        compiled_fast_selector = None
        if fast_selector is not None:
            if not isinstance(fast_selector, Mapping):
                raise TypeError(f"routes[{index}].fast_selector must be an object")
            _reject_unexpected_keys(
                fast_selector,
                {"display_name", "fallback_policy"},
                f"routes[{index}].fast_selector",
            )
            if mode == "alias" or not visible:
                raise ValueError(
                    f"Fast selector requires a visible concrete route: {slug}"
                )
            if not supports_fast:
                raise ValueError(f"Fast selector requires Fast support: {slug}")
            fallback_policy = _non_empty_string(
                fast_selector.get("fallback_policy"),
                f"routes[{index}].fast_selector.fallback_policy",
            )
            if fallback_policy != "fast_capable_only":
                raise ValueError(
                    f"route {slug} Fast selector must fail closed to Fast-capable providers"
                )
            compiled_fast_selector = {
                "display_name": _non_empty_string(
                    fast_selector.get("display_name"),
                    f"routes[{index}].fast_selector.display_name",
                ),
                "fallback_policy": fallback_policy,
            }
        max_cycles = rings[ring_id]["max_cycles"] if isinstance(ring_id, str) else 1

        routes.append(
            {
                "slug": slug,
                "display_name": display_name,
                "visibility": "visible" if visible else "hidden",
                "routing_mode": mode,
                "alias_for": alias_for,
                "ring_id": ring_id,
                "entrypoint": entrypoint,
                "fallback_tail": fallback_tail,
                "input_modalities": declared_modalities,
                "reasoning_levels": reasoning,
                "candidates": candidates,
                "eligible_candidates": {
                    modality: _eligible_profiles(
                        candidates, profiles, required_modalities={modality}
                    )
                    for modality in declared_modalities
                }
                if mode != "alias"
                else {},
                "supports_fast": supports_fast,
                "fast_selector": compiled_fast_selector,
                "fast_candidates": _eligible_profiles(
                    candidates,
                    profiles,
                    required_modalities=set(declared_modalities),
                    require_fast=True,
                )
                if supports_fast and mode != "alias"
                else [],
                "routing_policy": {
                    "candidate_filter": (
                        "required_modalities_service_tier_and_tool_transport"
                    ),
                    "on_no_eligible_provider": "fail_closed_before_first_output",
                    "session_affinity": "hint_revalidated_per_attempt"
                    if mode in {"auto", "preferred"}
                    else "disabled",
                    "traversal": "one_ring_pass_then_tail"
                    if uses_ring
                    else "ordered_candidates_once",
                    "max_cycles": max_cycles,
                    "commit_barrier": "before_first_visible_output_or_tool_call",
                    "foreign_history": "normalize_or_quarantine",
                },
            }
        )

    aliases = [route for route in routes if route["routing_mode"] == "alias"]
    non_alias_routes = {
        route["slug"]: route for route in routes if route["routing_mode"] != "alias"
    }
    for alias in aliases:
        if alias["alias_for"] not in non_alias_routes:
            raise ValueError(
                f"alias {alias['slug']} references unknown route: {alias['alias_for']}"
            )
        target = non_alias_routes[alias["alias_for"]]
        if alias["input_modalities"] != target["input_modalities"]:
            raise ValueError(
                f"alias {alias['slug']} input modalities differ from target"
            )
        if alias["supports_fast"] != target["supports_fast"]:
            raise ValueError(f"alias {alias['slug']} Fast support differs from target")

    selector_rows: list[dict[str, Any]] = []
    for route in routes:
        target = (
            non_alias_routes[route["alias_for"]]
            if route["routing_mode"] == "alias"
            else route
        )
        selector_rows.append(
            {
                "slug": route["slug"],
                "route_slug": target["slug"],
                "display_name": route["display_name"],
                "visibility": route["visibility"],
                "input_modalities": route["input_modalities"],
                "reasoning_levels": route["reasoning_levels"],
                "candidates": target["candidates"],
                "default_service_tier": "default",
                "request_service_tier_action": "preserve",
                "fallback_policy": "route_default",
            }
        )
        fast_selector = route["fast_selector"]
        if fast_selector is None:
            continue
        selector_rows.append(
            {
                "slug": f"fast/{route['slug']}",
                "route_slug": route["slug"],
                "display_name": fast_selector["display_name"],
                "visibility": route["visibility"],
                "input_modalities": route["input_modalities"],
                "reasoning_levels": route["reasoning_levels"],
                "candidates": route["fast_candidates"],
                "default_service_tier": "fast",
                "request_service_tier_action": "force_priority",
                "fallback_policy": fast_selector["fallback_policy"],
            }
        )
    selector_slugs = [row["slug"] for row in selector_rows]
    if len(selector_slugs) != len(set(selector_slugs)):
        raise ValueError("generated Fast selector collides with a declared route slug")

    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "credential_free": True,
        "default_service_tier": "default",
        "fast_selector_prefix": "fast/",
        "fast_request_service_tier": "priority",
        "profiles": list(profiles.values()),
        "rings": list(rings.values()),
        "routes": routes,
        "selector_rows": selector_rows,
    }


def normalize_selector_request(normalization: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve one public selector before provider alias mapping or body handling."""

    from .schema_contract import validate_payload

    validate_payload("normalize_selector_request", normalization)
    reject_private_material(normalization)
    _reject_unexpected_keys(
        normalization,
        {
            "catalog_source",
            "model_selector",
            "service_tier",
            "required_tool_transport",
            "required_modalities",
        },
        "normalization",
    )
    source = normalization.get("catalog_source")
    if not isinstance(source, Mapping):
        raise TypeError("normalization.catalog_source must be an object")
    catalog = compile_catalog(source)
    selector_slug = _non_empty_string(
        normalization.get("model_selector"), "normalization.model_selector"
    )
    selectors = {row["slug"]: row for row in catalog["selector_rows"]}
    selector = selectors.get(selector_slug)
    if selector is None:
        raise ValueError(f"unknown model selector: {selector_slug}")

    requested_tier = normalization.get("service_tier")
    if requested_tier is not None:
        requested_tier = _non_empty_string(requested_tier, "normalization.service_tier")
        if requested_tier not in {"default", "priority"}:
            raise ValueError("normalization.service_tier must be default or priority")

    tier_action: dict[str, str] = {"action": selector["request_service_tier_action"]}
    if selector["request_service_tier_action"] == "force_priority":
        tier_action["value"] = catalog["fast_request_service_tier"]
    elif requested_tier is not None:
        tier_action["value"] = requested_tier

    effective_fast = (
        selector["default_service_tier"] == "fast"
        or requested_tier == catalog["fast_request_service_tier"]
    )
    required_modalities = normalization.get("required_modalities", [])
    profiles = {item["id"]: item for item in catalog["profiles"]}
    if not set(required_modalities) <= set(selector["input_modalities"]):
        raise ValueError("selector does not admit required modalities")
    eligible_candidates = _eligible_profiles(
        selector["candidates"], profiles, required_modalities=set(required_modalities)
    )
    if not eligible_candidates:
        raise ValueError("selector has no modality-eligible candidates")
    fallback_policy = selector["fallback_policy"]
    required_tool_transport = normalization.get("required_tool_transport")
    if required_tool_transport is not None:
        required_tool_transport = _non_empty_string(
            required_tool_transport, "normalization.required_tool_transport"
        )
        if required_tool_transport not in ALLOWED_TOOL_TRANSPORTS:
            raise ValueError("normalization.required_tool_transport is unsupported")
        profiles = {item["id"]: item for item in catalog["profiles"]}
        eligible_candidates = _eligible_profiles(
            eligible_candidates,
            profiles,
            required_modalities=set(required_modalities),
            required_tool_transport=required_tool_transport,
        )
        fallback_policy = "required_tool_transport_only"
        if not eligible_candidates:
            raise ValueError(
                f"selector {selector_slug} has no provider for "
                f"{required_tool_transport}"
            )
    if effective_fast:
        profiles = {item["id"]: item for item in catalog["profiles"]}
        eligible_candidates = _eligible_profiles(
            selector["candidates"],
            profiles,
            required_modalities=set(required_modalities),
            require_fast=True,
            required_tool_transport=required_tool_transport,
        )
        if not eligible_candidates:
            raise ValueError(f"selector {selector_slug} has no Fast-capable candidates")
        fallback_policy = (
            "fast_and_required_tool_transport_only"
            if required_tool_transport is not None
            else "fast_capable_only"
        )

    return {
        "original_model_selector": selector_slug,
        "normalized_model_selector": selector["route_slug"],
        "default_service_tier": selector["default_service_tier"],
        "service_tier": tier_action,
        "fallback_policy": fallback_policy,
        "eligible_candidates": eligible_candidates,
        "required_tool_transport": required_tool_transport,
        "required_modalities": required_modalities,
    }


def _timestamp(value: Any, field: str) -> str:
    timestamp = _non_empty_string(value, field)
    if (
        re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", timestamp)
        is None
    ):
        raise ValueError(f"{field} must be an RFC 3339 UTC timestamp")
    return timestamp


def _percentage(value: Any, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{field} must be a number")
    if not 0 <= value <= 100:
        raise ValueError(f"{field} must be between 0 and 100")
    return float(value)


def _quota_windows(raw: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise TypeError(f"{field} must be a list")
    windows: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise TypeError(f"{field}[{index}] must be an object")
        _reject_unexpected_keys(
            item,
            {"id", "used_percent", "window_minutes", "reset_at"},
            f"{field}[{index}]",
        )
        window_id = _non_empty_string(item.get("id"), f"{field}[{index}].id")
        if SYMBOLIC_ID_RE.fullmatch(window_id) is None:
            raise ValueError(f"{field}[{index}].id must be a symbolic id")
        if window_id in ids:
            raise ValueError(f"{field} has duplicate window id: {window_id}")
        ids.add(window_id)
        used = _percentage(item.get("used_percent"), f"{field}[{index}].used_percent")
        minutes = _integer(
            item.get("window_minutes"), f"{field}[{index}].window_minutes"
        )
        if minutes <= 0:
            raise ValueError(f"{field}[{index}].window_minutes must be positive")
        reset_at = item.get("reset_at")
        if reset_at is not None:
            reset_at = _timestamp(reset_at, f"{field}[{index}].reset_at")
        windows.append(
            {
                "id": window_id,
                "used_percent": used,
                "remaining_percent": 100.0 - used,
                "window_minutes": minutes,
                "reset_at": reset_at,
            }
        )
    return windows


def _eligible_route_order(
    route: Mapping[str, Any],
    profiles: Mapping[str, Mapping[str, Any]],
    *,
    modality: str,
    fast: bool,
    required_tool_transport: str | None,
) -> list[str]:
    return _eligible_profiles(
        route["candidates"],
        profiles,
        required_modalities={modality},
        require_fast=fast,
        required_tool_transport=required_tool_transport,
    )


def _legal_attempt_orders(
    route: Mapping[str, Any],
    rings: Mapping[str, Mapping[str, Any]],
    profiles: Mapping[str, Mapping[str, Any]],
    *,
    modality: str,
    fast: bool,
    required_tool_transport: str | None,
) -> list[list[str]]:
    eligible = _eligible_route_order(
        route,
        profiles,
        modality=modality,
        fast=fast,
        required_tool_transport=required_tool_transport,
    )
    if route["entrypoint"] != "affinity_then_first":
        return [eligible]
    ring = rings[route["ring_id"]]
    eligible_ring = [item for item in ring["members"] if item in eligible]
    eligible_tail = [item for item in route["fallback_tail"] if item in eligible]
    if not eligible_ring:
        return [eligible_tail]
    return [
        _rotate_members(eligible_ring, entrypoint) + eligible_tail
        for entrypoint in eligible_ring
    ]


def project_runtime_status(status: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and project content-free host, route and account observations."""

    from .schema_contract import validate_payload

    validate_payload("project_runtime_status", status)
    reject_private_material(status)
    _reject_unexpected_keys(
        status,
        {
            "schema_version",
            "credential_free",
            "catalog_source",
            "host_identity",
            "execution_observation",
            "account_observations",
        },
        "status",
    )
    source = status.get("catalog_source")
    if not isinstance(source, Mapping):
        raise TypeError("status.catalog_source must be an object")
    catalog = compile_catalog(source)
    profiles = {item["id"]: item for item in catalog["profiles"]}
    rings = {item["id"]: item for item in catalog["rings"]}
    routes = {
        item["slug"]: item
        for item in catalog["routes"]
        if item["routing_mode"] != "alias"
    }
    selectors = {item["slug"]: item for item in catalog["selector_rows"]}

    host = status.get("host_identity")
    if not isinstance(host, Mapping):
        raise TypeError("status.host_identity must be an object")
    expected_host = {
        "state": host.get("state"),
        "projection": "not_projected",
        "route_binding": "none",
    }
    if dict(host) != expected_host or host.get("state") not in {
        "retained",
        "not_applicable",
        "unknown",
    }:
        raise ValueError(
            "host identity must be typed, not projected and independent of routing"
        )

    observation = status.get("execution_observation")
    if not isinstance(observation, Mapping):
        raise TypeError("status.execution_observation must be an object")
    _reject_unexpected_keys(
        observation,
        {
            "route_slug",
            "modality",
            "fast",
            "observed_at",
            "attempted_profiles",
            "selected_profile",
            "outcome",
            "required_tool_transport",
        },
        "status.execution_observation",
    )
    selector_slug = _non_empty_string(
        observation.get("route_slug"), "status.execution_observation.route_slug"
    )
    selector = selectors.get(selector_slug)
    if selector is None:
        raise ValueError("execution observation must reference a catalog selector")
    route = routes[selector["route_slug"]]
    modality = _non_empty_string(
        observation.get("modality"), "status.execution_observation.modality"
    )
    if modality not in route["input_modalities"]:
        raise ValueError(f"selector {selector_slug} does not admit modality {modality}")
    selector_defaults_fast = selector["default_service_tier"] == "fast"
    fast = selector_defaults_fast
    if "fast" in observation:
        declared_fast = _boolean(
            observation.get("fast"), "status.execution_observation.fast"
        )
        if selector_defaults_fast and not declared_fast:
            raise ValueError("a Fast selector cannot report a non-Fast execution")
        fast = declared_fast
    required_tool_transport = observation.get("required_tool_transport")
    if required_tool_transport is not None:
        required_tool_transport = _non_empty_string(
            required_tool_transport,
            "status.execution_observation.required_tool_transport",
        )
        if required_tool_transport not in ALLOWED_TOOL_TRANSPORTS:
            raise ValueError(
                "status.execution_observation.required_tool_transport is unsupported"
            )
    observed_at = _timestamp(
        observation.get("observed_at"), "status.execution_observation.observed_at"
    )
    attempted = _string_list(
        observation.get("attempted_profiles"),
        "status.execution_observation.attempted_profiles",
    )
    if not attempted:
        raise ValueError("execution observation needs at least one attempted profile")
    legal_orders = _legal_attempt_orders(
        route,
        rings,
        profiles,
        modality=modality,
        fast=fast,
        required_tool_transport=required_tool_transport,
    )
    matching_orders = [
        order for order in legal_orders if attempted == order[: len(attempted)]
    ]
    if not matching_orders:
        raise ValueError(
            f"attempted profiles are not a legal prefix for selector {selector_slug}"
        )
    outcome = _non_empty_string(
        observation.get("outcome"), "status.execution_observation.outcome"
    )
    if outcome not in {"success", "failed"}:
        raise ValueError("execution outcome must be success or failed")
    selected = observation.get("selected_profile")
    if outcome == "success":
        selected = _non_empty_string(
            selected, "status.execution_observation.selected_profile"
        )
        if selected != attempted[-1]:
            raise ValueError(
                "successful selection must equal the final attempted profile"
            )
    elif "selected_profile" in observation:
        raise ValueError("failed execution must not declare a selected profile")

    raw_accounts = status.get("account_observations")
    if not isinstance(raw_accounts, list):
        raise TypeError("status.account_observations must be a list")
    accounts: list[dict[str, Any]] = []
    account_ids: set[str] = set()
    for index, raw in enumerate(raw_accounts):
        field = f"status.account_observations[{index}]"
        if not isinstance(raw, Mapping):
            raise TypeError(f"{field} must be an object")
        _reject_unexpected_keys(
            raw,
            {"profile_id", "state", "quota", "recent_activity"},
            field,
        )
        profile_id = _non_empty_string(raw.get("profile_id"), f"{field}.profile_id")
        if profile_id in account_ids:
            raise ValueError(f"duplicate account observation: {profile_id}")
        if profile_id not in profiles or profiles[profile_id]["provider"] != "codex":
            raise ValueError(
                f"account observation must reference a Codex profile: {profile_id}"
            )
        account_ids.add(profile_id)
        state = _non_empty_string(raw.get("state"), f"{field}.state")
        if state not in {"ready", "degraded", "unavailable", "unknown"}:
            raise ValueError(f"unsupported account state: {state}")
        quota = raw.get("quota")
        projected_quota = None
        if quota is not None:
            if not isinstance(quota, Mapping):
                raise TypeError(f"{field}.quota must be an object")
            _reject_unexpected_keys(quota, {"observed_at", "windows"}, f"{field}.quota")
            projected_quota = {
                "observed_at": _timestamp(
                    quota.get("observed_at"), f"{field}.quota.observed_at"
                ),
                "windows": _quota_windows(
                    quota.get("windows", []), f"{field}.quota.windows"
                ),
            }
        activity = raw.get("recent_activity")
        if not isinstance(activity, Mapping):
            raise TypeError(f"{field}.recent_activity must be an object")
        _reject_unexpected_keys(
            activity,
            {"success", "failed", "window_minutes"},
            f"{field}.recent_activity",
        )
        projected_activity: dict[str, int] = {}
        for key in ("success", "failed", "window_minutes"):
            value = _integer(activity.get(key), f"{field}.recent_activity.{key}")
            minimum = 1 if key == "window_minutes" else 0
            if value < minimum:
                raise ValueError(f"{field}.recent_activity.{key} is invalid")
            projected_activity[key] = value
        accounts.append(
            {
                "profile_id": profile_id,
                "state": state,
                "quota": projected_quota,
                "recent_activity": projected_activity,
            }
        )

    execution = {
        "observed_at": observed_at,
        "attempted_profiles": attempted,
        "outcome": outcome,
        "fallback_used": len(attempted) > 1,
    }
    if selected is not None:
        execution["selected_profile"] = selected

    return {
        "schema_version": RUNTIME_STATUS_SCHEMA_VERSION,
        "credential_free": True,
        "host_identity": expected_host,
        "route_intent": {
            "selector_slug": selector_slug,
            "route_slug": route["slug"],
            "routing_mode": route["routing_mode"],
            "modality": modality,
            "fast": fast,
            "required_tool_transport": required_tool_transport,
            "legal_attempt_orders": legal_orders,
        },
        "execution": execution,
        "accounts": accounts,
    }


def qualify_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Qualify CLI configuration readback, never online routing or entitlement."""
    from .schema_contract import validate_payload

    validate_payload("qualify_snapshot", snapshot)
    checks = [
        {"id": "cli_version", "passed": snapshot["codex_cli_version"] == "0.160.0"},
        {"id": "profile_config", "passed": snapshot["profile_config_parsed"]},
        {"id": "catalog_readback", "passed": snapshot["catalog_readback_matches"]},
        {"id": "provider_readback", "passed": snapshot["provider_readback_matches"]},
    ]
    return {
        "schema_version": "codex_cli_configuration_qualification_v1",
        "qualified": all(row["passed"] for row in checks),
        "checks": checks,
        "scope": "offline_configuration_only",
        "online_qualification": "held",
        "effect_boundary": "content_free_observation_only",
    }


def build_upgrade_plan(upgrade: Mapping[str, Any]) -> dict[str, Any]:
    from .schema_contract import validate_payload

    validate_payload("upgrade_plan", upgrade)
    reject_private_material(upgrade)
    current = _git_ref(upgrade.get("current_ref"), "upgrade.current_ref")
    target = _git_ref(upgrade.get("target_ref"), "upgrade.target_ref")
    changed_seams = _string_list(upgrade.get("changed_seams"), "upgrade.changed_seams")
    unknown = sorted(set(changed_seams) - ALLOWED_CHANGE_SEAMS)
    if unknown:
        raise ValueError(f"unsupported changed seams: {unknown}")
    matrix = ["public_boundary", "doctor", "catalog_readback", "rollback_receipt"]
    seam_checks = {
        "history_projection": [
            "additional_tools",
            "foreign_reasoning",
            "tool_causality",
        ],
        "cli_configuration": ["cli_version", "profile_config", "provider_readback"],
        "integration_candidate": [
            "exact_base_head",
            "exact_ordered_source_heads",
            "required_seam_coverage",
            "integration_tree_receipt",
        ],
        "sse_lifecycle": ["unique_terminal", "active_item_pairing"],
        "retry_policy": ["bounded_attempts", "ttfb_p50_p95", "commit_barrier"],
        "quota_recovery": [
            "reset_receipt_ordering",
            "stale_cooldown_invalidation",
            "post_reset_account_probe",
            "fallback_probe_gate",
        ],
        "transport_pool": [
            "h2_reuse",
            "tls_resumption",
            "draining_rebuild",
            "no_replay",
        ],
        "tool_transport": [
            "custom_tool_candidate_admission",
            "custom_tool_item_preserved",
            "tool_dispatch_completed",
            "incompatible_fallback_fail_closed",
        ],
        "model_catalog": [
            "visible_routes",
            "hidden_alias",
            "fast_selector_rows",
            "fast_default_off",
        ],
        "request_normalizer": [
            "selector_prefix_capture",
            "priority_injection",
            "ordinary_selector_preserved",
            "effective_priority_admission",
            "fast_route_no_unsupported_fallback",
        ],
        "route_fallback": [
            "preferred_entrypoint_order",
            "single_ring_cycle",
            "terminal_tail_once",
        ],
        "modality_routing": ["declared_modality_admission", "no_eligible_fail_closed"],
    }
    for seam in changed_seams:
        matrix.extend(seam_checks[seam])
    return {
        "current_ref": current,
        "target_ref": target,
        "changed_seams": changed_seams,
        "steps": [
            "capture_private_operator_snapshot",
            "build_target_in_isolation",
            "run_public_safe_doctor",
            "run_changed_seam_matrix",
            "switch_operator_owned_pointer",
            "read_back_catalog_and_runtime",
            "retain_previous_pointer_for_rollback",
        ],
        "required_checks": list(dict.fromkeys(matrix)),
        "rollback_trigger": "any_failed_check_or_unexplained_regression",
    }


def reconcile_integration_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Compare a public-safe multi-source candidate with its last sync receipt.

    This operation owns provider-specific source coverage and upgrade policy.
    It returns inputs for LoopX's core integration-branch capability instead of
    implementing Git fetch, merge, push, or deployment effects.
    """

    from .schema_contract import validate_payload

    validate_payload("reconcile_integration_candidate", candidate)
    reject_private_material(candidate)
    _reject_unexpected_keys(
        candidate,
        {
            "base_ref",
            "integration_branch",
            "required_seams",
            "sources",
            "observed",
            "last_sync",
        },
        "integration",
    )
    base_ref = _git_ref(candidate.get("base_ref"), "integration.base_ref")
    integration_branch = _git_ref(
        candidate.get("integration_branch"), "integration.integration_branch"
    )
    required_seams = _string_list(
        candidate.get("required_seams"), "integration.required_seams"
    )
    if not required_seams:
        raise ValueError("integration.required_seams must not be empty")
    unknown_required = sorted(set(required_seams) - ALLOWED_CHANGE_SEAMS)
    if unknown_required:
        raise ValueError(f"unsupported required seams: {unknown_required}")

    raw_sources = candidate.get("sources")
    if not isinstance(raw_sources, list) or len(raw_sources) < 2:
        raise ValueError("integration.sources needs at least two ordered sources")
    sources: list[dict[str, Any]] = []
    source_ids: set[str] = set()
    source_refs: set[str] = set()
    covered_seams: set[str] = set()
    for index, raw in enumerate(raw_sources):
        field = f"integration.sources[{index}]"
        if not isinstance(raw, Mapping):
            raise TypeError(f"{field} must be an object")
        _reject_unexpected_keys(
            raw,
            {"id", "kind", "ref", "head_sha", "changed_seams"},
            field,
        )
        source_id = _non_empty_string(raw.get("id"), f"{field}.id")
        if SYMBOLIC_ID_RE.fullmatch(source_id) is None:
            raise ValueError(f"{field}.id must be a public symbolic id")
        if source_id in source_ids:
            raise ValueError(f"duplicate integration source id: {source_id}")
        source_ids.add(source_id)
        kind = _non_empty_string(raw.get("kind"), f"{field}.kind")
        if kind not in {"pull_request", "public_safe_patch"}:
            raise ValueError(f"{field}.kind must be pull_request or public_safe_patch")
        source_ref = _git_ref(raw.get("ref"), f"{field}.ref")
        if source_ref in source_refs:
            raise ValueError(f"duplicate integration source ref: {source_ref}")
        source_refs.add(source_ref)
        changed_seams = _string_list(raw.get("changed_seams"), f"{field}.changed_seams")
        if not changed_seams:
            raise ValueError(f"{field}.changed_seams must not be empty")
        unknown = sorted(set(changed_seams) - ALLOWED_CHANGE_SEAMS)
        if unknown:
            raise ValueError(f"{field} has unsupported changed seams: {unknown}")
        covered_seams.update(changed_seams)
        sources.append(
            {
                "id": source_id,
                "kind": kind,
                "ref": source_ref,
                "head_sha": _git_sha(raw.get("head_sha"), f"{field}.head_sha"),
                "changed_seams": changed_seams,
            }
        )

    missing_seams = sorted(set(required_seams) - covered_seams)
    if missing_seams:
        raise ValueError(
            f"integration candidate does not cover required seams: {missing_seams}"
        )

    def receipt(raw: Any, field: str) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise TypeError(f"{field} must be an object")
        _reject_unexpected_keys(
            raw, {"base_sha", "integration_sha", "source_heads"}, field
        )
        raw_heads = raw.get("source_heads")
        if not isinstance(raw_heads, Mapping):
            raise TypeError(f"{field}.source_heads must be an object")
        if set(raw_heads) != source_ids:
            raise ValueError(f"{field}.source_heads must match integration source ids")
        return {
            "base_sha": _git_sha(raw.get("base_sha"), f"{field}.base_sha"),
            "integration_sha": _git_sha(
                raw.get("integration_sha"), f"{field}.integration_sha"
            ),
            "source_heads": {
                source_id: _git_sha(
                    raw_heads[source_id], f"{field}.source_heads.{source_id}"
                )
                for source_id in sorted(source_ids)
            },
        }

    observed = receipt(candidate.get("observed"), "integration.observed")
    last_sync = receipt(candidate.get("last_sync"), "integration.last_sync")
    for source in sources:
        if observed["source_heads"][source["id"]] != source["head_sha"]:
            raise ValueError(
                f"observed source head does not match declared head for {source['id']}"
            )

    drift_reasons: list[dict[str, str]] = []
    if observed["base_sha"] != last_sync["base_sha"]:
        drift_reasons.append(
            {
                "kind": "base_moved",
                "ref": base_ref,
                "last_sync_sha": last_sync["base_sha"],
                "observed_sha": observed["base_sha"],
            }
        )
    for source in sources:
        source_id = source["id"]
        if observed["source_heads"][source_id] != last_sync["source_heads"][source_id]:
            drift_reasons.append(
                {
                    "kind": "source_moved",
                    "source_id": source_id,
                    "last_sync_sha": last_sync["source_heads"][source_id],
                    "observed_sha": observed["source_heads"][source_id],
                }
            )
    if observed["integration_sha"] != last_sync["integration_sha"]:
        drift_reasons.append(
            {
                "kind": "integration_head_moved",
                "ref": integration_branch,
                "last_sync_sha": last_sync["integration_sha"],
                "observed_sha": observed["integration_sha"],
            }
        )

    sync_required = bool(drift_reasons)
    return {
        "schema_version": INTEGRATION_CANDIDATE_SCHEMA_VERSION,
        "status": "sync_required" if sync_required else "in_sync",
        "sync_required": sync_required,
        "drift_reasons": drift_reasons,
        "required_seams": required_seams,
        "covered_seams": sorted(covered_seams),
        "source_order": sources,
        "core_integration_plan": {
            "base_ref": base_ref,
            "integration_branch": integration_branch,
            "source_refs": [source["ref"] for source in sources],
        },
        "reconcile_steps": [
            "refresh_declared_remote_refs_read_only",
            "verify_every_ref_resolves_to_declared_head",
            "preview_core_integration_branch_sync",
            "execute_local_sync_with_explicit_write_authority",
            "run_changed_seam_and_build_validation",
            "push_candidate_only_after_validation",
        ],
        "deployment_contract": {
            "artifact": "content_addressed_binary_with_sha256",
            "sequence": [
                "retain_previous_binary_and_config_pointer",
                "run_isolated_smoke",
                "compare_configuration_by_field",
                "switch_operator_owned_pointer",
                "read_back_catalog_retry_and_runtime",
            ],
            "rollback_trigger": "any_failed_readback_or_unexplained_regression",
            "session_store_policy": "preserve_in_place_never_copy_or_delete",
        },
        "effect_boundary": "read_only_public_safe_plan",
    }


def compile_cli_plan(cli_plan: Mapping[str, Any]) -> dict[str, Any]:
    """Pin a symbolic CLI launch declaration; deployment and credentials stay trusted."""
    from .schema_contract import validate_payload

    validate_payload("compile_cli_plan", cli_plan)
    source = cli_plan["catalog_source"]
    catalog = compile_catalog(source)
    normalized = normalize_selector_request(
        {
            "catalog_source": source,
            "model_selector": cli_plan["model_selector"],
            "service_tier": cli_plan["service_tier"],
            "required_modalities": cli_plan["required_capabilities"]["modalities"],
            "required_tool_transport": cli_plan["required_capabilities"][
                "tool_transport"
            ],
        }
    )
    selector = next(
        row
        for row in catalog["selector_rows"]
        if row["slug"] == cli_plan["model_selector"]
    )
    if cli_plan["reasoning_effort"] not in selector["reasoning_levels"]:
        raise ValueError("reasoning effort is not declared by selector")
    profiles = {row["id"]: row for row in catalog["profiles"]}
    # Online heterogeneous history qualification is held. Do not advertise it
    # as usable merely because metadata admits one isolated request shape.
    if any(
        profiles[slot]["provider"] != "codex"
        for slot in normalized["eligible_candidates"]
    ):
        raise ValueError(
            "heterogeneous CLI plans require independent online qualification"
        )
    result = {key: value for key, value in cli_plan.items() if key != "catalog_source"}
    result.update(
        {
            "schema_version": "codex_cli_route_launch_plan_v1",
            "service_tier": normalized["service_tier"].get("value", "default"),
            "eligible_candidates": normalized["eligible_candidates"],
            "qualification": "declaration_only",
            "online_qualification": "held",
            "provider_overrides": {},
        }
    )
    return result
