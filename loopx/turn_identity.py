"""Public-safe identity helpers shared by LoopX turn entrypoints."""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping


TURN_INSTANCE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
HOST_OWNED_TURN_INSTANCE_ID_PLACEHOLDER = (
    "<unique-work-iteration-id-reuse-on-retry>"
)
HOST_OWNED_TURN_INSTANCE_ID_VALUE_CONTRACT = (
    "1-128 public-safe letters, numbers, or ._:-"
)


def host_turn_materialization_required(
    explicit_goal_start: bool,
    selected_agent_id: object,
    runtime_profile: object,
    supported_profiles: set[str],
) -> bool:
    return bool(
        explicit_goal_start
        and selected_agent_id
        and runtime_profile in supported_profiles
    )


def host_turn_identity_materialization_contract(
    command_template: object,
) -> dict[str, str] | None:
    if not isinstance(command_template, str) or (
        HOST_OWNED_TURN_INSTANCE_ID_PLACEHOLDER not in command_template
    ):
        return None
    return {
        "schema_version": "loopx_host_turn_identity_materialization_v0",
        "owner": "host",
        "placeholder": HOST_OWNED_TURN_INSTANCE_ID_PLACEHOLDER,
        "value_contract": HOST_OWNED_TURN_INSTANCE_ID_VALUE_CONTRACT,
        "command_template": command_template,
        "materialization": (
            "generate one unique id for this work iteration, replace the placeholder "
            "before execution, and retain that materialized command for retries"
        ),
        "retry_policy": "reuse_exact_same_turn_instance_id",
    }


def render_host_turn_identity_materialization_markdown(contract: object) -> str:
    if not isinstance(contract, Mapping):
        return ""
    return f"""
The template is not executable until materialized. The host must generate one
Turn id matching `{contract.get('value_contract')}`, replace
`{contract.get('placeholder')}` once, retain the materialized command, and reuse
that exact id for every retry of this work iteration:

````text
{contract.get('command_template') or ''}
````

Retry policy: `{contract.get('retry_policy')}`.
"""


def host_turn_quota_guard_step(
    command: object,
) -> tuple[dict[str, str] | None, dict[str, str]]:
    contract = host_turn_identity_materialization_contract(command)
    return contract, {
        "id": "quota_guard",
        "kind": "host_materialized_guard" if contract else "guard",
        **(
            {
                "materialization_contract_ref": (
                    "#/guided_transaction/host_turn_identity_contract"
                )
            }
            if contract
            else {"command": str(command or "")}
        ),
        "purpose": "let LoopX choose the first bounded segment and scheduler cadence",
    }


def host_turn_guided_projection(command: object) -> dict[str, object]:
    contract, step = host_turn_quota_guard_step(command)
    return {
        "transaction_fields": (
            {"host_turn_identity_contract": contract} if contract else {}
        ),
        "step": step,
    }


def render_host_turn_identity_section(contract: object) -> str:
    guidance = render_host_turn_identity_materialization_markdown(contract)
    return f"\n## Host-owned Turn Identity\n{guidance}\n" if guidance else ""


def render_quota_guard_materialization_markdown(command: object) -> str:
    contract = host_turn_identity_materialization_contract(command)
    if contract:
        return (
            "\nBefore the quota guard, materialize its host-owned Turn identity.\n"
            + render_host_turn_identity_materialization_markdown(contract)
        )
    return f"\nRun the quota guard:\n\n```bash\n{command or ''}\n```"


def normalize_turn_instance_id(value: str | None) -> str | None:
    normalized = str(value).strip() if value is not None else None
    if normalized is not None and not TURN_INSTANCE_ID_RE.fullmatch(normalized):
        raise ValueError(
            "turn_instance_id must be 1-128 public-safe letters, numbers, or ._:-"
        )
    return normalized


def mint_turn_instance_id(*, prefix: str) -> str:
    """Create a public-safe opaque identity for a newly admitted Turn."""

    normalized_prefix = normalize_turn_instance_id(prefix)
    if not normalized_prefix:
        raise ValueError("turn_instance_id prefix must be non-empty")
    minted = normalize_turn_instance_id(
        f"{normalized_prefix}:{secrets.token_hex(16)}"
    )
    if minted is None:  # pragma: no cover - the constructed value is non-empty
        raise RuntimeError("failed to mint turn_instance_id")
    return minted
