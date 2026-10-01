from __future__ import annotations

import json
from typing import Any


PACKET_SUMMARY_SCHEMA_VERSION = "loopx_start_goal_packet_summary_v0"
PACKET_MEASUREMENT_SCHEMA_VERSION = "loopx_packet_duplication_measurement_v0"


def _iter_string_leaves(
    value: Any, path: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], str]]:
    leaves: list[tuple[tuple[str, ...], str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "packet_summary":
                continue
            leaves.extend(_iter_string_leaves(child, (*path, str(key))))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            leaves.extend(_iter_string_leaves(child, (*path, str(index))))
    elif isinstance(value, str):
        leaves.append((path, value))
    return leaves


def _without_packet_summary(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_packet_summary(child)
            for key, child in value.items()
            if key != "packet_summary"
        }
    if isinstance(value, list):
        return [_without_packet_summary(child) for child in value]
    return value


def _is_command_value(path: tuple[str, ...]) -> bool:
    if not path:
        return False
    leaf = path[-1]
    return (
        (len(path) >= 2 and path[-2] == "commands")
        or leaf in {"command", "command_template", "prompt", "canonical_cli_command"}
        or leaf.endswith(("_command", "_prompt", "_command_if_needed"))
    )


def _measure_packet_duplication(
    payload: dict[str, Any], *, objective: str | None
) -> dict[str, Any]:
    compatibility_payload = _without_packet_summary(payload)
    leaves = _iter_string_leaves(compatibility_payload)
    objective_occurrences = 0
    objective_field_count = 0
    if objective:
        for _, value in leaves:
            count = value.count(objective)
            if count:
                objective_occurrences += count
                objective_field_count += 1

    command_values: list[str] = []
    for path, value in leaves:
        if not value or not _is_command_value(path):
            continue
        command_values.append(value)
    unique_command_count = len(set(command_values))

    compatibility_json = json.dumps(
        compatibility_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return {
        "schema_version": PACKET_MEASUREMENT_SCHEMA_VERSION,
        "measurement_scope": "compatibility_projection_without_packet_summary",
        "serialized_bytes": len(compatibility_json.encode("utf-8")),
        "objective_content": {
            "substring_occurrences": objective_occurrences,
            "containing_field_count": objective_field_count,
            "duplicate_occurrences": max(0, objective_occurrences - 1),
        },
        "command_content": {
            "candidate_occurrences": len(command_values),
            "unique_values": unique_command_count,
            "duplicate_occurrences": len(command_values) - unique_command_count,
        },
    }


def build_packet_summary(
    payload: dict[str, Any],
    *,
    packet_kind: str,
    detail_refs: dict[str, str],
    legacy_fields_retained: bool = True,
    compact_projection_default: bool = False,
) -> dict[str, Any]:
    next_step = payload.get("recommended_next_step")
    next_step = next_step if isinstance(next_step, dict) else {}
    return {
        "schema_version": PACKET_SUMMARY_SCHEMA_VERSION,
        "packet_kind": packet_kind,
        "goal_id": payload.get("goal_id"),
        "objective": payload.get("goal_text"),
        "next_step_kind": next_step.get("kind"),
        "detail_refs": {
            name: {
                "schema_version": "loopx_packet_json_pointer_ref_v0",
                "json_pointer": pointer,
            }
            for name, pointer in detail_refs.items()
        },
        "compatibility": {
            "legacy_fields_retained": legacy_fields_retained,
            "compact_projection_default": compact_projection_default,
            "removal_gate": "explicit_host_shadow_parity",
        },
        "duplication_measurement": _measure_packet_duplication(
            payload,
            objective=str(payload.get("goal_text"))
            if payload.get("goal_text")
            else None,
        ),
    }
