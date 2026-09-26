"""Goal intake (fork slice S6, design decisions 13 and 21).

``loopx goal create`` turns a requirements document into a running role_v1
goal in one action:

1. bootstrap the goal in the state home (a project directory or a central
   progress repo that is separate from the code repos);
2. copy the requirements doc to ``docs/goals/<goal>/`` in the state home and
   register it as the goal's authority source;
3. register the agents and their roles, and declare the goal's code repos;
4. add the orchestrator's first todo (a planning todo claimed by the
   orchestrator), which is the durable trigger for its first turn.

Re-running with the same arguments is safe: every step is an upsert.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

REQUIREMENTS_SOURCE_ID = "goal-requirements"


def _objective_from_doc(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return " ".join(stripped.split())[:200]
    return fallback


def _parse_agents(values: list[str]) -> dict[str, str]:
    from .control_plane.agents.runtime_model import AGENT_ROLE_VALUES, normalize_agent_role

    roles: dict[str, str] = {}
    for value in values:
        agent, separator, role = str(value).partition("=")
        agent, role = agent.strip(), role.strip()
        if not separator or not agent:
            raise ValueError("--agent must use AGENT_ID=ROLE")
        normalized = normalize_agent_role(role)
        if not normalized:
            raise ValueError(f"--agent {agent}: role must be one of {', '.join(AGENT_ROLE_VALUES)}")
        if agent in roles and roles[agent] != normalized:
            raise ValueError(f"--agent {agent} given twice with different roles")
        roles[agent] = normalized
    orchestrators = [agent for agent, role in roles.items() if role == "orchestrator"]
    if len(orchestrators) != 1:
        raise ValueError("goal create needs exactly one --agent ID=orchestrator")
    return roles


def _repo_entries(values: list[str], *, base: Path) -> list[dict[str, Any]]:
    from .workspace.repos import parse_repo_spec

    entries = []
    for spec in values:
        head, comma, rest = str(spec).partition(",")
        name, eq, path = head.partition("=")
        if eq and path.strip() and not os.path.isabs(os.path.expanduser(path.strip())):
            path = str((base / path.strip()).resolve())
            spec = f"{name}={path}" + (comma + rest if comma else "")
        elif eq and path.strip():
            spec = f"{name}={os.path.expanduser(path.strip())}" + (comma + rest if comma else "")
        entries.append(parse_repo_spec(spec))
    return entries


def create_goal(
    *,
    project: Path,
    goal_id: str,
    doc: Path,
    agents: list[str],
    repos: list[str] | None = None,
    registry_path: Path | None = None,
    runtime_root: Path | None = None,
    objective: str | None = None,
    sync_global: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    from .authority import register_authority_source
    from .bootstrap import DEFAULT_DOMAIN, bootstrap_project, derive_goal_display_name
    from .configure_goal import configure_goal
    from .history import validate_goal_id_path_segment
    from .todos import add_goal_todo

    validate_goal_id_path_segment(goal_id)
    state_home = Path(project).expanduser().resolve()
    doc = Path(doc).expanduser().resolve()
    if not doc.is_file():
        raise FileNotFoundError(f"requirements doc does not exist: {doc}")
    roles = _parse_agents(agents)
    orchestrator = next(agent for agent, role in roles.items() if role == "orchestrator")
    repo_entries = _repo_entries(list(repos or []), base=Path.cwd())
    doc_text = doc.read_text(encoding="utf-8")
    goal_objective = objective or _objective_from_doc(doc_text, f"Deliver goal {goal_id}")
    registry = Path(registry_path).expanduser() if registry_path else state_home / ".loopx" / "registry.json"
    if not registry.is_absolute():
        registry = state_home / registry
    stored_doc = state_home / "docs" / "goals" / goal_id / doc.name
    relative_doc = stored_doc.relative_to(state_home)

    plan = {
        "state_home": str(state_home),
        "registry": str(registry),
        "requirements_doc": str(relative_doc),
        "agent_roles": roles,
        "orchestrator": orchestrator,
        "repos": [entry["name"] for entry in repo_entries],
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "goal_id": goal_id, **plan}

    state_home.mkdir(parents=True, exist_ok=True)
    stored_doc.parent.mkdir(parents=True, exist_ok=True)
    if stored_doc.resolve() != doc:
        shutil.copyfile(doc, stored_doc)

    steps: dict[str, Any] = {}
    from .agent_registry import load_goal_from_registry

    existing = load_goal_from_registry(registry, goal_id) if registry.exists() else None
    if existing is None:
        bootstrap = bootstrap_project(
            project=state_home, registry_path=registry, runtime_root=runtime_root, goal_id=goal_id,
            objective=goal_objective, display_name=derive_goal_display_name(goal_objective),
            domain=DEFAULT_DOMAIN, role="controller", parent_goal_id=None, state_file=None,
            goal_doc=relative_doc, adapter_kind="generic_project_goal_v0", adapter_status="connected",
            next_probe=None, spawn_allowed=False, max_children=3, allowed_domains=None,
            write_scope=None, force=False, dry_run=False, sync_global=sync_global,
        )
        if not bootstrap.get("ok"):
            raise ValueError(f"bootstrap failed: {bootstrap.get('error')}")
        steps["bootstrap"] = {"state_file": bootstrap.get("state_file"), "action": bootstrap.get("registry_goal_action")}
    else:
        steps["bootstrap"] = {"action": "existing"}

    configured = configure_goal(
        registry_path=registry, goal_id=goal_id, registered_agents=list(roles),
        agent_roles=roles, agent_model="role_v1", repos=repo_entries or None, execute=True,
    )
    if configured.get("ok") is False:
        raise ValueError(f"configure-goal failed: {configured.get('error')}")
    steps["configure"] = {"written": configured.get("written")}

    authority = register_authority_source(
        registry_path=registry, goal_id=goal_id, source_id=REQUIREMENTS_SOURCE_ID,
        source_ref=str(stored_doc), source_kind="doc", role="requirements", freshness="current",
        owner_status=None, gate_status=None, boundary="private_redacted", revision=None,
        conflict_rule=None, topic="requirements", dry_run=False,
    )
    if authority.get("ok") is False:
        raise ValueError(f"register-authority-source failed: {authority.get('error')}")
    steps["authority_source"] = {"source_id": REQUIREMENTS_SOURCE_ID}

    runtime_arg = str(runtime_root) if runtime_root else None
    first = add_goal_todo(
        registry_path=registry, goal_id=goal_id, runtime_root_arg=runtime_arg, role="agent",
        text=(
            f"Clarify the requirements in {relative_doc} with the user through gate threads, "
            "then propose the initial plan card (loopx plan propose)"
        ),
        action_kind="plan", claimed_by=orchestrator,
        role_contract={"required_role": "orchestrator", "requires_acceptance": False},
    )
    if not first.get("ok", True) or not first.get("todo_id"):
        raise ValueError(f"could not add the orchestrator planning todo: {first.get('error')}")
    steps["orchestrator_todo"] = {"todo_id": first["todo_id"], "already_exists": bool(first.get("already_exists"))}

    try:
        from .control_plane.coordination.local_authority_shadow_adapter import effective_runtime_root
        from .rollout_event_log import append_rollout_event, build_rollout_event, rollout_event_log_path

        rt = effective_runtime_root(registry, runtime_arg)
        append_rollout_event(
            rollout_event_log_path(rt, goal_id),
            build_rollout_event(
                goal_id=goal_id, event_kind="goal_intake", agent_id=orchestrator,
                todo_id=first["todo_id"], status="orchestrator_turn_requested",
                details={"agents": len(roles), "repos": len(repo_entries)},
            ),
        )
    except (OSError, ValueError):
        pass
    return {
        "ok": True, "dry_run": False, "goal_id": goal_id, **plan,
        "orchestrator_todo_id": first["todo_id"], "steps": steps,
        "next_commands": [
            f"loopx --registry {registry} todo list --goal-id {goal_id}",
            f"loopx --registry {registry} gate list --goal-id {goal_id}",
        ],
    }


def render_goal_create_markdown(payload: dict[str, Any]) -> str:
    if not payload.get("ok"):
        return f"goal create: error: {payload.get('error')}\n"
    lines = [
        f"# Goal `{payload['goal_id']}`" + (" (dry run)" if payload.get("dry_run") else " created"),
        "",
        f"- state home: {payload['state_home']}",
        f"- requirements: {payload['requirements_doc']}",
        "- agents: " + ", ".join(f"{agent}={role}" for agent, role in payload["agent_roles"].items()),
        "- repos: " + (", ".join(payload["repos"]) or "(none)"),
    ]
    if payload.get("orchestrator_todo_id"):
        lines.append(f"- orchestrator first todo: `{payload['orchestrator_todo_id']}`")
    return "\n".join(lines) + "\n"
