"""Versioned, bounded model metadata supplied explicitly by the operator.

Synthetic metadata proves configuration shape only, never online entitlement.
Instructions, prompts and arbitrary cache fields are deliberately excluded.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

METADATA_VERSION = "codex_cli_model_metadata_v1"
CODEX_VERSION = "0.160.0"
MODEL_FIELDS = {
    "slug",
    "display_name",
    "description",
    "default_reasoning_level",
    "supported_reasoning_levels",
    "shell_type",
    "visibility",
    "priority",
    "upgrade",
    "context_window",
    "auto_compact_token_limit",
    "effective_context_window_percent",
    "supports_reasoning_summaries",
    "supports_parallel_tool_calls",
    "support_verbosity",
    "default_verbosity",
    "apply_patch_tool_type",
    "truncation_policy",
    "input_modalities",
    "additional_speed_tiers",
    "service_tiers",
    "default_service_tier",
    "minimal_client_version",
    "supported_in_api",
    "prefer_websockets",
    "supports_image_detail_original",
    "supports_search_tool",
}


def read_metadata(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or set(data) != {"schema_version", "codex_cli_version", "source_kind", "models"}
        or data["schema_version"] != METADATA_VERSION
        or data["codex_cli_version"] != CODEX_VERSION
        or data["source_kind"] not in {"synthetic", "operator_verified"}
        or not isinstance(data["models"], list)
        or not data["models"]
    ):
        raise ValueError("invalid versioned CLI model metadata")
    seen = set()
    for row in data["models"]:
        if not isinstance(row, dict) or set(row) - MODEL_FIELDS:
            raise ValueError("unsupported CLI metadata field")
        integer_fields = {
            "priority",
            "context_window",
            "auto_compact_token_limit",
            "effective_context_window_percent",
        }
        boolean_fields = {
            "supports_reasoning_summaries",
            "supports_parallel_tool_calls",
            "support_verbosity",
            "supported_in_api",
            "prefer_websockets",
            "supports_image_detail_original",
            "supports_search_tool",
        }
        enum_fields = {
            "default_reasoning_level": {
                "none",
                "minimal",
                "low",
                "medium",
                "high",
                "xhigh",
                "max",
                "ultra",
            },
            "shell_type": {"shell_command", "unified_exec"},
            "visibility": {"list", "hide"},
            "default_verbosity": {"low", "medium", "high"},
            "apply_patch_tool_type": {"freeform", "function"},
            "default_service_tier": {None, "default", "fast", "priority", "flex"},
        }
        for key in integer_fields:
            if key in row and (type(row[key]) is not int or row[key] < 0):
                raise ValueError(
                    "metadata integer field requires a nonnegative integer"
                )
        for key in boolean_fields:
            if key in row and type(row[key]) is not bool:
                raise ValueError("metadata boolean field requires a boolean")
        for key, allowed in enum_fields.items():
            if key in row and (
                not isinstance(row[key], (str, type(None))) or row[key] not in allowed
            ):
                raise ValueError("metadata enum field is unsupported")
        if row.get("upgrade") is not None:
            raise ValueError("CLI metadata upgrades are not supported")
        if (
            "minimal_client_version" in row
            and row["minimal_client_version"] != CODEX_VERSION
        ):
            raise ValueError("metadata requires the pinned CLI version")
        for key in ("additional_speed_tiers", "service_tiers"):
            if key in row and not isinstance(row[key], list):
                raise ValueError("metadata tier fields require arrays")
        if any(tier != "fast" for tier in row.get("additional_speed_tiers", [])):
            raise ValueError("unsupported speed tier metadata")
        slug = row.get("slug")
        if (
            not isinstance(slug, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,99}", slug)
            or slug in seen
        ):
            raise ValueError("metadata requires distinct model identifiers")
        seen.add(slug)
        modalities = row.get("input_modalities")
        if (
            not isinstance(modalities, list)
            or not modalities
            or any(value not in {"text", "image"} for value in modalities)
        ):
            raise ValueError("metadata requires declared modalities")
        levels = row.get("supported_reasoning_levels")
        if not isinstance(levels, list) or not levels:
            raise ValueError("metadata requires reasoning levels")
        for level in levels:
            if (
                not isinstance(level, dict)
                or set(level) != {"effort", "description"}
                or level["effort"]
                not in {
                    "none",
                    "minimal",
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                    "max",
                    "ultra",
                }
                or not isinstance(level["description"], str)
                or len(level["description"]) > 160
            ):
                raise ValueError("unsupported reasoning metadata")
        for key in ("display_name", "description"):
            if key in row and (
                not isinstance(row[key], str)
                or len(row[key]) > 160
                or any(char in row[key] for char in "\n\r\0")
            ):
                raise ValueError("metadata labels must be bounded single lines")
        for tier in row.get("service_tiers", []):
            if (
                not isinstance(tier, dict)
                or set(tier)
                - {
                    "id",
                    "name",
                    "description",
                }
                or tier.get("id") not in {"default", "priority", "flex"}
            ):
                raise ValueError("unsupported service tier metadata")
        for tier in row.get("service_tiers", []):
            for key in ("name", "description"):
                if key in tier and (
                    not isinstance(tier[key], str)
                    or len(tier[key]) > 160
                    or any(char in tier[key] for char in "\n\r\0")
                ):
                    raise ValueError("tier labels must be bounded single lines")
        policy = row.get("truncation_policy")
        if policy is not None and (
            not isinstance(policy, dict)
            or set(policy) != {"mode", "limit"}
            or policy["mode"] not in {"bytes", "tokens"}
            or type(policy["limit"]) is not int
            or policy["limit"] <= 0
        ):
            raise ValueError("unsupported truncation metadata")
    return data


def model_source(metadata: dict[str, Any], slug: str) -> dict[str, Any]:
    for row in metadata["models"]:
        if row["slug"] == slug:
            result = deepcopy(row)
            # CLI 0.160.0 requires instructions in catalog rows. This fixed public
            # synthetic value validates shape; it is not imported host content.
            defaults = {
                "experimental_supported_tools": [],
                "shell_type": "unified_exec",
                "apply_patch_tool_type": "freeform",
                "truncation_policy": {"mode": "tokens", "limit": 10000},
                "supports_reasoning_summaries": True,
                "supports_parallel_tool_calls": True,
                "support_verbosity": False,
                "supported_in_api": True,
            }
            for key, value in defaults.items():
                result.setdefault(key, value)
            result["base_instructions"] = "You are a coding assistant."
            return result
    raise ValueError("active route model is absent from CLI metadata")
