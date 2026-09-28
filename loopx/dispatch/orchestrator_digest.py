"""A bounded, precomputed goal-state digest for orchestrator Turns (decision 43).

Design decision 2 says the orchestrator reads all context from LoopX state
every Turn, so the digest it gets must stay compact. Before this module it got
none: the E2E pilot v1 orchestrator Turns ran 26-43 host steps, most of them
CLI reads (status, todo list, gate show, plan show) that re-sent 40-60k
tokens of context each ($1-2.6 per Turn). The dispatcher now renders this
digest at launch and puts it in the orchestrator's system-prompt addendum.

The digest is advisory: it is current as of launch, the orchestrator still
writes only through the CLI, and the Turn pipeline's validation and
writeback still guard every write. It is built from the same read models the
CLI prints (``list_goal_todos``, the gate index and threads, plan cards, the
live plan dependency waits and the rollout event log), never from dispatcher
state.

Bounds: every field is clipped, every section has an item cap with an
explicit ``… N more … omitted`` marker, and the whole text is cut at
:data:`DIGEST_MAX_CHARS` (about 5k tokens) on a line boundary with a final
truncation marker. Ordering is deterministic: todos by status rank then
state-file order, done todos by completion time (newest first) then id,
gates by thread activity, events in log order.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

DIGEST_SCHEMA_TITLE = "LoopX goal state digest"
# Hard bound on the rendered digest: about 5k tokens at 4 characters a token.
DIGEST_MAX_CHARS = 20_000
DIGEST_TRUNCATION_MARKER = "… [digest truncated at the size bound; read the rest with the CLI]"
LIVE_TODO_LIMIT = 30
DONE_TODO_LIMIT = 20
OPEN_GATE_LIMIT = 10
GATE_TAIL_MESSAGES = 2
GATE_TAIL_MESSAGES_AWAITING = 4
PENDING_PLAN_LIMIT = 5
RECENT_EVENT_LIMIT = 12
REPO_LIMIT = 12
CRITERIA_LIMIT = 8
# Per-section character budgets (they sum to less than DIGEST_MAX_CHARS). A
# section keeps whole items, in its deterministic order, until its budget is
# spent and then says how many it left out; the most actionable items come
# first (gates awaiting the orchestrator, blocked and in-review todos).
SECTION_BUDGETS = {
    "gates": 5_500,
    "plans": 1_000,
    "live_todos": 7_500,
    "closed_todos": 1_600,
    "events": 1_400,
}

_TEXT_CHARS = 160
_CRITERIA_CHARS = 280
_FEEDBACK_CHARS = 320
_WAIT_CHARS = 200
_MESSAGE_CHARS = 260
_OBJECTIVE_CHARS = 300
_STATUS_RANK = {"blocked": 0, "in_review": 1, "open": 2, "deferred": 3}
_LIVE_STATUSES = frozenset(_STATUS_RANK)
# Rollout events the orchestrator acts on; the high-volume quota and refresh
# events are left out.
_RELEVANT_EVENT_KINDS = frozenset({
    "goal_intake", "plan_proposed", "plan_decided", "todo_criteria_change", "todo_supersede",
    "todo_dependency_rewrite", "todo_review_blocked", "review_gate_decided", "push_requested",
    "push_declined", "push_result", "goal_complete_opened", "goal_complete_decided",
    "usage_budget_exhausted", "usage_budget_decided", "todo_complete", "todo_update",
})
# Only the log's tail is read: the last events, not the goal's history.
_EVENT_READ_TAIL = 400


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _omitted(count: int, what: str, hint: str) -> str:
    return f"- … {count} more {what} omitted ({hint})"


def _fit(
    header: Sequence[str], items: Sequence[Sequence[str]], *, budget: int, limit: int, what: str, hint: str,
) -> list[str]:
    """Header plus whole items in order while they fit ``budget`` and ``limit``, then an omission marker."""

    lines = list(header)
    used = sum(len(line) + 1 for line in lines)
    reserve = len(_omitted(len(items), what, hint)) + 1
    shown = 0
    for item in items[:limit]:
        size = sum(len(line) + 1 for line in item)
        if used + size + reserve > budget:
            break
        lines.extend(item)
        used += size
        shown += 1
    if shown < len(items):
        lines.append(_omitted(len(items) - shown, what, hint))
    return lines


def _goal_objective(goal: Mapping[str, Any], state_file: str | None) -> str:
    if state_file:
        try:
            from ..control_plane.goals.active_state_metadata import parse_state_frontmatter

            objective = parse_state_frontmatter(Path(state_file).read_text(encoding="utf-8")).get("objective")
        except OSError:
            objective = None
        if objective:
            return _clip(objective, _OBJECTIVE_CHARS)
    return _clip(goal.get("display_name") or goal.get("id") or "", _OBJECTIVE_CHARS)


def _requirements_doc(goal: Mapping[str, Any]) -> str | None:
    for source in goal.get("authority_sources") or []:
        if isinstance(source, Mapping) and source.get("kind") == "goal_doc" and source.get("path"):
            path = str(source["path"])
            return None if path.startswith("/") else _clip(path, 200)
    return None


def _contract_lines(agent_todos: Any) -> list[str]:
    from ..cli_commands.turn_decision import _goal_acceptance_brief

    contract = agent_todos.get("goal_acceptance_contract") if isinstance(agent_todos, Mapping) else None
    brief = _goal_acceptance_brief(contract)
    if not brief:
        return ["Goal acceptance contract: none enabled."]
    lines = ["Goal acceptance contract:"]
    if brief.get("objective"):
        lines.append(f"- objective: {_clip(brief['objective'], _OBJECTIVE_CHARS)}")
    criteria = list(brief.get("criteria") or [])
    lines += [f"- {_clip(item, 200)}" for item in criteria[:CRITERIA_LIMIT]]
    if len(criteria) > CRITERIA_LIMIT:
        lines.append(_omitted(len(criteria) - CRITERIA_LIMIT, "criteria", "loopx goal-acceptance"))
    return lines


def _repo_lines(goal: Mapping[str, Any]) -> list[str]:
    repos = [repo for repo in goal.get("repos") or [] if isinstance(repo, Mapping) and repo.get("name")]
    if not repos:
        return ["Repos: none declared."]
    parts = []
    for repo in sorted(repos, key=lambda item: str(item.get("name")))[:REPO_LIMIT]:
        extra = [f"{key}={repo[key]}" for key in ("merge_target", "default_branch") if repo.get(key)]
        parts.append(str(repo["name"]) + (f" ({', '.join(extra)})" if extra else ""))
    more = f"; … {len(repos) - REPO_LIMIT} more" if len(repos) > REPO_LIMIT else ""
    return [f"Repos: {'; '.join(parts)}{more}"]


def _agent_line(goal: Mapping[str, Any]) -> str:
    roles = (goal.get("coordination") or {}).get("agent_roles") or {}
    if not isinstance(roles, Mapping) or not roles:
        return "Agents: none registered with a role."
    return "Agents: " + ", ".join(f"{agent}={roles[agent]}" for agent in sorted(roles))


def _delivery_state(row: Mapping[str, Any], superseded: bool) -> str | None:
    from ..plan_dependencies import accepted_by
    from ..workspace.review_checkout import parse_delivered_shas

    status = str(row.get("status") or "")
    if superseded:
        return "superseded"
    shas = parse_delivered_shas(row.get("evidence"))
    delivered = ",".join(f"{name}@{sha[:10]}" for name, sha in sorted(shas.items()))
    if status == "done":
        acceptor = accepted_by(row)
        if acceptor:
            return f"accepted by {acceptor}, merged" + (f" ({delivered})" if delivered else "")
        return "done"
    if status == "in_review":
        by = row.get("delivered_by")
        return "delivered" + (f" by {by}" if by else "") + (f" ({delivered})" if delivered else "") + ", awaiting review"
    return None


def _todo_head(row: Mapping[str, Any]) -> str:
    parts = [f"`{row.get('todo_id')}` [{row.get('status')}]"]
    role = row.get("required_role")
    if role:
        parts.append(f"role={role}")
    if row.get("claimed_by"):
        parts.append(f"agent={row['claimed_by']}")
    if row.get("acceptor_agent"):
        parts.append(f"acceptor={row['acceptor_agent']}")
    if row.get("requires_acceptance") is False:
        parts.append("no-acceptance")
    reject = row.get("reject_count")
    if isinstance(reject, int) and reject > 0:
        parts.append(f"rejects={reject}")
    repos = row.get("task_repositories") or ([row["task_repository"]] if row.get("task_repository") else [])
    if repos:
        parts.append("repos=" + ",".join(str(repo) for repo in repos))
    if row.get("priority"):
        parts.append(str(row["priority"]))
    return " ".join(parts) + f": {_clip(row.get('text'), _TEXT_CHARS)}"


def _todo_lines(
    rows: Sequence[Mapping[str, Any]], waits: Mapping[str, Sequence[str]], superseded: set[str],
) -> list[str]:
    live = sorted(
        (row for row in rows if str(row.get("status") or "") in _LIVE_STATUSES),
        key=lambda row: (_STATUS_RANK[str(row.get("status"))], int(row.get("index") or 0), str(row.get("todo_id"))),
    )
    done = sorted(
        (row for row in rows if str(row.get("status") or "") not in _LIVE_STATUSES),
        key=lambda row: (str(row.get("completed_at") or row.get("updated_at") or ""), str(row.get("todo_id"))),
        reverse=True,
    )
    live_items = []
    for row in live:
        todo_id = str(row.get("todo_id"))
        item = [f"- {_todo_head(row)}"]
        if row.get("acceptance_criteria"):
            item.append(f"  criteria: {_clip(row['acceptance_criteria'], _CRITERIA_CHARS)}")
        if waits.get(todo_id):
            item.append(f"  waits: {_clip('; '.join(waits[todo_id]), _WAIT_CHARS)}")
        if row.get("review_feedback"):
            item.append(f"  feedback: {_clip(row['review_feedback'], _FEEDBACK_CHARS)}")
        delivery = _delivery_state(row, todo_id in superseded)
        if delivery:
            item.append(f"  delivery: {delivery}")
        live_items.append(item)
    done_items = []
    for row in done:
        todo_id = str(row.get("todo_id"))
        state = _delivery_state(row, todo_id in superseded) or str(row.get("status"))
        reject = row.get("reject_count")
        rejects = f" rejects={reject}" if isinstance(reject, int) and reject > 0 else ""
        done_items.append([f"- `{todo_id}` [{state}]{rejects}: {_clip(row.get('text'), 100)}"])
    return [
        *_fit([f"Agent todos ({len(live)} live, {len(done)} closed):"], live_items,
              budget=SECTION_BUDGETS["live_todos"], limit=LIVE_TODO_LIMIT, what="live todos", hint="loopx todo list"),
        *_fit([], done_items, budget=SECTION_BUDGETS["closed_todos"], limit=DONE_TODO_LIMIT,
              what="closed todos", hint="loopx todo list --status done"),
    ]


def _gate_lines(
    runtime_root: Path, goal_id: str, user_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[str], list[str]]:
    """Open user gates with a bounded thread tail, and the ids awaiting the orchestrator."""

    from ..gate_threads import AWAITING_ORCHESTRATOR, read_gate_index, read_gate_thread

    index = read_gate_index(runtime_root, goal_id)["gates"]
    open_rows = [row for row in user_rows if str(row.get("status") or "") in {"open", "blocked"}]

    def key(row: Mapping[str, Any]) -> tuple[int, str, str]:
        entry = index.get(str(row.get("todo_id"))) or {}
        awaiting = entry.get("awaiting") == AWAITING_ORCHESTRATOR
        return (0 if awaiting else 1, str(entry.get("last_at") or row.get("updated_at") or ""), str(row.get("todo_id")))

    ordered = sorted(open_rows, key=key)
    awaiting_ids = [
        str(row.get("todo_id")) for row in ordered
        if (index.get(str(row.get("todo_id"))) or {}).get("awaiting") == AWAITING_ORCHESTRATOR
    ]
    items = []
    for row in ordered:
        todo_id = str(row.get("todo_id"))
        entry = index.get(todo_id) or {}
        kind = entry.get("kind") or "system"
        awaiting = entry.get("awaiting") or "awaiting_user"
        extra = f" plan={entry['plan_id']}" if entry.get("plan_id") else ""
        if entry.get("review_todo_id"):
            extra += f" review_todo={entry['review_todo_id']}"
        if entry.get("options"):
            extra += " options=" + ",".join(str(option) for option in entry["options"])[:120]
        item = [f"- `{todo_id}` kind={kind} {awaiting}{extra}: {_clip(row.get('text'), _TEXT_CHARS)}"]
        messages = read_gate_thread(runtime_root, goal_id, todo_id) if len(items) < OPEN_GATE_LIMIT else []
        tail = GATE_TAIL_MESSAGES_AWAITING if awaiting == AWAITING_ORCHESTRATOR else GATE_TAIL_MESSAGES
        if len(messages) > tail:
            item.append(f"  … {len(messages) - tail} earlier messages (loopx gate show --todo-id {todo_id})")
        for message in messages[-tail:]:
            item.append(f"  {message.get('author')}#{message.get('seq')}: {_clip(message.get('text'), _MESSAGE_CHARS)}")
        items.append(item)
    header = [f"Open user gates ({len(open_rows)}):" if open_rows else "Open user gates: none."]
    lines = _fit(header, items, budget=SECTION_BUDGETS["gates"], limit=OPEN_GATE_LIMIT,
                 what="open gates", hint="loopx gate list")
    return lines, awaiting_ids


def _plan_lines(runtime_root: Path, goal_id: str) -> list[str]:
    from ..plan_cards import list_plans

    plans = list_plans(runtime_root, goal_id)
    pending = [plan for plan in plans if plan.get("status") in {"pending", "applying"}]
    applied = [plan for plan in plans if plan.get("status") == "applied"]
    items = []
    for plan in pending:
        body = plan.get("plan") or {}
        changes = len(body.get("criteria_changes") or [])
        items.append([
            f"- pending `{plan.get('plan_id')}` rev {plan.get('revision') or 1} gate={plan.get('gate_todo_id')}: "
            f"{_clip(body.get('title'), 120)} ({len(body.get('todos') or [])} todos"
            + (f", {changes} criteria changes" if changes else "") + ")"
        ])
    lines = _fit([f"Plan cards: {len(applied)} applied, {len(pending)} pending."], items,
                 budget=SECTION_BUDGETS["plans"] - 200, limit=PENDING_PLAN_LIMIT,
                 what="pending plans", hint="loopx plan list")
    if applied:
        last = applied[-1]
        lines.append(
            f"- last applied `{last.get('plan_id')}`: {_clip((last.get('plan') or {}).get('title'), 120)} "
            f"({len(last.get('todo_id_map') or {})} todos created)"
        )
    return lines


def _tail_events(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 512_000))
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    events = []
    for line in raw.splitlines()[-_EVENT_READ_TAIL:]:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _event_lines(runtime_root: Path, goal_id: str) -> list[str]:
    from ..rollout_event_log import rollout_event_log_path

    relevant = []
    for event in _tail_events(rollout_event_log_path(runtime_root, goal_id)):
        kind = str(event.get("event_kind") or "")
        details = event.get("details") if isinstance(event.get("details"), Mapping) else {}
        if kind not in _RELEVANT_EVENT_KINDS:
            continue
        if kind == "todo_update" and details.get("acceptance") in (None, "", "delivered"):
            continue
        relevant.append((event, details))
    items = []
    for event, details in reversed(relevant):
        extra = "".join(
            f" {key}={_clip(details[key], 60)}"
            for key in ("acceptance", "plan_id", "decision", "option") if details.get(key)
        )
        who = f" by {event['agent_id']}" if event.get("agent_id") else ""
        items.append([
            f"- {str(event.get('recorded_at') or '')[:19]} {event.get('event_kind')} "
            f"{event.get('todo_id') or ''}{who} status={event.get('status') or ''}{extra}".rstrip()
        ])
    header = ["Recent events (newest first):" if relevant else "Recent events: none."]
    return _fit(header, items, budget=SECTION_BUDGETS["events"], limit=RECENT_EVENT_LIMIT,
                what="earlier events", hint="loopx history")


def _launch_lines(todo: Mapping[str, Any] | None, launch_reason: str | None, awaiting_ids: Sequence[str]) -> list[str]:
    from ..orchestrator_bookkeeping import is_orchestrator_planning_todo
    from ..todo_acceptance import escalated_todo_id
    from .orchestrator_actions import action_gate_ids, is_orchestrator_action_todo

    if not todo:
        return [f"This Turn: launched for `{launch_reason or 'unknown'}` without a todo."]
    todo_id = todo.get("todo_id")
    if is_orchestrator_planning_todo(todo):
        kind = "planning todo (clarify through gate threads, then propose a plan card)"
    elif escalated_todo_id(todo):
        kind = f"escalation of `{escalated_todo_id(todo)}` (rejected twice, now blocked)"
    elif is_orchestrator_action_todo(todo) and action_gate_ids(todo):
        kind = "gate replies awaiting you: " + ", ".join(f"`{gate}`" for gate in action_gate_ids(todo))
    elif is_orchestrator_action_todo(todo):
        kind = "orchestrator action todo"
    else:
        kind = "orchestrator todo"
    lines = [
        f"This Turn: todo `{todo_id}` [{todo.get('status')}], {kind}; dispatcher reason `{launch_reason or 'selected_todo'}`.",
        f"  text: {_clip(todo.get('text'), 400)}",
    ]
    if awaiting_ids:
        lines.append("  gate replies awaiting you: " + ", ".join(f"`{gate}`" for gate in awaiting_ids))
    return lines


def bound_digest(text: str, max_chars: int = DIGEST_MAX_CHARS) -> str:
    """Cut ``text`` to ``max_chars`` on a line boundary with the truncation marker."""

    if len(text) <= max_chars:
        return text
    budget = max_chars - len(DIGEST_TRUNCATION_MARKER) - 1
    cut = text.rfind("\n", 0, budget)
    return text[: cut if cut > 0 else budget].rstrip() + "\n" + DIGEST_TRUNCATION_MARKER


def build_orchestrator_digest(
    *, registry_path: Path, runtime_root: Path, goal_id: str, todo_id: str | None = None,
    launch_reason: str | None = None, now: datetime | None = None, max_chars: int = DIGEST_MAX_CHARS,
) -> str:
    """Render the bounded state digest for one orchestrator Turn of ``goal_id``."""

    from ..agent_registry import load_goal_from_registry
    from ..plan_dependencies import (
        dependency_waits,
        read_supersessions,
        supersession_map,
        todo_is_superseded,
    )
    from ..todos import list_goal_todos

    goal = load_goal_from_registry(Path(registry_path), goal_id) or {"id": goal_id}
    listed = list_goal_todos(registry_path=registry_path, goal_id=goal_id, runtime_root_arg=str(runtime_root))
    rows = [row for row in listed.get("todos") or [] if isinstance(row, Mapping)]
    agent_rows = [row for row in rows if str(row.get("role") or "agent") == "agent"]
    user_rows = [row for row in rows if str(row.get("role") or "") == "user"]
    by_id = {str(row.get("todo_id")): row for row in rows}
    mapping = supersession_map(read_supersessions(runtime_root, goal_id))
    superseded = {str(row.get("todo_id")) for row in agent_rows if todo_is_superseded(row, mapping)}
    try:
        waits = dependency_waits(
            registry_path=registry_path, goal_id=goal_id, runtime_root=runtime_root,
            runtime_root_arg=str(runtime_root), rows=by_id, goal=goal,
        )
    except (OSError, ValueError):
        waits = {}
    gate_lines, awaiting_ids = _gate_lines(runtime_root, goal_id, user_rows)
    stamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    lines = [
        f"## {DIGEST_SCHEMA_TITLE} (as of launch, {stamp})",
        "",
        *_launch_lines(by_id.get(str(todo_id)) if todo_id else None, launch_reason, awaiting_ids),
        "",
        f"Goal `{goal_id}`: {_goal_objective(goal, listed.get('state_file'))}",
    ]
    doc = _requirements_doc(goal)
    if doc:
        lines.append(f"Requirements doc: {doc}")
    lines += [
        *_contract_lines(listed.get("agent_todos")),
        *_repo_lines(goal),
        _agent_line(goal),
        "",
        *gate_lines,
        "",
        *_plan_lines(runtime_root, goal_id),
        "",
        *_todo_lines(agent_rows, waits, superseded),
        "",
        *_event_lines(runtime_root, goal_id),
    ]
    return bound_digest("\n".join(lines).rstrip() + "\n", max_chars)


def orchestrator_digest_or_note(**kwargs: Any) -> str:
    """The digest, or a one-line note when it cannot be built (the Turn still runs)."""

    try:
        return build_orchestrator_digest(**kwargs)
    except Exception as exc:  # noqa: BLE001 - the digest is advisory
        return (
            f"## {DIGEST_SCHEMA_TITLE}\n\nUnavailable for this Turn ({type(exc).__name__}); "
            "read the state with `loopx todo list`, `loopx gate list` and `loopx plan list`.\n"
        )

