"""role_v1 acceptance flow (fork slice S2).

Delivery, verdict and escalation for todos that require acceptance
(design-v0 decisions 5-9 and 11):

* A developer completing a todo whose ``requires_acceptance`` is true runs the
  existing validation-command gate and, when it passes, moves the todo to
  ``in_review`` with delivery evidence instead of ``done``.
* The todo's ``acceptor_agent`` (or the goal's single ``role=acceptor`` agent)
  accepts (-> ``done`` through the ordinary completion transaction) or
  rejects (-> ``open`` for the same developer, ``reject_count`` + 1 and the
  feedback stored on the todo).
* The second rejection escalates: the todo is blocked (claim kept, never
  auto-reassigned) and an orchestrator-routed replan todo is created.

Every write goes through the existing public Todo operations
(``update_goal_todo``, ``add_goal_todo`` and the terminal completion), so the
Markdown and promoted canonical authority paths share one lifecycle.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .agent_registry import (
    acceptor_agents_for_goal,
    agent_roles_for_goal,
    load_goal_from_registry,
    orchestrator_agent_for_goal,
    registered_agent_ids_for_goal,
)
from .control_plane.agents.runtime_model import AgentRuntimeModel, agent_runtime_model_for_goal
from .control_plane.todos.contract import (
    TODO_STATUS_IN_REVIEW,
    TODO_STATUS_OPEN,
    compact_todo_text,
    normalize_todo_claimed_by,
    normalize_todo_reject_count,
    normalize_todo_requires_acceptance,
    normalize_todo_status,
    normalize_todo_task_repositories,
    todo_requires_acceptance,
    todo_review_agent,
)

TODO_ACCEPTANCE_SCHEMA_VERSION = "loopx_todo_acceptance_v0"
ESCALATION_TEXT_PREFIX = "Escalation: "
_ESCALATED_TODO = re.compile(r"^Escalation: (todo_[A-Za-z0-9_-]+) was rejected \d+ times by ")
# Design decision 6: the second rejection of the same todo escalates.
REJECT_ESCALATION_THRESHOLD = 2
_EVIDENCE_LIMIT = 900


def escalated_todo_id(escalation: Mapping[str, Any] | None) -> str | None:
    """The todo an S2 escalation todo was opened for, from its own text template."""

    if not isinstance(escalation, Mapping) or str(escalation.get("action_kind") or "") != "replan":
        return None
    match = _ESCALATED_TODO.match(str(escalation.get("text") or escalation.get("title") or ""))
    return match.group(1) if match else None


def goal_uses_role_v1(goal: Mapping[str, Any] | None) -> bool:
    try:
        return agent_runtime_model_for_goal(goal) is AgentRuntimeModel.ROLE_V1
    except ValueError:
        return False


def resolve_todo_acceptor(goal: Mapping[str, Any] | None, todo: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve who owns the verdict on ``todo`` (design decision 5)."""

    acceptors = acceptor_agents_for_goal(dict(goal) if goal else None)
    bound = normalize_todo_claimed_by(todo.get("acceptor_agent"))
    agent = todo_review_agent(todo, acceptors)
    if bound:
        source = "todo_acceptor_agent"
    elif agent:
        source = "single_goal_acceptor"
    else:
        source = "unresolved_orchestrator_decides"
    return {"agent_id": agent, "source": source, "goal_acceptors": acceptors}


def _clip(text: str, limit: int = _EVIDENCE_LIMIT) -> str:
    text = compact_todo_text(text)
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _read_todo(
    *, registry_path: Path, goal_id: str, todo_id: str, runtime_root_arg: str | None,
    project: Path | None, state_file: Path | None,
) -> dict[str, Any] | None:
    from .todos import list_goal_todos

    listed = list_goal_todos(
        registry_path=registry_path, goal_id=goal_id, todo_id=todo_id,
        runtime_root_arg=runtime_root_arg, project=project, state_file=state_file,
    )
    todo = listed.get("todo")
    return dict(todo) if isinstance(todo, Mapping) else None


