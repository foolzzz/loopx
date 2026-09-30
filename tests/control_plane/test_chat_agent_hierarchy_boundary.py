from __future__ import annotations

import json
from pathlib import Path

import pytest

from loopx.chat_action_store import ChatActionStore
from loopx.chat_actions import ChatActionService
from loopx.control_plane.agents.runtime_model import RetiredAgentHierarchyError


GOAL_ID = "chat-hierarchy-boundary"
AGENT_ID = "worker-one"


def _service(tmp_path: Path) -> ChatActionService:
    state = tmp_path / "ACTIVE_GOAL_STATE.md"
    state.write_text("# Goal\n\n## User Todo\n\n## Agent Todo\n", encoding="utf-8")
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "goals": [
                    {
                        "id": GOAL_ID,
                        "repo": str(tmp_path),
                        "state_file": state.name,
                        "coordination": {
                            "registered_agents": [AGENT_ID],
                            "agent_profiles": {
                                AGENT_ID: {
                                    "schema_version": "agent_profile_v0",
                                    "endpoint_id": "codex",
                                    "worktree_policy": "shared",
                                    "review_policy": {"can_self_merge": True},
                                }
                            },
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return ChatActionService(
        store=ChatActionStore(tmp_path / "actions"), registry_path=registry
    )


@pytest.mark.parametrize("endpoint_id", [AGENT_ID, "codex"])
def test_chat_agent_resolution_rejects_retired_hierarchy(
    tmp_path: Path, endpoint_id: str
) -> None:
    service = _service(tmp_path)

    with pytest.raises(RetiredAgentHierarchyError):
        service._resolve_goal_agent(GOAL_ID, endpoint_id)


@pytest.mark.parametrize(
    ("action_kind", "parameters"),
    [
        (
            "todo.create",
            {"goal_id": GOAL_ID, "text": "Route current work", "agent_id": AGENT_ID},
        ),
        (
            "todo.update",
            {"goal_id": GOAL_ID, "todo_id": "todo-a", "operation": "edit"},
        ),
        (
            "monitor.update",
            {
                "goal_id": GOAL_ID,
                "todo_id": "todo-a",
                "agent_id": AGENT_ID,
                "operation": "pause",
            },
        ),
    ],
)
def test_chat_work_routing_rejects_retired_hierarchy(
    tmp_path: Path, action_kind: str, parameters: dict[str, object]
) -> None:
    service = _service(tmp_path)

    with pytest.raises(RetiredAgentHierarchyError):
        service.preview(
            {
                "action_kind": action_kind,
                "summary": "Review current work routing",
                "normalized_parameters": parameters,
                "context": {},
                "idempotency_key": f"retired-{action_kind}",
            }
        )
