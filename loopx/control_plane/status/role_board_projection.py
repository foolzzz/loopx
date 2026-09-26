"""Role board projection for role_v1 goals (fork slice S8, design decision 25).

For each role_v1 goal the status payload carries a bounded ``role_board``:

- ``agents``: the registered roster with its role (``coordination.agent_roles``)
  and the agent's current activity, read from the dispatcher's private state
  (``<runtime_root>/dispatch/state.json``) plus active task leases;
- ``todos``: agent todos with the role_v1 fields the board groups by
  (status, effective role, bound agent, acceptor, reject count, acceptance flag,
  repos) and whether a Turn or lease is active on it right now;
- ``gates``: open user gates with their thread state and plan-card link.

The projection is read-only. It never writes dispatcher, gate or plan state,
and missing or unreadable sources degrade to ``unknown`` rather than failing
the status read. Board stages (planned, assigned, running, in review,
rework, done) are derived by the presentation from these raw facts.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..todos.contract import todo_effective_required_role, todo_requires_acceptance

ROLE_BOARD_SCHEMA_VERSION = "loopx_role_board_v0"
ROLE_BOARD_ROLES = ("orchestrator", "developer", "acceptor")
ROLE_BOARD_ACTIVITIES = ("running", "idle", "cooldown", "unavailable", "unknown")

MAX_ROLE_BOARD_AGENTS = 16
MAX_ROLE_BOARD_OPEN_TODOS = 60
MAX_ROLE_BOARD_DONE_TODOS = 12
MAX_ROLE_BOARD_GATES = 10
MAX_ROLE_BOARD_PLANS = 20
MAX_ROLE_BOARD_TEXT = 160
MAX_ROLE_BOARD_REPOS = 8
# Fork G2: the orchestrator-owned per-todo acceptance criteria on the card.
MAX_ROLE_BOARD_CRITERIA_TEXT = 300

_DONE_STATUSES = {"done", "completed"}
_HIDDEN_STATUSES = {"superseded", "cancelled", "canceled", "obsolete", "archived"}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _text(value: Any, limit: int = MAX_ROLE_BOARD_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _pid_alive(pid: Any) -> bool:
    from ...dispatch.state import pid_alive

    return pid_alive(pid)


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def load_dispatcher_snapshot(runtime_root: Path, *, now: float | None = None) -> dict[str, Any]:
    """Read the dispatcher's live runs and active cooldowns (read-only)."""

    from ...dispatch.state import DISPATCH_STATE_SCHEMA_VERSION, dispatch_state_path, lock_is_held

    now = time.time() if now is None else now
    state = _read_json(dispatch_state_path(runtime_root))
    if state is None or state.get("schema_version") != DISPATCH_STATE_SCHEMA_VERSION:
        return {"available": False, "serving": False, "runs": [], "agent_cooldowns": {},
                "provider_cooldowns": {}, "agent_slots": {}, "updated_at": None}

    def active(bucket: Any) -> dict[str, dict[str, Any]]:
        return {
            str(name): item
            for name, item in _dict(bucket).items()
            if isinstance(item, dict) and (_timestamp(item.get("until")) or 0) > now
        }

    runs = [
        run for run in _dict(state.get("runs")).values()
        if isinstance(run, dict) and _pid_alive(run.get("pid"))
    ]
    try:
        serving = lock_is_held(runtime_root)
    except OSError:
        serving = False
    return {
        "available": True,
        "serving": serving,
        "runs": runs,
        "agent_cooldowns": active(state.get("agent_cooldowns")),
        "provider_cooldowns": active(state.get("provider_cooldowns")),
        "agent_slots": _dict(state.get("agent_slots")),
        "updated_at": _timestamp(state.get("updated_at")),
    }


