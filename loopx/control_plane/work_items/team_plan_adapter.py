"""Storage adapters for the typed team-plan transaction.

Legacy records and their operation receipt share one atomic Markdown write.
Promoted goals use the existing AuthorityStore transaction and projection outbox.
Neither adapter grants receiver adoption or execution authority.
"""
from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ...agent_registry import registered_agent_ids_for_goal
from ...history import load_registry
from ...registry import find_registry_goal
from ...state_refresh import now_local
from ..effect_runtime import EffectRuntimeRejected, effect_runtime_result
from ..runtime.public_safety import validate_public_safe_value
from ..coordination.local_authority import read_canonical_todos_if_promoted
from ...paths import effective_runtime_root
from ..coordination.legacy_writer_fence import legacy_todo_write_transaction
from ..coordination.runtime_shadow_writer_adapter import (
    begin_todo_runtime_shadow_capture, write_captured_todo_state,
    settle_todo_runtime_shadow_capture,
)
from ..todos.path_resolution import resolve_todo_state_path
from ..todos.active_state_editing import (
    insert_into_existing_section, insert_new_section, section_bounds,
    verify_state_text_durable, replace_updated_at,
)
from ..todos.contract import (
    TODO_ACTION_KIND_ADVANCEMENT_VALUES, format_todo_metadata_line,
    parse_todo_metadata_line,
)
from ..todos.provider_projection import settle_canonical_todo_projection


class TeamPlanCommitError(ValueError):
    def __init__(self, code: str, message: str, current_fingerprint: str | None = None):
        super().__init__(message)
        self.code = code
        self.current_fingerprint = current_fingerprint


def apply_team_plan(
    *, registry_path: Path, goal_id: str, agent_id: str | None,
    proposal: Mapping[str, Any], expected_state_fingerprint: str | None = None,
    read_fingerprint: Callable[[], str] | None = None,
) -> dict[str, Any]:
    from .governed_transition_proposal import steward_team_plan_intent_basis

    validate_public_safe_value(dict(proposal), path="team_plan")
    registry_path = Path(registry_path).expanduser()
    runtime = effective_runtime_root(registry_path, None)
    goal = find_registry_goal(load_registry(registry_path), goal_id)
    if goal is None:
        raise ValueError("steward team plan proposal names an unknown Goal")
    project, state = resolve_todo_state_path(registry_path=registry_path, goal_id=goal_id)
    request = {
        "goal_id": goal_id, "plan": dict(proposal),
        "actor_agent_id": agent_id,
        "registered_agents": registered_agent_ids_for_goal(goal),
        "supported_action_kinds": sorted(TODO_ACTION_KIND_ADVANCEMENT_VALUES),
        "observed_at": now_local(),
        "expected_state_fingerprint": expected_state_fingerprint,
        "intent_basis": steward_team_plan_intent_basis(
            goal_id=goal_id, goal=goal, registry_path=registry_path, plan=proposal),
    }
    identity = effect_runtime_result("work_items.team_plan.identity", request)
    marker = f"<!-- loopx:team-plan:{identity['operation_id'].split(':')[1]} "

    def previous_receipt(original: str) -> dict[str, Any] | None:
        matches = re.findall(re.escape(marker) + r"([A-Za-z0-9+/=]+) -->", original)
        if marker in original and not matches:
            raise ValueError("team plan operation receipt is malformed")
        if len(matches) > 1:
            raise ValueError("duplicate team plan operation receipts")
        return json.loads(base64.b64decode(matches[0], validate=True)) if matches else None

    canonical = read_canonical_todos_if_promoted(runtime_root=runtime, goal_id=goal_id)
    if canonical is not None:
        # Receipts created before promotion remain immutable historical facts.
        # Read only; never fall back to a legacy writer for a promoted goal.
        previous = previous_receipt(state.read_text(encoding="utf-8"))
        if previous is not None:
            return dict(effect_runtime_result("work_items.team_plan.plan", {
                **request, "previous_receipt": previous})["result"])
        response = effect_runtime_result("work_items.team_plan.commit", {
            **request, "runtime_root": str(runtime.resolve()),
            "expected_provider_revision": canonical["provider_revision"],
            "current_state_fingerprint": read_fingerprint() if read_fingerprint else None,
        })
        if response.get("status") not in {"applied", "recovered", "replayed", "no_change"}:
            raise TeamPlanCommitError(str(response.get("reason_code") or "team_plan_commit_failed"),
                str(response.get("reason") or "team plan commit could not be verified"),
                read_fingerprint() if read_fingerprint else None)
        projected = settle_canonical_todo_projection(dict(response), registry_path=registry_path,
            runtime_root=runtime, goal_id=goal_id, project=project, state_file=state)
        if projected.get("projection_delivery") == "pending":
            raise TeamPlanCommitError("team_plan_projection_pending", "Tasks committed; display readback is pending")
        result = dict(response["result"])
        result["projection_delivery"] = projected.get("projection_delivery")
        return result

    with legacy_todo_write_transaction(registry_path, goal_id, state, agent_id,
            "todo_add", False, runtime_root=runtime):
        original = state.read_text(encoding="utf-8")
        previous = previous_receipt(original)
        existing = [metadata for line in original.splitlines()
                    if (metadata := parse_todo_metadata_line(line)) is not None]
        current_fingerprint = read_fingerprint() if read_fingerprint else None
        try:
            planned = effect_runtime_result("work_items.team_plan.plan", {
                **request, "previous_receipt": previous, "todos": existing,
                "current_state_fingerprint": current_fingerprint,
            })
        except EffectRuntimeRejected as error:
            raise TeamPlanCommitError(error.diagnostic_code, str(error), current_fingerprint) from None
        result = dict(planned["result"])
        if planned["replayed"]:
            verify_state_text_durable(state, original)
            return result
        if not planned["todos"]:
            return result
        capture = begin_todo_runtime_shadow_capture(registry_path=registry_path,
            runtime_root=runtime, goal_id=goal_id, state_path=state,
            write_class="todo_add", original_text=original)
        lines = original.splitlines()
        for todo in planned["todos"]:
            metadata = format_todo_metadata_line(**{key: todo[key] for key in (
                "todo_id", "status", "task_class", "action_kind", "claimed_by",
                "updated_at") if key in todo})
            block = f"- [ ] {todo['text']}\n{metadata}"
            bounds = section_bounds(lines, "agent")
            if bounds:
                insert_into_existing_section(lines, bounds[0], bounds[1], block)
            else:
                insert_new_section(lines, "agent", block)
        # Store outside Todo sections so archival and canonical projections do
        # not remove operation history. Encoded JSON cannot terminate a comment.
        encoded = base64.b64encode(json.dumps(planned["receipt"], ensure_ascii=False,
            separators=(",", ":")).encode()).decode()
        first_heading = next((i for i, line in enumerate(lines) if line.startswith("#")), len(lines))
        lines[first_heading:first_heading] = [marker + encoded + " -->", ""]
        text = replace_updated_at("\n".join(lines) + "\n", str(request["observed_at"]))
        write_captured_todo_state(capture, runtime_root=runtime, goal_id=goal_id,
                                  state_path=state, text=text)
        verify_state_text_durable(state, text)
    return settle_todo_runtime_shadow_capture(result, registry_path=registry_path,
        runtime_root=runtime, goal_id=goal_id, capture=capture)