def _delivery_refs(
    goal_id: str, todo: Mapping[str, Any], workspace: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Public-safe delivery refs: branch/repos (S5) and workspace identity kind."""

    refs: dict[str, Any] = {}
    repos = normalize_todo_task_repositories(todo.get("task_repositories"))
    if repos:
        from .workspace.git_workspace import todo_branch

        refs["branch"] = todo_branch(goal_id, str(todo.get("todo_id") or ""))
        refs["repos"] = repos
    elif todo.get("task_repository"):
        refs["repository"] = str(todo.get("task_repository"))
    if isinstance(workspace, Mapping):
        for key in ("workspace_kind", "identity_kind"):
            if isinstance(workspace.get(key), str) and workspace.get(key):
                refs[key] = workspace[key]
        digest = workspace.get("workspace_revision_digest")
        if isinstance(digest, str) and digest:
            refs["workspace_revision"] = digest[:16]
    return refs


def _validation_source(
    *, registry_path: Path, goal_id: str, todo_id: str, runtime_root_arg: str | None,
    project: Path | None, state_file: Path | None,
) -> dict[str, Any] | None:
    """Return the private validation declaration of the todo, if any."""

    from .control_plane.coordination.local_authority import read_canonical_todos_if_promoted
    from .control_plane.coordination.local_authority_shadow_adapter import effective_runtime_root
    from .control_plane.todos.completion_validation import (
        _materialized_todo_item,
        resolve_private_completion_validation_declaration,
    )
    from .control_plane.todos.completion_validation_projection import (
        completion_validation_declaration,
    )
    from .control_plane.todos.path_resolution import resolve_todo_state_path

    _project, resolved_state_file = resolve_todo_state_path(
        registry_path=registry_path, goal_id=goal_id, project=project, state_file=state_file,
        require_existing=False,
    )
    runtime_root = effective_runtime_root(registry_path, runtime_root_arg)
    canonical = read_canonical_todos_if_promoted(runtime_root=runtime_root, goal_id=goal_id)
    if canonical is not None:
        record = next(
            (dict(item) for item in canonical["todos"] if str(item.get("todo_id") or "") == todo_id),
            None,
        )
        if record is None:
            return None
        return resolve_private_completion_validation_declaration(
            canonical_todo=record, state_file=resolved_state_file, runtime_root=runtime_root,
            registry_path=registry_path, goal_id=goal_id, todo_id=todo_id, role="agent",
            persist_if_resolved=False,
        )
    item = _materialized_todo_item(state_file=resolved_state_file, todo_id=todo_id, role="agent")
    return completion_validation_declaration(item) if item else None


def run_delivery_validation(
    *, registry_path: Path, goal_id: str, todo: Mapping[str, Any], runtime_root_arg: str | None,
    project: Path | None, state_file: Path | None,
    delivery_workspace: Mapping[str, Any] | None,
    validation_workspace_path: Path | None,
) -> dict[str, Any] | None:
    """Run the todo's declared validation command; ``None`` when undeclared."""

    from .control_plane.todos.completion_validation import _run_declared_completion_validation
    from .control_plane.todos.completion_validation import normalize_validation_command_json
    from .control_plane.todos.completion_validation import todo_requires_todo_workspace
    from .control_plane.todos.completion_validation import todo_workspace_for_validation

    todo_id = str(todo.get("todo_id") or "")
    declaration = _validation_source(
        registry_path=registry_path, goal_id=goal_id, todo_id=todo_id,
        runtime_root_arg=runtime_root_arg, project=project, state_file=state_file,
    )
    if not declaration:
        return None
    argv = declaration.get("validation_command_argv")
    if isinstance(argv, str):
        argv = normalize_validation_command_json(argv)
    timeout = declaration.get("validation_timeout_seconds")
    return _run_declared_completion_validation(
        validation_command=declaration.get("validation_command"),
        validation_argv=list(argv) if isinstance(argv, list) else None,
        validation_label=declaration.get("validation_label"),
        validation_timeout_seconds=int(timeout) if timeout is not None else None,
        registry_path=registry_path,
        goal_id=goal_id,
        task_repository=str(todo["task_repository"]) if todo.get("task_repository") else None,
        delivery_workspace=delivery_workspace,
        validation_workspace_path=validation_workspace_path,
        todo_workspace_path=todo_workspace_for_validation(
            registry_path=registry_path, goal_id=goal_id, todo=todo, runtime_root_arg=runtime_root_arg,
        ),
        todo_workspace_required=todo_requires_todo_workspace(todo),
    )


def route_role_v1_completion(
    terminal: Callable[..., dict[str, Any]], kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Divert a developer completion to ``in_review`` when acceptance is required.

    Todos outside role_v1 (or on a role_v1 goal without registered roles),
    user todos, todos that do not require acceptance, and non-open todos
    (terminal replay) keep the ordinary completion path.
    """

    registry_path = Path(kwargs["registry_path"])
    goal_id = str(kwargs["goal_id"])
    goal = load_goal_from_registry(registry_path, goal_id)
    # A role_v1 goal without registered roles runs like flat peers: nobody
    # could review a delivery, so completion stays direct (and read-free).
    if (
        not goal_uses_role_v1(goal)
        or not agent_roles_for_goal(dict(goal) if goal else None)
        or kwargs.get("role") == "user"
    ):
        return terminal(**kwargs)
    try:
        todo = _read_todo(
            registry_path=registry_path, goal_id=goal_id, todo_id=str(kwargs["todo_id"]),
            runtime_root_arg=kwargs.get("runtime_root_arg"),
            project=kwargs.get("project"), state_file=kwargs.get("state_file"),
        )
    except ValueError:
        todo = None
    if todo is None or todo.get("role") != "agent":
        return terminal(**kwargs)
    status = normalize_todo_status(todo.get("status"))
    actor = normalize_todo_claimed_by(kwargs.get("agent_id")) or normalize_todo_claimed_by(
        kwargs.get("claimed_by")
    )
    if (
        status == TODO_STATUS_IN_REVIEW
        and kwargs.get("completion_turn_key")
        and actor
        and normalize_todo_claimed_by(todo.get("delivered_by")) == actor
    ):
        # A retried Turn settlement observes its own committed delivery.
        return {
            "ok": True, "dry_run": bool(kwargs.get("dry_run")), "role": "agent", "completed": False,
            "in_review": True, "status": TODO_STATUS_IN_REVIEW, "changed": False,
            "idempotent_replay": True, "goal_id": goal_id, "todo_id": todo["todo_id"],
        }
    if status == TODO_STATUS_IN_REVIEW and actor and actor == resolve_todo_acceptor(goal, todo)["agent_id"]:
        # The resolved acceptor completing a delivered todo (for example an
        # acceptor Turn returning validated_completion) is its accept verdict.
        return accept_goal_todo(
            registry_path=registry_path, goal_id=goal_id, todo_id=str(todo["todo_id"]),
            agent_id=actor, note=kwargs.get("note"), evidence=kwargs.get("evidence"),
            runtime_root_arg=kwargs.get("runtime_root_arg"),
            project=kwargs.get("project"), state_file=kwargs.get("state_file"),
            dry_run=bool(kwargs.get("dry_run")),
            completion_options={
                key: kwargs[key] for key in (
                    "completion_turn_key", "completion_identity_source",
                    "completion_delivery_workspace", "completion_validation_workspace_path",
                ) if kwargs.get(key) is not None
            },
        )
    if status == TODO_STATUS_IN_REVIEW:
        reviewer = resolve_todo_acceptor(goal, todo)
        raise ValueError(
            f"todo_id {todo['todo_id']!r} is in_review awaiting "
            f"{reviewer['agent_id'] or 'an acceptor chosen by the orchestrator'}; "
            "record the verdict with `loopx todo accept` or `loopx todo reject`"
        )
    if status != TODO_STATUS_OPEN or not delivery_requires_review(goal, todo):
        return terminal(**kwargs)
    return deliver_goal_todo_for_review(goal=goal, todo=todo, **kwargs)


def delivery_requires_review(goal: Mapping[str, Any] | None, todo: Mapping[str, Any]) -> bool:
    """Whether a developer completion of ``todo`` goes to ``in_review``.

    An explicit ``requires_acceptance`` wins. The implicit default (true for
    developer advancement work) applies only when the goal has an acceptor to
    review it, a bound ``acceptor_agent`` or at least one ``role=acceptor``
    agent, so role_v1 goals without acceptors keep completing directly.
    """

    if not todo_requires_acceptance(todo):
        return False
    if normalize_todo_requires_acceptance(todo.get("requires_acceptance")) is True:
        return True
    return bool(
        normalize_todo_claimed_by(todo.get("acceptor_agent"))
        or acceptor_agents_for_goal(dict(goal) if goal else None)
    )


def deliver_goal_todo_for_review(
    *, goal: Mapping[str, Any] | None, todo: Mapping[str, Any], registry_path: Path, goal_id: str,
    todo_id: str, runtime_root_arg: str | None = None, evidence: str | None = None,
    note: str | None = None, agent_id: str | None = None, claimed_by: str | None = None,
    completion_delivery_workspace: Mapping[str, Any] | None = None,
    completion_validation_workspace_path: Path | None = None,
    project: Path | None = None, state_file: Path | None = None, dry_run: bool = False,
    **_ignored_completion_options: Any,
) -> dict[str, Any]:
    """Validate a developer delivery and move the todo to ``in_review``.

    Successor/no-follow-up completion options are not applied here: follow-up
    work is planned by the orchestrator once the acceptor has decided. Task
    lease proofs are not consumed either: canonical ``hard_lease`` goals only
    change a leased todo's status through an atomic lifecycle operation, which
    does not exist for ``in_review`` yet, so such a delivery is rejected by the
    kernel's lease fence and the todo stays open.
    """

    from .todos import update_goal_todo

    if _ignored_completion_options.get("next_user_todo"):
        # Decision 11 (S6): only the orchestrator opens user gates, also on delivery.
        from .gate_threads import require_user_gate_author

        require_user_gate_author(
            registry_path=registry_path, goal_id=goal_id,
            actor_agent_id=agent_id or claimed_by, surface="--next-user-todo",
        )
    author = (
        normalize_todo_claimed_by(agent_id)
        or normalize_todo_claimed_by(claimed_by)
        or normalize_todo_claimed_by(todo.get("claimed_by"))
    )
    receipt = None
    if not dry_run:
        receipt = run_delivery_validation(
            registry_path=registry_path, goal_id=goal_id, todo=todo,
            runtime_root_arg=runtime_root_arg, project=project, state_file=state_file,
            delivery_workspace=completion_delivery_workspace,
            validation_workspace_path=completion_validation_workspace_path,
        )
    if receipt is not None and receipt.get("passed") is not True:
        return {
            "ok": False, "dry_run": dry_run, "completed": False, "in_review": False,
            "goal_id": goal_id, "todo_id": todo_id, "changed": False,
            "validation": receipt, "validation_blocked_completion": True,
        }
    refs = _delivery_refs(goal_id, todo, completion_delivery_workspace)
    if refs.get("repos") and isinstance(goal, Mapping) and not dry_run:
        # G12: pin the delivered commit per repo; the acceptor reviews it in a
        # detached checkout and the accept merge merges exactly this sha.
        from .workspace.review_checkout import delivered_branch_shas, format_delivered_shas

        shas = delivered_branch_shas(goal, todo_id, refs["repos"])
        if shas:
            refs["delivered_shas"] = format_delivered_shas(shas)
    validation_label = (
        f"passed({receipt.get('command_label') or 'validation'})" if receipt else "not_declared"
    )
    parts = [f"delivered_by={author or 'unattributed'}"]
    parts += [f"{key}={','.join(value) if isinstance(value, list) else value}" for key, value in refs.items()]
    parts.append(f"validation={validation_label}")
    if evidence:
        parts.append(compact_todo_text(evidence))
    delivery_evidence = _clip("; ".join(parts))
    result = update_goal_todo(
        registry_path=registry_path, goal_id=goal_id, todo_id=todo_id, role="agent",
        runtime_root_arg=runtime_root_arg, status=TODO_STATUS_IN_REVIEW,
        evidence=delivery_evidence, note=note, agent_id=author,
        role_contract={"delivered_by": author} if author else None,
        project=project, state_file=state_file, dry_run=dry_run,
    )
    if result.get("ok") is False:
        return {**result, "completed": False, "in_review": False}
    reviewer = resolve_todo_acceptor(goal, todo)
    return {
        **result,
        "ok": True,
        "role": "agent",
        "completed": False,
        "in_review": True,
        "status": TODO_STATUS_IN_REVIEW,
        "acceptance": {
            "schema_version": TODO_ACCEPTANCE_SCHEMA_VERSION,
            "transition": "delivered",
            "delivered_by": author,
            "delivery_refs": refs,
            "validation": receipt,
            "acceptor": reviewer,
        },
    }


def _require_verdict_actor(
    *, goal: Mapping[str, Any] | None, todo: Mapping[str, Any], agent_id: str | None, verb: str,
) -> tuple[str, dict[str, Any]]:
    if not goal_uses_role_v1(goal):
        raise ValueError(f"todo {verb} requires a role_v1 goal")
    actor = normalize_todo_claimed_by(agent_id)
    if not actor:
        raise ValueError(f"todo {verb} requires --agent-id of the acceptor")
    if actor not in registered_agent_ids_for_goal(dict(goal) if goal else None):
        raise ValueError(f"agent_id={actor!r} is not registered for this goal")
    if todo.get("role") != "agent":
        raise ValueError(f"todo {verb} applies only to agent todos")
    status = normalize_todo_status(todo.get("status"))
    if status != TODO_STATUS_IN_REVIEW:
        raise ValueError(
            f"todo_id {todo.get('todo_id')!r} is status={status!r}; only in_review todos take a verdict"
        )
    reviewer = resolve_todo_acceptor(goal, todo)
    if reviewer["agent_id"] is None:
        raise ValueError(
            "no acceptor is bound to this todo and the goal does not have exactly one "
            f"role=acceptor agent (found: {', '.join(reviewer['goal_acceptors']) or 'none'}); "
            "the orchestrator must bind one with `loopx todo update --acceptor-agent`"
        )
    if actor != reviewer["agent_id"]:
        raise ValueError(
            f"agent_id={actor!r} cannot {verb} todo_id={todo.get('todo_id')!r}; "
            f"its acceptor is {reviewer['agent_id']!r} ({reviewer['source']})"
        )
    return actor, reviewer


def _accepted_workspace(
    *, registry_path: Path, goal: Mapping[str, Any] | None, todo: Mapping[str, Any],
    runtime_root_arg: str | None,
) -> dict[str, Any] | None:
    """The S5 workspace an accepted todo merges from, if any.

    Eligibility keys on the todo branch ``loopx/<goal>/<todo>``, not on the
    worktree directories: after ``workspace cleanup`` (or a lost runtime
    directory) the branch still carries the delivered commits, and the merge
    preflight tolerates a missing worktree. When the branch exists in only
    some of the todo's repos, the merge blocks on the others
    (``todo_branch_missing``) instead of completing without them.
    """

    repos = normalize_todo_task_repositories(todo.get("task_repositories"))
    todo_id = str(todo.get("todo_id") or "")
    if not repos or not todo_id or not isinstance(goal, Mapping):
        return None
    from .control_plane.coordination.local_authority_shadow_adapter import effective_runtime_root
    from .workspace.git_workspace import repos_with_todo_branch

    if not repos_with_todo_branch(goal, todo_id, repos):
        return None
    return {"repos": repos, "runtime_root": effective_runtime_root(registry_path, runtime_root_arg)}


def _public_merge(payload: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("ok", "error_code", "merged", "would_merge", "repos", "conflict_report", "failure", "rolled_back")
    return {key: payload[key] for key in keys if key in payload}


def _delivery_moved(preflight: Mapping[str, Any]) -> bool:
    report = preflight.get("conflict_report")
    return isinstance(report, Mapping) and any(
        isinstance(blocker, Mapping) and blocker.get("error_code") == "delivery_moved"
        for item in report.get("repos") or [] if isinstance(item, Mapping)
        for blocker in item.get("blockers") or []
    )


def _merge_conflict_feedback(actor: str, preflight: Mapping[str, Any], *, delivery_moved: bool = False) -> str:
    """Repo-relative feedback for a blocked or failed merge."""

    report = preflight.get("conflict_report")
    parts: list[str] = []
    if isinstance(report, Mapping):
        for item in report.get("repos") or []:
            if not isinstance(item, Mapping):
                continue
            for blocker in item.get("blockers") or []:
                if not isinstance(blocker, Mapping):
                    continue
                # Repo-relative paths only; blocker reasons can name local paths.
                paths = blocker.get("conflicted_paths") or blocker.get("paths") or []
                parts.append(
                    f"{item.get('name')}: {blocker.get('error_code')}"
                    + (f" ({', '.join(str(path) for path in list(paths)[:5])})" if paths else "")
                )
    failure = preflight.get("failure")
    if isinstance(failure, Mapping):
        # merge_apply_failed: the check passed but applying the merge did not
        # (for example the target moved); nothing stayed merged.
        parts.append(f"{failure.get('name')}: {failure.get('error_code')}")
    detail = "; ".join(parts)
    if delivery_moved:
        return _clip(
            f"accepted by {actor}, but the todo branch moved after delivery, so the reviewed commit "
            f"is no longer its tip and nothing was merged; deliver again: {detail}",
            600,
        )
    return _clip(
        f"accepted by {actor} but the merge into the target is blocked; rebase or resolve on the "
        f"todo branch and deliver again: {detail or preflight.get('error_code') or 'merge_blocked'}",
        600,
    )


def accept_goal_todo(
    *, registry_path: Path, goal_id: str, todo_id: str, agent_id: str | None,
    note: str | None = None, evidence: str | None = None, runtime_root_arg: str | None = None,
    project: Path | None = None, state_file: Path | None = None, dry_run: bool = False,
    completion_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Acceptor verdict: accept a delivered todo (``in_review`` -> ``done``).

    ``completion_options`` carries Turn settlement identity (turn key,
    identity source, delivery workspace) into the terminal completion so an
    acceptor Turn's accept replays idempotently.
    """

    goal = load_goal_from_registry(registry_path, goal_id)
    todo = _read_todo(
        registry_path=registry_path, goal_id=goal_id, todo_id=todo_id,
        runtime_root_arg=runtime_root_arg, project=project, state_file=state_file,
    )
    if todo is None:
        raise ValueError(f"todo_id {todo_id!r} was not found")
    actor, reviewer = _require_verdict_actor(goal=goal, todo=todo, agent_id=agent_id, verb="accept")
    return accept_delivered_todo(
        registry_path=registry_path, goal=goal, goal_id=goal_id, todo=todo, actor=actor,
        actor_source=reviewer["source"], note=note, evidence=evidence,
        runtime_root_arg=runtime_root_arg, project=project, state_file=state_file,
        dry_run=dry_run, completion_options=completion_options,
    )


def accept_delivered_todo(
    *, registry_path: Path, goal: Mapping[str, Any] | None, goal_id: str, todo: Mapping[str, Any],
    actor: str, actor_source: str, note: str | None = None, evidence: str | None = None,
    runtime_root_arg: str | None = None, project: Path | None = None, state_file: Path | None = None,
    dry_run: bool = False, completion_options: Mapping[str, Any] | None = None,
    lifecycle_agent_id: str | None = None,
) -> dict[str, Any]:
    """Merge first, then complete an ``in_review`` todo whose verdict ``actor`` owns.

    The acceptor's verdict and the owner's manual accept (an acceptor-blocked
    gate, G12) share this path; the caller has checked the actor's authority.
    ``lifecycle_agent_id`` is the registered agent that the writes of an
    unclaimed todo are attributed to when ``actor`` is not an agent (the owner).
    """

    from .agent_registry import lifecycle_agent_for_owner_write
    from .todos import terminal_complete_goal_todo

    todo_id = str(todo.get("todo_id") or "")
    # The lifecycle actor of the completion stays the delivering claim owner,
    # so claim fences and leases are unchanged; the verdict names the acceptor.
    # An unclaimed todo needs a registered agent: the owner is not one.
    owner = lifecycle_agent_for_owner_write(
        dict(goal or {}), todo.get("claimed_by"), lifecycle_agent_id, actor,
    ) or actor
    verdict = f"accepted_by={actor}"
    if note:
        verdict += f": {compact_todo_text(note)}"
    completion_evidence = _clip("; ".join(
        part for part in (verdict, compact_todo_text(evidence) if evidence else "",
                          compact_todo_text(todo.get("evidence")) if todo.get("evidence") else "") if part
    ))
    workspace = _accepted_workspace(
        registry_path=registry_path, goal=goal, todo=todo, runtime_root_arg=runtime_root_arg,
    )
    merge_payload = None
    if workspace is not None and not dry_run:
        from .todos import update_goal_todo
        from .workspace import git_workspace
        from .workspace.review_checkout import parse_delivered_shas

        # The real merge runs BEFORE the terminal completion, so a todo is
        # never ``done`` without its merge. The merge is atomic across the
        # todo's repos (its own preflight runs under the goal lock) and
        # retry-idempotent: a repo whose target already contains the todo
        # branch is skipped. It merges exactly the delivered sha recorded at
        # delivery (G12); a todo branch that moved since blocks the merge.
        # Nothing fetches or pushes (a push stays behind a user gate).
        delivered = parse_delivered_shas(todo.get("evidence"))
        merged = git_workspace.merge(
            goal, todo_id, workspace["repos"], workspace["runtime_root"],
            **({"expected_source_shas": delivered} if delivered else {}),
        )
        if not merged.get("ok"):
            # Decisions 18 and 22: a blocked merge (conflict, dirty worktree,
            # moved target, failed apply) returns the whole todo to its
            # developer. It is not a quality verdict, so reject_count is kept.
            moved = _delivery_moved(merged)
            feedback = _merge_conflict_feedback(actor, merged, delivery_moved=moved)
            reopened = update_goal_todo(
                registry_path=registry_path, goal_id=goal_id, todo_id=todo_id, role="agent",
                runtime_root_arg=runtime_root_arg, status=TODO_STATUS_OPEN,
                role_contract={"review_feedback": feedback}, agent_id=owner,
                project=project, state_file=state_file, dry_run=False,
            )
            return {
                **reopened,
                "merge": _public_merge(merged),
                "acceptance": {
                    "schema_version": TODO_ACCEPTANCE_SCHEMA_VERSION,
                    "transition": "merge_blocked", "verdict": "accept", "acceptor": actor,
                    "acceptor_source": actor_source, "delivered_by": todo.get("delivered_by"),
                    "review_feedback": feedback,
                    **({"reason": "delivery_moved"} if moved else {}),
                },
            }
        merge_payload = _public_merge(merged)
    result = terminal_complete_goal_todo(
        registry_path=registry_path, goal_id=goal_id, todo_id=todo_id, role="agent",
        runtime_root_arg=runtime_root_arg, evidence=completion_evidence,
        note=_clip(verdict, 400), agent_id=owner,
        project=project, state_file=state_file, dry_run=dry_run,
        **dict(completion_options or {}),
    )
    completed = result.get("ok") is not False
    resumed: list[str] = []
    if not dry_run and completed:
        from .control_plane.coordination.local_authority_shadow_adapter import effective_runtime_root
        from .plan_cards import resume_ready_plan_todos

        # Dependents of this todo in an applied plan card become workable. The
        # merge (when the todo has a workspace) has already landed here.
        resumed = resume_ready_plan_todos(
            registry_path=registry_path, goal_id=goal_id,
            runtime_root=effective_runtime_root(registry_path, runtime_root_arg),
            runtime_root_arg=runtime_root_arg,
        )
    # A completion refused after a successful merge (the re-run validation
    # failed) leaves the todo ``in_review``: the merged branch is recorded in
    # ``merge`` and a later accept re-merges as a no-op, so the verdict can be
    # retried, or turned into a reject that returns the todo to its developer.
    return {
        **result,
        **({"merge": merge_payload} if merge_payload is not None else {}),
        **({"resumed_todo_ids": resumed} if resumed else {}),
        "acceptance": {
            "schema_version": TODO_ACCEPTANCE_SCHEMA_VERSION,
            "transition": "accepted" if completed else "completion_blocked",
            "verdict": "accept", "acceptor": actor,
            "acceptor_source": actor_source, "delivered_by": todo.get("delivered_by"),
            "note": compact_todo_text(note) if note else None,
        },
    }


def reject_goal_todo(
    *, registry_path: Path, goal_id: str, todo_id: str, agent_id: str | None,
    note: str | None, runtime_root_arg: str | None = None,
    project: Path | None = None, state_file: Path | None = None, dry_run: bool = False,
) -> dict[str, Any]:
    """Acceptor verdict: reject a delivered todo.

    The first rejection reopens the todo for the same developer with the
    feedback stored on it. The second escalates to the orchestrator.
    """

    from .control_plane.todos.review_feedback import fit_review_feedback, strip_trailing_host_glyphs
    from .todos import add_goal_todo, update_goal_todo

    feedback = strip_trailing_host_glyphs(compact_todo_text(note))
    if not feedback:
        raise ValueError(
            "todo reject requires --note with the acceptor's feedback naming each failed acceptance "
            "criterion; if you cannot review, use `loopx todo block-review --reason ...`"
        )
    goal = load_goal_from_registry(registry_path, goal_id)
    todo = _read_todo(
        registry_path=registry_path, goal_id=goal_id, todo_id=todo_id,
        runtime_root_arg=runtime_root_arg, project=project, state_file=state_file,
    )
    if todo is None:
        raise ValueError(f"todo_id {todo_id!r} was not found")
    actor, reviewer = _require_verdict_actor(goal=goal, todo=todo, agent_id=agent_id, verb="reject")
    # Like accept, the write is attributed to the claim owner so the kernel's
    # claim fence is unchanged; the acceptor's authority was checked above and
    # is recorded in review_feedback and the returned verdict.
    owner = normalize_todo_claimed_by(todo.get("claimed_by")) or actor
    count = (normalize_todo_reject_count(todo.get("reject_count")) or 0) + 1
    # Pilot v1 N6: use the whole review_feedback contract (600), criteria first.
    review_feedback = fit_review_feedback(f"rejected by {actor} (#{count}): ", feedback)
    escalate = count >= REJECT_ESCALATION_THRESHOLD
    orchestrator = orchestrator_agent_for_goal(dict(goal) if goal else None)
    role_contract = {"reject_count": count, "review_feedback": review_feedback}
    if escalate:
        reason = (
            f"escalated to the orchestrator after {count} rejections; "
            "awaiting reassign, split, revised criteria or a user gate"
        )
        result = update_goal_todo(
            registry_path=registry_path, goal_id=goal_id, todo_id=todo_id, role="agent",
            runtime_root_arg=runtime_root_arg, status="blocked", reason=reason,
            role_contract=role_contract, agent_id=owner,
            project=project, state_file=state_file, dry_run=dry_run,
        )
        escalation_text = (
            f"{ESCALATION_TEXT_PREFIX}{todo_id} was rejected {count} times by {actor}. Decide: reassign, "
            f"split, revise the acceptance criteria, or open a user gate. Latest feedback: {feedback}"
        )
        escalation = add_goal_todo(
            registry_path=registry_path, goal_id=goal_id, role="agent",
            runtime_root_arg=runtime_root_arg, text=_clip(escalation_text, 480),
            task_class="advancement_task", action_kind="replan",
            claimed_by=orchestrator,
            role_contract={"required_role": "orchestrator", "requires_acceptance": False},
            project=project, state_file=state_file, dry_run=dry_run,
        )
    else:
        result = update_goal_todo(
            registry_path=registry_path, goal_id=goal_id, todo_id=todo_id, role="agent",
            runtime_root_arg=runtime_root_arg, status=TODO_STATUS_OPEN,
            role_contract=role_contract, agent_id=owner,
            project=project, state_file=state_file, dry_run=dry_run,
        )
        escalation = None
    acceptance: dict[str, Any] = {
        "schema_version": TODO_ACCEPTANCE_SCHEMA_VERSION,
        "transition": "escalated" if escalate else "reopened",
        "verdict": "reject", "acceptor": actor, "acceptor_source": reviewer["source"],
        "reject_count": count, "review_feedback": review_feedback,
        "developer": normalize_todo_claimed_by(todo.get("claimed_by"))
        or normalize_todo_claimed_by(todo.get("delivered_by")),
    }
    if escalation is not None:
        acceptance["escalation"] = {
            "todo_id": escalation.get("todo_id"), "routed_to": orchestrator,
            "required_role": "orchestrator", "action_kind": "replan",
        }
    return {**result, "acceptance": acceptance}


def reject_delivery_from_turn(
    *, registry_path: Path, goal_id: str, todo_id: str, agent_id: str, feedback: str,
    runtime_root_arg: str | None = None,
) -> bool:
    """Record an acceptor Turn's ``repair_required`` result as a reject verdict.

    Returns ``False`` (nothing written) unless the Todo is ``in_review`` on a
    role_v1 goal and ``agent_id`` is its resolved acceptor, so a retried Turn
    that already reopened the Todo falls back to the ordinary repair note.
    A reject must name what failed (G12): an empty or blank summary is the
    acceptor's ``blocked`` verdict instead (``loopx.todo_review_blocked``).
    """

    goal = load_goal_from_registry(registry_path, goal_id)
    if not goal_uses_role_v1(goal):
        return False
    todo = _read_todo(
        registry_path=registry_path, goal_id=goal_id, todo_id=todo_id,
        runtime_root_arg=runtime_root_arg, project=None, state_file=None,
    )
    actor = normalize_todo_claimed_by(agent_id)
    if (
        todo is None
        or normalize_todo_status(todo.get("status")) != TODO_STATUS_IN_REVIEW
        or not actor
        or actor != resolve_todo_acceptor(goal, todo)["agent_id"]
    ):
        return False
    if not compact_todo_text(feedback):
        from .todo_review_blocked import block_goal_todo_review

        block_goal_todo_review(
            registry_path=registry_path, goal_id=goal_id, todo_id=todo_id, agent_id=actor,
            reason="the acceptor returned repair_required without feedback, so no reject was recorded",
            runtime_root_arg=runtime_root_arg,
        )
        return True
    reject_goal_todo(
        registry_path=registry_path, goal_id=goal_id, todo_id=todo_id, agent_id=actor,
        note=feedback, runtime_root_arg=runtime_root_arg,
    )
    return True