def _active_leases(runtime_root: Path, goal_id: str, *, now: float) -> dict[str, str]:
    """``todo_id -> owner`` for unexpired task leases of a Markdown-authority goal."""

    directory = Path(runtime_root) / "goals" / goal_id / "task-leases"
    leases: dict[str, str] = {}
    try:
        paths = sorted(directory.glob("todo_*.json"))[:200]
    except OSError:
        return leases
    for path in paths:
        lease = _read_json(path)
        if not lease or lease.get("status") != "active":
            continue
        expires = _timestamp(lease.get("expires_at"))
        owner = lease.get("owner")
        if expires is not None and expires > now and isinstance(owner, str) and owner:
            leases[str(lease.get("todo_id") or path.stem)] = owner
    return leases


def _plan_links(runtime_root: Path, goal_id: str) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return (todo_id -> plan link, gate todo_id -> plan summary)."""

    try:
        from ...plan_cards import list_plans

        plans = list_plans(runtime_root, goal_id)[-MAX_ROLE_BOARD_PLANS:]
    except Exception:  # noqa: BLE001 - a broken plan file must not break status
        return {}, {}
    by_todo: dict[str, dict[str, Any]] = {}
    by_gate: dict[str, dict[str, Any]] = {}
    for plan in plans:
        if not isinstance(plan, dict) or not plan.get("plan_id"):
            continue
        body = _dict(plan.get("plan"))
        summary = {
            "plan_id": str(plan["plan_id"]),
            "plan_status": _text(plan.get("status"), 40),
            "plan_title": _text(body.get("title")),
            "plan_revision": plan.get("revision") if isinstance(plan.get("revision"), int) else None,
            "plan_todo_count": len(_rows(body.get("todos"))),
            "gate_todo_id": _text(plan.get("gate_todo_id"), 80),
        }
        if summary["gate_todo_id"]:
            by_gate[summary["gate_todo_id"]] = summary
        for todo_id in _dict(plan.get("todo_id_map")).values():
            if isinstance(todo_id, str) and todo_id:
                by_todo[todo_id] = {"plan_id": summary["plan_id"], "plan_gate_todo_id": summary["gate_todo_id"]}
    return by_todo, by_gate


def _gate_index(runtime_root: Path, goal_id: str) -> dict[str, dict[str, Any]]:
    try:
        from ...gate_threads import read_gate_index

        return {str(key): value for key, value in read_gate_index(runtime_root, goal_id)["gates"].items()
                if isinstance(value, dict)}
    except Exception:  # noqa: BLE001 - thread index is advisory for the board
        return {}


def _agent_activity(
    agent_id: str,
    *,
    goal_id: str,
    dispatcher: Mapping[str, Any],
    leased_todos: list[str],
) -> dict[str, Any]:
    runs = [run for run in dispatcher.get("runs") or [] if run.get("agent_id") == agent_id]
    goal_runs = [run for run in runs if run.get("goal_id") == goal_id]
    record: dict[str, Any] = {
        "running_todo_ids": sorted({str(run["todo_id"]) for run in goal_runs if run.get("todo_id")} | set(leased_todos))[:8],
        "running_turns": len(runs),
    }
    agent_cooldown = _dict(_dict(dispatcher.get("agent_cooldowns")).get(agent_id))
    provider = _dict(_dict(dispatcher.get("agent_slots")).get(agent_id)).get("provider")
    provider_cooldown = _dict(_dict(dispatcher.get("provider_cooldowns")).get(str(provider))) if provider else {}
    if runs or leased_todos:
        record["activity"] = "running"
        if runs and not goal_runs:
            record["running_goal_ids"] = sorted({str(run.get("goal_id")) for run in runs})[:4]
    elif agent_cooldown:
        record["activity"] = "unavailable"
        record["reason"] = _text(agent_cooldown.get("status") or agent_cooldown.get("reason"), 80)
        record["until"] = _timestamp(agent_cooldown.get("until"))
    elif provider_cooldown:
        record["activity"] = "cooldown"
        record["reason"] = _text(provider_cooldown.get("kind"), 80)
        record["until"] = _timestamp(provider_cooldown.get("until"))
    elif dispatcher.get("available"):
        record["activity"] = "idle"
    else:
        record["activity"] = "unknown"
    if provider:
        record["provider"] = _text(provider, 80)
    return record


def _repositories(todo: Mapping[str, Any]) -> list[str]:
    repos = todo.get("task_repositories")
    if not isinstance(repos, list):
        return []
    return [str(repo)[:80] for repo in repos if isinstance(repo, str) and repo][:MAX_ROLE_BOARD_REPOS]


def _reject_count(todo: Mapping[str, Any]) -> int:
    value = todo.get("reject_count")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _is_gate(todo: Mapping[str, Any]) -> bool:
    return todo.get("role") == "user" and todo.get("task_class") == "user_gate"


def build_goal_role_board(
    *,
    goal: Mapping[str, Any],
    todos: Iterable[Mapping[str, Any]],
    runtime_root: Path,
    dispatcher: Mapping[str, Any],
    now: float | None = None,
) -> dict[str, Any] | None:
    """Build one goal's role board, or ``None`` for goals without role_v1 roles."""

    coordination = _dict(goal.get("coordination"))
    roles = {
        str(agent): str(role)
        for agent, role in _dict(coordination.get("agent_roles")).items()
        if role in ROLE_BOARD_ROLES
    }
    agent_model = coordination.get("agent_model")
    if agent_model == "peer_v1" or (agent_model != "role_v1" and not roles):
        return None
    goal_id = str(goal.get("id") or "")
    now = time.time() if now is None else now
    registered = [str(agent) for agent in coordination.get("registered_agents") or [] if isinstance(agent, str)]
    roster = list(dict.fromkeys([*registered, *roles]))

    leases = _active_leases(runtime_root, goal_id, now=now)
    running_by_todo: dict[str, str] = {
        str(run["todo_id"]): str(run.get("agent_id") or "")
        for run in dispatcher.get("runs") or []
        if run.get("goal_id") == goal_id and run.get("todo_id")
    }
    for todo_id, owner in leases.items():
        running_by_todo.setdefault(todo_id, owner)
    plan_by_todo, plan_by_gate = _plan_links(runtime_root, goal_id)

    agents = []
    for agent_id in roster[:MAX_ROLE_BOARD_AGENTS]:
        agents.append({
            "agent_id": agent_id,
            "role": roles.get(agent_id),
            **_agent_activity(
                agent_id,
                goal_id=goal_id,
                dispatcher=dispatcher,
                leased_todos=[todo_id for todo_id, owner in leases.items() if owner == agent_id],
            ),
        })

    open_cards: list[dict[str, Any]] = []
    done_cards: list[dict[str, Any]] = []
    gate_rows: list[dict[str, Any]] = []
    for todo in todos:
        todo_id = str(todo.get("todo_id") or "").strip()
        status = str(todo.get("status") or ("done" if todo.get("done") else "open")).strip().lower()
        if not todo_id or status in _HIDDEN_STATUSES:
            continue
        if _is_gate(todo):
            if status not in _DONE_STATUSES and not todo.get("done"):
                gate_rows.append(dict(todo))
            continue
        if todo.get("role") != "agent" or todo.get("task_class") == "continuous_monitor":
            continue
        done = status in _DONE_STATUSES
        card: dict[str, Any] = {
            "todo_id": todo_id,
            "text": _text(todo.get("title") or todo.get("text")) or todo_id,
            "status": status,
            "effective_role": todo_effective_required_role(todo),
            "required_role": todo.get("required_role") if todo.get("required_role") in ROLE_BOARD_ROLES else None,
            "claimed_by": _text(todo.get("claimed_by") or todo.get("bound_agent"), 120),
            "acceptor_agent": _text(todo.get("acceptor_agent"), 120),
            "reject_count": _reject_count(todo),
            "requires_acceptance": todo_requires_acceptance(todo),
            "task_repositories": _repositories(todo),
            "priority": _text(todo.get("priority"), 8),
            "task_class": _text(todo.get("task_class"), 40),
            "updated_at": _text(todo.get("updated_at"), 40),
        }
        criteria = _text(todo.get("acceptance_criteria"), MAX_ROLE_BOARD_CRITERIA_TEXT)
        if criteria:
            card["acceptance_criteria"] = criteria
        if not done and todo_id in running_by_todo:
            card["running"] = True
            card["running_agent_id"] = running_by_todo[todo_id] or None
        card.update(plan_by_todo.get(todo_id, {}))
        (done_cards if done else open_cards).append(card)

    # Keep the cards that need attention when the open list is truncated.
    open_cards.sort(key=lambda card: (
        card["status"] != "in_review", not card.get("running"), card["reject_count"] == 0,
    ))
    done_cards.sort(key=lambda card: card.get("updated_at") or "", reverse=True)
    gate_index = _gate_index(runtime_root, goal_id)
    gates = []
    for todo in gate_rows:
        todo_id = str(todo["todo_id"])
        entry = gate_index.get(todo_id, {})
        plan = plan_by_gate.get(todo_id) or {}
        awaiting = entry.get("awaiting") if entry.get("awaiting") in {"awaiting_user", "awaiting_orchestrator"} else "awaiting_user"
        gate = {
            "todo_id": todo_id,
            "text": _text(todo.get("title") or todo.get("text")) or todo_id,
            "kind": "plan_approval" if entry.get("kind") == "plan_approval" or plan else "decision",
            "awaiting": awaiting,
            "message_count": entry.get("message_count") if isinstance(entry.get("message_count"), int) else 0,
            "blocks_agent": _text(todo.get("blocks_agent"), 120),
            "updated_at": _text(entry.get("last_at") or todo.get("updated_at"), 40),
        }
        if plan:
            gate.update({key: plan[key] for key in ("plan_id", "plan_status", "plan_title", "plan_revision", "plan_todo_count")})
        gates.append(gate)
    gates.sort(key=lambda gate: (gate["awaiting"] != "awaiting_user", gate["kind"] != "plan_approval"))

    board: dict[str, Any] = {
        "schema_version": ROLE_BOARD_SCHEMA_VERSION,
        "agent_model": agent_model if isinstance(agent_model, str) else "role_v1",
        "dispatcher": {
            "available": bool(dispatcher.get("available")),
            "serving": bool(dispatcher.get("serving")),
            "updated_at": dispatcher.get("updated_at"),
        },
        "agents": agents,
        "todos": [*open_cards[:MAX_ROLE_BOARD_OPEN_TODOS], *done_cards[:MAX_ROLE_BOARD_DONE_TODOS]],
        "gates": gates[:MAX_ROLE_BOARD_GATES],
    }
    omitted = {
        "agents": max(0, len(roster) - MAX_ROLE_BOARD_AGENTS),
        "open_todos": max(0, len(open_cards) - MAX_ROLE_BOARD_OPEN_TODOS),
        "done_todos": max(0, len(done_cards) - MAX_ROLE_BOARD_DONE_TODOS),
        "gates": max(0, len(gates) - MAX_ROLE_BOARD_GATES),
    }
    if any(omitted.values()):
        board["omitted"] = omitted
    return board


def attach_goal_role_boards(payload: dict[str, Any], *, runtime_root: Path) -> None:
    """Attach ``run_history.goals[].role_board`` for role_v1 goals in place."""

    goals = _rows(_dict(payload.get("run_history")).get("goals"))
    if not goals:
        return
    todos_by_goal: dict[str, list[dict[str, Any]]] = {}
    for item in _rows(_dict(payload.get("todo_index")).get("items")):
        todos_by_goal.setdefault(str(item.get("goal_id") or ""), []).append(item)
    dispatcher: dict[str, Any] | None = None
    now = time.time()
    for goal in goals:
        if not _dict(goal.get("coordination")):
            continue
        if dispatcher is None:
            dispatcher = load_dispatcher_snapshot(runtime_root, now=now)
        try:
            board = build_goal_role_board(
                goal=goal,
                todos=todos_by_goal.get(str(goal.get("id") or ""), []),
                runtime_root=runtime_root,
                dispatcher=dispatcher,
                now=now,
            )
        except Exception:  # noqa: BLE001 - the board is a read model; never fail status
            board = None
        if board is not None:
            goal["role_board"] = board
