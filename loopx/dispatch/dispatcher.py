"""Resident dispatcher: decides *when* each registered agent runs a Turn.

The dispatcher is a scheduler, not a second state machine (design-v0
decisions 3-4). Per goal and agent it asks LoopX's quota ``should-run``
decision whether the agent has work, keeps slot, cooldown and auth rules, and
launches ``loopx turn run-once`` as a child process. The Turn pipeline owns
the typed result, validation, idempotent writeback and quota spend; the
dispatcher only records what happened to its children.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import policy
from ..gate_threads import gates_awaiting_orchestrator
from ..plan_criteria_changes import CRITERIA_CHANGE_PENDING_REASON, criteria_change_pending_todo_ids
from ..plan_dependencies import read_dependency_wait_snapshot
from .orchestrator_actions import (
    ORCHESTRATOR_ACTION_REPEAT_LIMIT,
    action_todo_text,
    ORCHESTRATOR_ACTION_FAILED_TURN_LIMIT,
    ORCHESTRATOR_ACTION_RETIRED_REASON,
    action_subject,
    action_validator_argv,
    is_orchestrator_action_todo,
    live_orchestrator_todo,
)
from .orchestrator_digest import orchestrator_digest_or_note
from .prompts import compose_system_prompt, dispatch_prompt_addendum, replace_system_prompt_argument
from . import settlement_retry
from .review_checkouts import (
    blocked_review_todo_ids,
    is_review_turn,
    prepare_acceptor_review,
    remove_acceptor_review,
    review_run_record,
    settle_acceptor_review,
)
from .usage_budget import alert_usage_budget
from ..usage_budget_gate import budget_hold
from .state import (
    DispatchLock,
    dispatch_dir,
    load_state,
    pid_alive,
    save_state,
)

DISPATCH_PASS_SCHEMA_VERSION = "loopx_dispatch_pass_v0"
# Decision 43: bookkeeping todos one orchestrator may close per pass before it launches.
ORCHESTRATOR_BOOKKEEPING_PASS_LIMIT = 5
ORCHESTRATOR_BOOKKEEPING_LIMIT_REASON = "orchestrator_bookkeeping_limit"
DEFAULT_LOOPX_ARGV = (sys.executable, "-m", "loopx.cli")

ShouldRun = Callable[[str, str], Mapping[str, Any]]
Preflight = Callable[[Any, Mapping[str, str]], Mapping[str, Any]]


@dataclass
class DispatchConfig:
    registry_path: Path
    runtime_root: Path
    goal_ids: list[str]
    project: Path | None = None
    tick_seconds: float = 60.0
    poll_seconds: float = 3.0
    event_debounce_seconds: float = 5.0
    max_global: int = 4
    turn_timeout_seconds: float = 3600.0
    backoff_base_seconds: float = 60.0
    backoff_cap_seconds: float = 6 * 3600.0
    long_cooldown_seconds: float = 3600.0
    auth_cooldown_seconds: float = 300.0
    workspace_failure_cooldown_seconds: float = 300.0
    no_global_sync: bool = False
    loopx_argv: tuple[str, ...] = DEFAULT_LOOPX_ARGV
    environ: Mapping[str, str] | None = None
    extra_turn_args: tuple[str, ...] = field(default_factory=tuple)
    # Turn validator for work whose todo declares no validation command.
    default_validation_argv: tuple[str, ...] | None = None


def _now() -> float:
    return time.time()


def _agent_key(goal_id: str, agent_id: str) -> str:
    return f"{goal_id}/{agent_id}"


def _todo_agent_key(goal_id: str, todo_id: str, agent_id: str) -> str:
    return f"{goal_id}/{todo_id}@{agent_id}"


def _retry_key(goal_id: str, agent_id: str, todo_id: Any) -> str:
    """Crash-retry identity key: per todo and agent, else per agent."""

    return _todo_agent_key(goal_id, str(todo_id), agent_id) if todo_id else _agent_key(goal_id, agent_id)


class Dispatcher:
    def __init__(
        self,
        config: DispatchConfig,
        *,
        should_run: ShouldRun | None = None,
        preflight: Preflight | None = None,
        clock: Callable[[], float] = _now,
    ) -> None:
        self.config = config
        self.runtime_root = Path(config.runtime_root).expanduser()
        # Absolute once: Turns and their validators run in todo worktrees, where
        # the default relative registry (.loopx/registry.json) does not resolve.
        self.registry_path = Path(config.registry_path).expanduser().resolve()
        self.environ = dict(os.environ if config.environ is None else config.environ)
        self._should_run = should_run or self._loopx_should_run
        self._preflight = preflight or _default_preflight
        self.clock = clock
        self.state = load_state(self.runtime_root)
        self.children: dict[str, subprocess.Popen[bytes]] = {}
        self._fingerprints: dict[str, str] = {}

    # ------------------------------------------------------------------
    # LoopX seams
    # ------------------------------------------------------------------

    def _loopx_should_run(self, goal_id: str, agent_id: str) -> Mapping[str, Any]:
        from ..quota import build_quota_should_run
        from ..status import AUTONOMOUS_REPLAN_PERIODIC_LOOKBACK, collect_status

        goal = self._goal(goal_id) or {}
        scan_root = self._goal_project(goal)
        status = collect_status(
            registry_path=self.registry_path,
            runtime_root_override=str(self.runtime_root),
            scan_roots=[scan_root],
            limit=AUTONOMOUS_REPLAN_PERIODIC_LOOKBACK,
            goal_id=goal_id,
            agent_lane_id=agent_id,
        )
        return build_quota_should_run(
            status, goal_id=goal_id, agent_id=agent_id, runtime_root=self.runtime_root
        )

    def _goal(self, goal_id: str) -> dict[str, Any] | None:
        from ..agent_registry import load_goal_from_registry

        return load_goal_from_registry(self.registry_path, goal_id)

    def _goal_project(self, goal: Mapping[str, Any]) -> Path:
        if self.config.project is not None:
            return Path(self.config.project).expanduser()
        repo = goal.get("repo")
        if repo:
            return Path(str(repo)).expanduser()
        return self.registry_path.parent

    def _agents_for_goal(self, goal: Mapping[str, Any]) -> list[tuple[str, str | None]]:
        from ..agent_registry import agent_roles_for_goal, registered_agent_ids_for_goal

        roles = agent_roles_for_goal(dict(goal))
        agents = registered_agent_ids_for_goal(dict(goal))
        order = {policy.ROLE_ORCHESTRATOR: 0, policy.ROLE_ACCEPTOR: 1, policy.ROLE_DEVELOPER: 2}
        return sorted(
            ((agent, roles.get(agent)) for agent in agents),
            key=lambda item: (order.get(item[1] or "", 3), item[0]),
        )

    # ------------------------------------------------------------------
    # event watching
    # ------------------------------------------------------------------

    def goal_fingerprint(self, goal_id: str) -> str:
        """A cheap digest of the files a goal's state changes touch."""

        entries: list[Any] = []
        paths: list[Path] = [self.registry_path]
        try:
            from ..control_plane.todos.path_resolution import resolve_todo_state_path

            _project, state_file = resolve_todo_state_path(
                registry_path=self.registry_path, goal_id=goal_id, require_existing=False
            )
            paths.append(state_file)
        except Exception:  # noqa: BLE001 - a missing goal simply has no state file
            pass
        goal_dir = self.runtime_root / "goals" / goal_id
        paths.append(goal_dir)
        try:
            children = sorted(goal_dir.iterdir())
        except OSError:
            children = []
        for child in children:
            if child.name in {"workspaces", "reviews"}:
                continue
            paths.append(child)
            if child.name == "turns" and child.is_dir():
                try:
                    newest = max((item.stat().st_mtime_ns for item in child.iterdir()), default=0)
                except OSError:
                    newest = 0
                entries.append(("turns_newest", newest))
        for path in paths:
            try:
                stat = path.stat()
                entries.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                entries.append((str(path), None))
        return hashlib.sha256(json.dumps(entries, default=str).encode("utf-8")).hexdigest()

    def changed_goals(self) -> list[str]:
        changed = []
        for goal_id in self.config.goal_ids:
            fingerprint = self.goal_fingerprint(goal_id)
            if self._fingerprints.get(goal_id) != fingerprint:
                changed.append(goal_id)
            self._fingerprints[goal_id] = fingerprint
        return changed

    # ------------------------------------------------------------------
    # reconcile
    # ------------------------------------------------------------------

    def reconcile(self, *, trigger: str = "tick") -> dict[str, Any]:
        now = self.clock()
        report: dict[str, Any] = {
            "schema_version": DISPATCH_PASS_SCHEMA_VERSION,
            "trigger": trigger,
            "at": now,
            "reaped": self.reap(),
            "launched": [],
            "skipped": [],
            "gates_opened": [],
            "errors": [],
        }
        self._expire_cooldowns(now)
        for goal_id in self.config.goal_ids:
            try:
                self._reconcile_goal(goal_id, report)
            except Exception as exc:  # noqa: BLE001 - one goal never stops the others
                report["errors"].append({"goal_id": goal_id, "error": f"{type(exc).__name__}: {exc}"[:400]})
        self.state["last_pass"] = {
            "at": now,
            "trigger": trigger,
            "launched": len(report["launched"]),
            "errors": len(report["errors"]),
        }
        save_state(self.runtime_root, self.state)
        return report

    def _running(self) -> dict[str, dict[str, Any]]:
        return self.state.setdefault("runs", {})

    def _reconcile_goal(self, goal_id: str, report: dict[str, Any]) -> None:
        from ..agent_config import AgentConfigError, resolve_agent

        goal = self._goal(goal_id)
        if goal is None:
            report["errors"].append({"goal_id": goal_id, "error": "goal_not_registered"})
            return
        project = self._goal_project(goal)
        role_v1 = policy.goal_is_role_v1(goal)
        closed = self._goal_complete_hold(goal_id, goal, report) if role_v1 else None
        if closed is not None:
            # Decision 42: the owner closed the goal at its goal_complete gate;
            # it is not considered again until it is resumed.
            for agent_id, _registry_role in self._agents_for_goal(goal):
                report["skipped"].append({"goal_id": goal_id, "agent_id": agent_id, **closed})
            return
        self._resume_plan_dependents(goal_id, report)
        try:  # G9: the 80% alert never blocks; 100% opens the budget gate (decision 41).
            alert_usage_budget(registry_path=self.registry_path, runtime_root=self.runtime_root,
                               goal_id=goal_id, state=self.state, report=report, now=self.clock())
        except Exception as exc:  # noqa: BLE001 - report and retry next pass
            report["errors"].append({"goal_id": goal_id, "error": f"usage_budget: {exc}"[:400]})
        if role_v1:
            self._request_push_when_merged(goal_id, report)
            self._open_goal_complete_when_done(goal_id, goal, report)
        budget_held = self._budget_hold(goal_id, goal, report)
        for agent_id, registry_role in self._agents_for_goal(goal):
            if budget_held is not None:
                # Decision 41: no new Turn of this goal for any role while its
                # budget gate is open (or the owner stopped it there); running
                # Turns are left to finish.
                report["skipped"].append({"goal_id": goal_id, "agent_id": agent_id, **budget_held})
                continue
            skip = lambda reason, **extra: report["skipped"].append(  # noqa: E731
                {"goal_id": goal_id, "agent_id": agent_id, "reason": reason, **extra}
            )
            try:
                definition = resolve_agent(
                    agent_id, project, runtime_root=self.runtime_root, role=registry_role,
                )
            except AgentConfigError as exc:
                skip("agent_config_invalid", issues=list(exc.issues)[:5])
                continue
            if not definition.enabled:
                skip("agent_disabled")
                continue
            role = registry_role or definition.role
            limit = policy.slot_limit(role, definition.max_concurrency)
            # run-once fences role_v1 developer/acceptor Turns per todo, so the
            # agent fills its slots with different todos of this goal; every
            # other Turn keeps one lane per agent and goal (decision 32).
            todo_lane = policy.todo_scoped_lane(role_v1=role_v1, registry_role=registry_role)
            self.state.setdefault("agent_slots", {})[agent_id] = {
                "role": role,
                "max": limit,
                "provider": definition.provider,
            }
            now = self.clock()
            agent_cooldown = self.state.get("agent_cooldowns", {}).get(agent_id)
            if agent_cooldown and agent_cooldown.get("until", 0) > now:
                skip("agent_cooldown", until=agent_cooldown.get("until"))
                continue
            provider_cooldown = self.state.get("provider_cooldowns", {}).get(definition.provider)
            if provider_cooldown and provider_cooldown.get("until", 0) > now:
                skip("provider_cooldown", provider=definition.provider, until=provider_cooldown.get("until"))
                continue
            preflight_done = False
            bookkeeping_closed = 0
            while True:
                runs = self._running()
                if len(runs) >= self.config.max_global:
                    skip("global_cap")
                    break
                agent_runs = [run for run in runs.values() if run.get("agent_id") == agent_id]
                if len(agent_runs) >= limit:
                    skip("slots_full", running=len(agent_runs), max=limit)
                    break
                if not todo_lane and any(run.get("goal_id") == goal_id for run in agent_runs):
                    # The orchestrator and peer_v1 lanes admit one in-flight Turn
                    # per agent and goal (run-once: turn_lane_in_flight).
                    skip("turn_lane_in_flight")
                    break
                if role == policy.ROLE_ORCHESTRATOR and any(
                    run.get("goal_id") == goal_id and run.get("role") == policy.ROLE_ORCHESTRATOR
                    for run in runs.values()
                ):
                    skip("orchestrator_running")
                    break
                resume_todo = settlement_retry.pending_resume_todo(
                    self.state.get("retry_turns") or {}, goal_id=goal_id, agent_id=agent_id,
                    excluded={
                        str(run.get("todo_id")) for run in runs.values()
                        if run.get("goal_id") == goal_id and run.get("todo_id")
                    },
                )
                if resume_todo and self._todo_cooling(goal_id, resume_todo, agent_id, now):
                    skip("todo_cooldown", todo_id=resume_todo)
                    break
                try:
                    payload = (
                        settlement_retry.resume_payload(resume_todo)
                        if resume_todo
                        else self._should_run(goal_id, agent_id)
                    )
                except Exception as exc:  # noqa: BLE001 - LoopX decision failure is reported, not fatal
                    report["errors"].append(
                        {"goal_id": goal_id, "agent_id": agent_id, "error": f"should_run: {exc}"[:400]}
                    )
                    break
                baseline_key = _agent_key(goal_id, agent_id)
                state_changed = (
                    self.state.get("orchestrator_baselines", {}).get(baseline_key)
                    != self.goal_fingerprint(goal_id)
                )
                decision = policy.decide_turn(payload, role=role, state_changed=state_changed)
                if not decision["launch"] and role == policy.ROLE_ORCHESTRATOR:
                    self._open_orchestrator_action(goal_id, agent_id, decision, report)
                # Decision 40: the acceptor does not review a todo while a plan
                # card changing its acceptance criteria awaits the user.
                criteria_held = (
                    set(criteria_change_pending_todo_ids(self.runtime_root, goal_id))
                    if role == policy.ROLE_ACCEPTOR else set()
                )
                if not decision["launch"]:
                    skip(
                        decision["reason"],
                        **{key: value for key, value in decision.items() if key not in {"launch", "reason"}},
                        **({"criteria_change_pending_todo_ids": sorted(criteria_held)} if criteria_held else {}),
                    )
                    break
                todo_id = decision.get("todo_id")
                in_flight = {
                    run.get("todo_id")
                    for run in runs.values()
                    if run.get("goal_id") == goal_id and run.get("todo_id")
                }
                cooling = {
                    candidate
                    for candidate in {
                        key.split("/", 1)[1].split("@", 1)[0]
                        for key in (self.state.get("todo_cooldowns") or {})
                        if key.startswith(f"{goal_id}/")
                    }
                    if self._todo_cooling(goal_id, candidate, agent_id, now)
                }
                # G12: while an acceptor-blocked gate is open for a todo, its
                # acceptor is not relaunched on it.
                review_blocked = (
                    blocked_review_todo_ids(self.registry_path, self.runtime_root, goal_id)
                    if role == policy.ROLE_ACCEPTOR else set()
                )
                held = review_blocked | criteria_held
                # A deferred todo can still be offered by upstream should-run
                # (its single ``resume_when`` is met) while a decision-37 plan
                # dependency waits. A pinned todo-lane Turn on it is refused by
                # run-once without a host call, so launching it only grows the
                # todo's backoff (E2E pilot v1). Fill the slot with an
                # executable todo instead, or skip.
                deferred = (
                    {str(todo_id)}
                    if todo_lane and todo_id and decision.get("todo_status") == "deferred"
                    else set()
                )
                held |= deferred
                if todo_id and (todo_id in in_flight | cooling | held) and role != policy.ROLE_ORCHESTRATOR:
                    # Fill a free slot of this agent with its next executable todo.
                    alternate = policy.alternate_todo(payload, exclude=in_flight | cooling | held)
                    if alternate:
                        decision = {**decision, "todo_id": alternate, "reason": "alternate_todo", "pinned": True}
                        todo_id = alternate
                if todo_id and todo_id in in_flight:
                    skip("todo_in_flight", todo_id=todo_id)
                    break
                if (
                    role == policy.ROLE_ORCHESTRATOR and todo_id
                    and self._action_todo_failures(goal_id, str(todo_id)) >= ORCHESTRATOR_ACTION_FAILED_TURN_LIMIT
                ):
                    self._retire_action_todo(goal_id, agent_id, str(todo_id), report)
                    skip(ORCHESTRATOR_ACTION_RETIRED_REASON, todo_id=todo_id)
                    break
                if role == policy.ROLE_ORCHESTRATOR and todo_id:
                    if bookkeeping_closed >= ORCHESTRATOR_BOOKKEEPING_PASS_LIMIT:
                        # Bounded work per pass; the next pass continues.
                        skip(ORCHESTRATOR_BOOKKEEPING_LIMIT_REASON, todo_id=todo_id)
                        break
                    if self._close_bookkeeping_todo(goal_id, str(todo_id), report):
                        # Decision 43: mechanical bookkeeping closed without a
                        # model Turn; ask LoopX again for real orchestrator work.
                        bookkeeping_closed += 1
                        continue
                if todo_id and todo_id in review_blocked:
                    skip("review_blocked_gate_open", todo_id=todo_id)
                    break
                if todo_id and todo_id in criteria_held:
                    skip(CRITERIA_CHANGE_PENDING_REASON, todo_id=todo_id)
                    break
                if todo_id and todo_id in deferred:
                    waits = read_dependency_wait_snapshot(self.runtime_root, goal_id).get(str(todo_id))
                    skip(
                        policy.SELECTED_TODO_DEFERRED_REASON,
                        todo_id=todo_id,
                        **({"dependency_wait": "; ".join(waits)[:400]} if waits else {}),
                    )
                    break
                if todo_id and self._todo_cooling(goal_id, str(todo_id), agent_id, now):
                    skip("todo_cooldown", todo_id=todo_id)
                    break
                if not preflight_done:
                    record = self._preflight(definition, self.environ)
                    preflight_done = True
                    if not record.get("ok"):
                        self._agent_unavailable(goal_id, agent_id, str(record.get("status") or "unknown"), report)
                        break
                    self._agent_available(goal_id, agent_id)
                if todo_lane and todo_id:
                    # Pin every todo-lane Turn: run-once must work exactly the
                    # todo this pass reserved, not re-select one of its own.
                    decision = {**decision, "pinned": True}
                try:
                    launched = self._launch(goal, definition, role, decision, project, report)
                except Exception as exc:  # noqa: BLE001 - one agent's launch never stops the others
                    report["errors"].append(
                        {"goal_id": goal_id, "agent_id": agent_id, "error": f"launch: {type(exc).__name__}: {exc}"[:400]}
                    )
                    break
                if launched is None or role == policy.ROLE_ORCHESTRATOR:
                    break

    # ------------------------------------------------------------------
    # launch
    # ------------------------------------------------------------------

    def _todo_record(self, goal_id: str, todo_id: str) -> dict[str, Any] | None:
        from ..todos import list_goal_todos

        listed = list_goal_todos(
            registry_path=self.registry_path,
            goal_id=goal_id,
            todo_id=todo_id,
            runtime_root_arg=str(self.runtime_root),
        )
        for item in listed.get("todos") or []:
            if isinstance(item, Mapping) and item.get("todo_id") == todo_id:
                return dict(item)
        return None

    def _journaled_turn_key(self, goal_id: str, turn_instance_id: str) -> str | None:
        """The turn key run-once journaled for this Turn identity, if any."""

        turns = self.runtime_root / "goals" / goal_id / "turns"
        try:
            candidates = sorted(turns.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        except OSError:
            return None
        for path in candidates[:200]:
            try:
                journal = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            plan = journal.get("plan") if isinstance(journal, Mapping) else None
            transaction = plan.get("transaction") if isinstance(plan, Mapping) else None
            if isinstance(transaction, Mapping) and transaction.get("turn_instance_id") == turn_instance_id:
                key = journal.get("turn_key") or transaction.get("turn_key")
                return str(key) if key else None
        return None

    def _todo_validation_argv(self, goal_id: str, todo: Mapping[str, Any]) -> tuple[list[str] | None, int | None]:
        """The todo's declared validation command as a Turn validator argv.

        run-once needs an independent validator for material results; the
        todo's own (orchestrator-written) validation command is that check.
        """

        if todo.get("completion_validation_required") is not True:
            return None, None
        import shlex

        from ..control_plane.todos.completion_validation import (
            resolve_private_completion_validation_declaration,
        )
        from ..control_plane.todos.path_resolution import resolve_todo_state_path

        _project, state_file = resolve_todo_state_path(
            registry_path=self.registry_path, goal_id=goal_id, require_existing=False
        )
        declaration = resolve_private_completion_validation_declaration(
            canonical_todo=todo,
            state_file=state_file,
            runtime_root=self.runtime_root,
            registry_path=self.registry_path,
            goal_id=goal_id,
            todo_id=str(todo.get("todo_id")),
            role=str(todo.get("role") or "agent"),
            persist_if_resolved=False,
        ) or {}
        argv = declaration.get("validation_command_argv")
        if not argv and declaration.get("validation_command"):
            argv = shlex.split(str(declaration["validation_command"]))
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
            return None, None
        timeout = declaration.get("validation_timeout_seconds")
        return argv, int(timeout) if isinstance(timeout, int) else None

    def _prepare_workspace(
        self,
        goal: Mapping[str, Any],
        todo_id: str,
        todo: Mapping[str, Any],
        role: str | None,
        report: dict[str, Any],
        *,
        attempt: str,
    ) -> tuple[bool, Path | None, dict[str, str], dict[str, Any] | None]:
        """Return ``(ok, cwd, repo_paths, review_checkout)``; cwd is None when no workspace applies.

        An acceptor reviewing a delivered todo gets a detached review checkout
        of the delivered commit (G12), never the developer's todo worktree.
        """

        if role not in {policy.ROLE_DEVELOPER, policy.ROLE_ACCEPTOR, None}:
            return True, None, {}, None
        goal_id = str(goal.get("id"))
        repos = [str(item) for item in (todo.get("task_repositories") or []) if item]
        if not repos:
            return True, None, {}, None
        from ..workspace import git_workspace

        if is_review_turn(role, todo):
            prepared = prepare_acceptor_review(goal, todo, self.runtime_root, attempt=attempt)
        else:
            prepared = git_workspace.prepare(dict(goal), todo_id, repos, self.runtime_root)
        if not prepared.get("ok"):
            until = self.clock() + self.config.workspace_failure_cooldown_seconds
            self.state.setdefault("todo_cooldowns", {})[f"{goal_id}/{todo_id}"] = {
                "until": until,
                "failures": 0,
                "reason": "workspace_prepare_failed",
            }
            report["errors"].append(
                {
                    "goal_id": goal_id,
                    "todo_id": todo_id,
                    "error": "review_checkout_failed" if is_review_turn(role, todo) else "workspace_prepare_failed",
                    "repos": [
                        {"name": item.get("name"), "error_code": item.get("error_code")}
                        for item in prepared.get("repos") or []
                        if not item.get("ok")
                    ]
                    or prepared.get("error_code"),
                }
            )
            return False, None, {}, None
        paths = {str(name): str(path) for name, path in (prepared.get("paths") or {}).items()}
        review = review_run_record(prepared) if is_review_turn(role, todo) else None
        if len(paths) == 1:
            return True, Path(next(iter(paths.values()))), paths, review
        return True, Path(str(prepared["workspace_root"])), paths, review

    def _budget_hold(self, goal_id: str, goal: Mapping[str, Any], report: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return budget_hold(self.registry_path, self.runtime_root, goal_id, goal)
        except Exception as exc:  # noqa: BLE001 - fail closed: only goals with budget gates get here
            report["errors"].append({"goal_id": goal_id, "error": f"usage_budget_hold: {exc}"[:400]})
            return {"reason": "budget_hold_unreadable"}

    def _request_push_when_merged(self, goal_id: str, report: dict[str, Any]) -> None:
        """Open the goal's push_request user gate once all its work is merged (G8).

        A system gate like re-login (decision 17): LoopX opens it through the
        push-request API, which keeps it to one open gate per goal and does
        not reopen a declined push until new merges arrive.
        """

        from ..push_requests import PUSH_REASON_ALL_MERGED, request_push

        try:
            result = request_push(
                registry_path=self.registry_path, goal_id=goal_id, runtime_root_arg=str(self.runtime_root),
                reason=PUSH_REASON_ALL_MERGED, requested_by="dispatcher", require_all_merged=True,
            )
        except Exception as exc:  # noqa: BLE001 - report and retry next pass
            report["errors"].append({"goal_id": goal_id, "error": f"push_request: {exc}"[:400]})
            return
        if result.get("opened"):
            report["gates_opened"].append(
                {"goal_id": goal_id, "key": "push_request", "todo_id": result.get("gate_todo_id")}
            )

    def _open_goal_complete_when_done(self, goal_id: str, goal: Mapping[str, Any], report: dict[str, Any]) -> None:
        """Open the goal's goal_complete gate once its work is merged and pushed (decision 42).

        Deterministic, with no model Turn: the gate's content (merges, push
        results, usage) is computed here. It runs after the push request, so
        a pending push gate opens first and this one waits for it.
        """

        from ..goal_complete_gate import open_goal_complete_gate

        try:
            result = open_goal_complete_gate(
                registry_path=self.registry_path, runtime_root=self.runtime_root, goal_id=goal_id, goal=goal,
                runtime_root_arg=str(self.runtime_root),
            )
        except Exception as exc:  # noqa: BLE001 - report and retry next pass
            report["errors"].append({"goal_id": goal_id, "error": f"goal_complete: {exc}"[:400]})
            return
        if result.get("opened"):
            report["gates_opened"].append(
                {"goal_id": goal_id, "key": "goal_complete", "todo_id": result.get("gate_todo_id")}
            )

    def _goal_complete_hold(
        self, goal_id: str, goal: Mapping[str, Any], report: dict[str, Any],
    ) -> dict[str, Any] | None:
        from ..goal_complete_gate import goal_complete_hold

        try:
            return goal_complete_hold(self.runtime_root, goal_id, goal)
        except Exception as exc:  # noqa: BLE001 - a stopped goal is not launched anyway
            report["errors"].append({"goal_id": goal_id, "error": f"goal_complete_hold: {exc}"[:400]})
            return None

    def _resume_plan_dependents(self, goal_id: str, report: dict[str, Any]) -> None:
        """Reopen plan todos whose dependencies are done, through the Todo API.

        Like opening a gate this is an ordinary Todo write, not dispatcher
        state; it covers completions that did not pass through an accept
        verdict (for example todos that need no acceptance).
        """

        from ..plan_cards import resume_ready_plan_todos

        refreshes: list[dict[str, Any]] = []
        try:
            resumed = resume_ready_plan_todos(
                registry_path=self.registry_path, goal_id=goal_id, runtime_root=self.runtime_root,
                runtime_root_arg=str(self.runtime_root), branch_refreshes=refreshes,
            )
        except (OSError, ValueError) as exc:
            report["errors"].append({"goal_id": goal_id, "error": f"plan_resume: {exc}"[:300]})
            return
        if resumed:
            report.setdefault("resumed", []).extend({"goal_id": goal_id, "todo_id": todo_id} for todo_id in resumed)
        # Pilot v1 N10: untouched todo branches fast-forwarded on release.
        for refresh in refreshes:
            report.setdefault("todo_branches_refreshed", []).append(
                {"goal_id": goal_id, "todo_id": refresh.get("todo_id"), "repos": list(refresh.get("refreshed") or [])}
            )

    def _open_orchestrator_action(
        self, goal_id: str, agent_id: str, decision: Mapping[str, Any], report: dict[str, Any],
    ) -> None:
        """Open one orchestrator todo for an action no todo carries (orchestrator_actions)."""

        gate_ids = gates_awaiting_orchestrator(self.runtime_root, goal_id)
        if gate_ids:
            # Only open gates count; the cheap index read comes first.
            try:
                open_gates = self._open_user_todo_ids(goal_id)
            except Exception:  # noqa: BLE001 - without a readback we must not duplicate todos
                return
            gate_ids = [gate for gate in gate_ids if gate in open_gates]
        effective_action = None
        if decision.get("reason") == "orchestrator_action_without_todo":
            effective_action = str(decision.get("detail") or "").removeprefix("effective_action=") or None
        if not gate_ids and not effective_action:
            # Nothing is pending any more: a later recurrence starts afresh.
            self.state.get("orchestrator_actions", {}).pop(_agent_key(goal_id, agent_id), None)
            return
        from ..todos import add_goal_todo, list_goal_todos

        try:
            rows = list_goal_todos(
                registry_path=self.registry_path, goal_id=goal_id, role="agent",
                runtime_root_arg=str(self.runtime_root),
            ).get("todos") or []
        except Exception:  # noqa: BLE001 - without a readback we must not duplicate todos
            return
        if live_orchestrator_todo(rows, agent_id):
            return
        key = _agent_key(goal_id, agent_id)
        actions = self.state.setdefault("orchestrator_actions", {})
        subject = ",".join(gate_ids) if gate_ids else f"effective_action={effective_action}"
        previous = actions.get(key) or {}
        repeats = int(previous.get("count") or 0) if previous.get("subject") == subject else 0
        if repeats >= ORCHESTRATOR_ACTION_REPEAT_LIMIT:
            # The orchestrator's Turns did not clear it: stop spending Turns and
            # say so, instead of looping or stalling silently.
            self._open_gate(
                goal_id, key=f"orchestrator-action:{agent_id}",
                text=(f"Orchestrator {agent_id} did not settle {subject} after {repeats} action todos "
                      "(dispatcher). Inspect the goal, then close this gate."),
                blocks_agent=agent_id, report=report,
            )
            return
        try:
            added = add_goal_todo(
                registry_path=self.registry_path, goal_id=goal_id, role="agent",
                runtime_root_arg=str(self.runtime_root),
                text=action_todo_text(effective_action, gate_ids),
                task_class="advancement_task", action_kind="replan", claimed_by=agent_id,
                role_contract={"required_role": policy.ROLE_ORCHESTRATOR, "requires_acceptance": False},
                note="Opened by the LoopX dispatcher.",
            )
        except Exception as exc:  # noqa: BLE001 - report and retry next pass
            report["errors"].append({"goal_id": goal_id, "error": f"orchestrator_action: {exc}"[:400]})
            return
        todo_id = str(added.get("todo_id") or "")
        actions[key] = {"subject": subject, "count": repeats + 1, "todo_id": todo_id, "opened_at": self.clock()}
        report.setdefault("orchestrator_todos_opened", []).append(
            {"goal_id": goal_id, "agent_id": agent_id, "todo_id": todo_id, "subject": subject}
        )

    def _close_bookkeeping_todo(self, goal_id: str, todo_id: str, report: dict[str, Any]) -> bool:
        """Close an orchestrator todo that is pure bookkeeping instead of launching a Turn (decision 43)."""

        from ..orchestrator_bookkeeping import mechanical_orchestrator_closeout

        todo = self._todo_record(goal_id, todo_id)
        if not todo:
            return False
        try:
            outcome = mechanical_orchestrator_closeout(
                registry_path=self.registry_path, runtime_root=self.runtime_root, goal_id=goal_id,
                todo=todo, runtime_root_arg=str(self.runtime_root),
            )
        except Exception as exc:  # noqa: BLE001 - the Turn runs as before
            report["errors"].append({"goal_id": goal_id, "todo_id": todo_id, "error": f"bookkeeping: {exc}"[:400]})
            return False
        if outcome is None:
            return False
        if not outcome.get("closed"):
            report["errors"].append(
                {"goal_id": goal_id, "todo_id": todo_id, "error": f"bookkeeping: {outcome.get('error')}"[:400]}
            )
            return False
        report.setdefault("orchestrator_todos_closed", []).append(
            {"goal_id": goal_id, "todo_id": todo_id, "kind": outcome.get("kind"), "evidence": outcome.get("evidence")}
        )
        return True

    def _action_todo_failures(self, goal_id: str, todo_id: str) -> int:
        return int((self.state.get("orchestrator_action_failures") or {}).get(f"{goal_id}/{todo_id}") or 0)

    def _retire_action_todo(
        self, goal_id: str, agent_id: str, todo_id: str, report: dict[str, Any],
    ) -> None:
        """Close an action todo whose Turns keep failing and tell the user once.

        Its validator can never pass when the orchestrator has nothing left to
        do (E2E pilot v1: a finished goal), so relaunching it spent a Turn per
        backoff expiry without end. The dispatcher opened the todo; it closes
        it through the ordinary supersede transition, attributed to the claim
        owner, and opens the repeat-limit gate, which holds the orchestrator's
        lane until the user closes it. Every relaunch of this loop therefore
        needs a user decision instead of spending Turns on its own.
        """

        from ..todos import supersede_goal_todo

        todo = self._todo_record(goal_id, todo_id) or {}
        subject = action_subject(todo) or todo_id
        failures = self._action_todo_failures(goal_id, todo_id)
        try:
            supersede_goal_todo(
                registry_path=self.registry_path, goal_id=goal_id, runtime_root_arg=str(self.runtime_root),
                todo_id=todo_id, agent_id=str(todo.get("claimed_by") or agent_id),
                reason=f"dispatcher: the orchestrator's Turns failed {failures} times on this action todo",
            )
        except Exception as exc:  # noqa: BLE001 - report; the todo stays held by its failure count
            report["errors"].append({"goal_id": goal_id, "todo_id": todo_id, "error": f"retire_action: {exc}"[:400]})
            return
        self.state.setdefault("orchestrator_action_failures", {}).pop(f"{goal_id}/{todo_id}", None)
        report.setdefault("orchestrator_todos_retired", []).append(
            {"goal_id": goal_id, "agent_id": agent_id, "todo_id": todo_id, "subject": subject, "failed_turns": failures}
        )
        self._open_gate(
            goal_id, key=f"orchestrator-action:{agent_id}",
            text=(f"Orchestrator {agent_id} did not settle {subject}: its Turns on action todo {todo_id} "
                  f"failed {failures} times, so the dispatcher closed that todo. Inspect the goal (a stale "
                  "Next Action can raise this), then close this gate."),
            blocks_agent=agent_id, report=report,
        )

    def _todo_cooling(self, goal_id: str, todo_id: str, agent_id: str, now: float) -> bool:
        """A todo-wide cooldown (workspace prepare) or this agent's failure backoff."""

        bucket = self.state.get("todo_cooldowns") or {}
        for key in (f"{goal_id}/{todo_id}", _todo_agent_key(goal_id, todo_id, agent_id)):
            if (bucket.get(key) or {}).get("until", 0) > now:
                return True
        return False

    def _loopx_command(self) -> str:
        """The CLI prefix a launched agent must use to reach this state home."""

        return shlex.join(
            [
                *self.config.loopx_argv,
                "--registry",
                str(self.registry_path),
                "--runtime-root",
                str(self.runtime_root),
            ]
        )

    def _plan_applied_validator(self, goal_id: str) -> list[str]:
        return [
            *self.config.loopx_argv,
            "--registry",
            str(self.registry_path),
            "--runtime-root",
            str(self.runtime_root),
            "plan",
            "list",
            "--goal-id",
            goal_id,
            "--require-status",
            "applied",
        ]

    def _launch(
        self,
        goal: Mapping[str, Any],
        definition: Any,
        role: str | None,
        decision: Mapping[str, Any],
        project: Path,
        report: dict[str, Any],
    ) -> str | None:
        from ..agent_config import AgentConfigError, provider_launch_env, turn_run_once_host_arguments
        from ..turn_identity import mint_turn_instance_id

        goal_id = str(goal.get("id"))
        agent_id = definition.id
        todo_id = decision.get("todo_id")
        cwd: Path | None = None
        repo_paths: dict[str, str] = {}
        validation_argv: list[str] | None = None
        validation_timeout: int | None = None
        todo: Mapping[str, Any] = {}
        review_checkout: dict[str, Any] | None = None
        run_id = f"{int(self.clock())}-{uuid.uuid4().hex[:8]}"
        if todo_id and decision.get("todo_is_agent_todo", True):
            todo = self._todo_record(goal_id, str(todo_id)) or {}
            ok, cwd, repo_paths, review_checkout = self._prepare_workspace(
                goal, str(todo_id), todo, role, report, attempt=run_id,
            )
            if not ok:
                return None
            try:
                validation_argv, validation_timeout = self._todo_validation_argv(goal_id, todo)
            except (OSError, ValueError) as exc:
                report["errors"].append(
                    {"goal_id": goal_id, "todo_id": todo_id, "error": f"validation_declaration: {exc}"[:300]}
                )
        run_project = cwd or project
        run_project.mkdir(parents=True, exist_ok=True)

        retries = self.state.setdefault("retry_turns", {})
        retry_key = _retry_key(goal_id, agent_id, todo_id)
        # Crash identities are kept per todo, so two Turns of one agent in one
        # goal never reuse or drop each other's identity. The agent key is the
        # todo-less form, and the state layout before per-todo lanes.
        retry = retries.get(retry_key)
        legacy = retries.get(_agent_key(goal_id, agent_id)) if todo_id else None
        if not retry and legacy and legacy.get("todo_id") == todo_id:
            retry = legacy
        crashes = 0
        settlement_retries = 0
        if retry and retry.get("todo_id") == todo_id:
            turn_instance_id = str(retry["turn_instance_id"])
            crashes = int(retry.get("crashes") or 0)
            settlement_retries = int(retry.get("settlement_retries") or 0)
            reused = True
            if retry.get("settlement") and retry.get("project") and Path(str(retry["project"])).is_dir():
                # The settlement is bound to the delivery workspace the host
                # ran in (the quota spend checks it), so resume from there.
                # A removed workspace (an acceptor's review checkout) resumes
                # from the goal project; the retry limit bounds that case.
                run_project = Path(str(retry["project"]))
        else:
            turn_instance_id = mint_turn_instance_id(prefix="dispatch")
            reused = False
        retries.pop(retry_key, None)
        if legacy is not None and legacy.get("todo_id") == todo_id:
            # Only the legacy identity this launch took over; another todo's
            # crash identity under the agent key stays for its own relaunch.
            retries.pop(_agent_key(goal_id, agent_id), None)

        run_dir = dispatch_dir(self.runtime_root) / "runs"
        run_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = run_dir / f"{run_id}.out.json"
        stderr_path = run_dir / f"{run_id}.err.log"

        host_args = turn_run_once_host_arguments(definition)
        env = dict(self.environ)
        provider = definition.provider_config
        if definition.runtime == "claude-code":
            prompt_path = compose_system_prompt(
                destination=run_dir / f"{run_id}.system-prompt.md",
                base_prompt_file=definition.system_prompt_file,
                addendum=dispatch_prompt_addendum(
                    goal_id=goal_id,
                    agent_id=agent_id,
                    role=role,
                    todo_id=str(todo_id) if todo_id else None,
                    workspace_repos=repo_paths,
                    review_checkout=review_checkout is not None,
                    awaiting_gates=(
                        gates_awaiting_orchestrator(self.runtime_root, goal_id)
                        if role == "orchestrator" else None
                    ),
                    loopx_command=self._loopx_command(),
                    state_digest=(
                        orchestrator_digest_or_note(
                            registry_path=self.registry_path, runtime_root=self.runtime_root, goal_id=goal_id,
                            todo_id=str(todo_id) if todo_id else None, launch_reason=decision.get("reason"),
                        )
                        if role == policy.ROLE_ORCHESTRATOR else None
                    ),
                ),
            )
            host_args = replace_system_prompt_argument(host_args, prompt_path)
            host_args.extend(["--claude-provider", definition.provider])
        elif provider is not None and provider.auth.type != "oauth_cli":
            # codex-cli reads its key from the env var named by env_key; the
            # value only ever lives in the child's environment.
            try:
                env.update(provider_launch_env(provider, environ=self.environ))
            except AgentConfigError:
                self._agent_unavailable(goal_id, agent_id, "missing_credential", report)
                if review_checkout is not None:
                    remove_acceptor_review(goal, review_checkout, self.runtime_root)
                return None

        argv = [
            *self.config.loopx_argv,
            "--registry",
            str(self.registry_path),
            "--runtime-root",
            str(self.runtime_root),
            "--format",
            "json",
            "turn",
            "run-once",
            "--goal-id",
            goal_id,
            "--agent-id",
            agent_id,
            "--project",
            str(run_project),
            *host_args,
            "--timeout-seconds",
            str(int(self.config.turn_timeout_seconds)),
            "--execute",
        ]
        resume_turn_key = self._journaled_turn_key(goal_id, turn_instance_id) if reused else None
        if resume_turn_key:
            # Resume the exact journaled transaction: run-once replays what
            # already settled and continues from the last side-effect-safe phase.
            argv.extend(["--resume-turn-key", resume_turn_key, "--retry-failed-turn"])
        else:
            argv.extend(["--turn-instance-id", turn_instance_id])
            if todo_id and (cwd is not None or decision.get("pinned")):
                argv.extend(["--todo-id", str(todo_id)])
        if not validation_argv and role == "orchestrator" and todo_id and _is_plan_todo(todo):
            # The intake planning todo is done once its plan card is applied;
            # that is the orchestrator Turn's independent validator.
            validation_argv = self._plan_applied_validator(goal_id)
        if not validation_argv and role == "orchestrator" and todo_id and is_orchestrator_action_todo(todo):
            validation_argv = action_validator_argv(
                self.config.loopx_argv[0], registry=str(self.registry_path),
                runtime_root=str(self.runtime_root), goal_id=goal_id, todo=todo,
            )
        if not validation_argv and role == "orchestrator" and todo_id:
            from ..todo_acceptance import escalated_todo_id

            escalated = escalated_todo_id(todo)
            if escalated:
                # An escalation is resolved once the escalated todo is no longer
                # blocked: reopened, reassigned, split or superseded.
                validation_argv = [
                    self.config.loopx_argv[0], "-m", "loopx.dispatch.checks", "todo-not-status",
                    "--registry", str(self.registry_path), "--runtime-root", str(self.runtime_root),
                    "--goal-id", goal_id, "--todo-id", escalated, "--status", "blocked",
                ]
        if not validation_argv and self.config.default_validation_argv:
            validation_argv = list(self.config.default_validation_argv)
        if validation_argv:
            argv.extend(["--validation-command-json", json.dumps(validation_argv)])
            if validation_timeout:
                argv.extend(["--validation-timeout-seconds", str(validation_timeout)])
        if self.config.no_global_sync:
            argv.append("--no-global-sync")
        argv.extend(self.config.extra_turn_args)
        try:
            with open(stdout_path, "wb") as stdout, open(stderr_path, "wb") as stderr:
                child = subprocess.Popen(
                    argv,
                    cwd=str(run_project),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
        except OSError:
            if review_checkout is not None:
                remove_acceptor_review(goal, review_checkout, self.runtime_root)
            raise
        self.children[run_id] = child
        record = {
            "run_id": run_id,
            "goal_id": goal_id,
            "agent_id": agent_id,
            "role": role,
            "provider": definition.provider,
            "runtime": definition.runtime,
            "todo_id": todo_id,
            "turn_instance_id": turn_instance_id,
            "turn_instance_reused": reused,
            "resume_turn_key": resume_turn_key,
            "crashes": crashes,
            "settlement_retries": settlement_retries,
            "reason": decision.get("reason"),
            "pid": child.pid,
            "started_at": self.clock(),
            "project": str(run_project),
            "workspace_repos": sorted(repo_paths),
            **({"orchestrator_action": True}
               if role == policy.ROLE_ORCHESTRATOR and todo_id and is_orchestrator_action_todo(todo) else {}),
            **({"review_checkout": review_checkout} if review_checkout is not None else {}),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
        self._running()[run_id] = record
        save_state(self.runtime_root, self.state)
        report["launched"].append({key: record[key] for key in ("run_id", "goal_id", "agent_id", "role", "todo_id", "turn_instance_id", "turn_instance_reused", "reason")})
        return run_id

    # ------------------------------------------------------------------
    # reap and outcomes
    # ------------------------------------------------------------------

    def reap(self) -> list[dict[str, Any]]:
        finished: list[dict[str, Any]] = []
        for run_id, run in list(self._running().items()):
            child = self.children.get(run_id)
            if child is not None:
                returncode = child.poll()
                if returncode is None:
                    continue
                self.children.pop(run_id, None)
            else:
                # Adopted from an earlier dispatcher process: liveness only.
                if pid_alive(run.get("pid")):
                    continue
                returncode = None
            finished.append(self._settle_run(run_id, run, returncode))
        if finished:
            save_state(self.runtime_root, self.state)
        return finished

    def _settle_run(self, run_id: str, run: dict[str, Any], returncode: int | None) -> dict[str, Any]:
        try:
            stdout_text = Path(run["stdout_path"]).read_text(encoding="utf-8", errors="replace")
        except (OSError, KeyError):
            stdout_text = ""
        outcome = policy.classify_outcome(returncode, stdout_text)
        if returncode is not None and returncode < 0:
            outcome["outcome"] = policy.OUTCOME_CRASHED
        now = self.clock()
        goal_id = str(run.get("goal_id"))
        agent_id = str(run.get("agent_id"))
        provider = str(run.get("provider") or "")
        kind = outcome.get("failure_kind")
        if outcome["outcome"] == policy.OUTCOME_CRASHED:
            # Relaunch with the same Turn identity: run-once settles by
            # turn_instance_id, so a replay never double-settles.
            crashes = int(run.get("crashes") or 0) + 1
            self.state.setdefault("retry_turns", {})[_retry_key(goal_id, agent_id, run.get("todo_id"))] = {
                "turn_instance_id": run.get("turn_instance_id"),
                "todo_id": run.get("todo_id"),
                "crashed_at": now,
                "crashes": crashes,
            }
            if crashes >= 3 and run.get("todo_id"):
                self.state.setdefault("todo_cooldowns", {})[_todo_agent_key(goal_id, run["todo_id"], agent_id)] = {
                    "until": now
                    + policy.backoff_seconds(
                        crashes - 2, base=self.config.backoff_base_seconds, cap=self.config.backoff_cap_seconds
                    ),
                    "failures": 0,
                    "reason": "repeated_crash",
                }
        elif kind in policy.PROVIDER_BACKOFF_FAILURE_KINDS:
            self._provider_backoff(goal_id, agent_id, provider, str(kind))
        elif kind in policy.AUTH_FAILURE_KINDS:
            self._agent_unavailable(goal_id, agent_id, str(kind), None)
        elif outcome["outcome"] == policy.OUTCOME_COMMITTED:
            cooldown = self.state.get("provider_cooldowns", {}).get(provider)
            if cooldown:
                cooldown["failures"] = 0
            if run.get("todo_id"):
                bucket = self.state.setdefault("todo_cooldowns", {})
                bucket.pop(f"{goal_id}/{run['todo_id']}", None)
                bucket.pop(_todo_agent_key(goal_id, run["todo_id"], agent_id), None)
        elif run.get("todo_id"):
            # A Turn that keeps failing on the same todo backs off instead of
            # relaunching every pass; LoopX's repair/replan routing still owns
            # what happens to the todo itself.
            # Keyed by agent: a developer's failures must not stall the
            # acceptor's review of the same todo (E2E pilot).
            bucket = self.state.setdefault("todo_cooldowns", {})
            todo_key = _todo_agent_key(goal_id, run["todo_id"], agent_id)
            failures = int((bucket.get(todo_key) or {}).get("failures") or 0) + 1
            bucket[todo_key] = {
                "until": now
                + policy.backoff_seconds(
                    failures, base=self.config.backoff_base_seconds, cap=self.config.backoff_cap_seconds
                ),
                "failures": failures,
                "reason": outcome["outcome"],
                **({"error": outcome["error"]} if outcome.get("error") else {}),
            }
        if run.get("orchestrator_action") and run.get("todo_id"):
            # A failed Turn on an action todo (typically its validator, when
            # nothing is left to do) counts toward retiring it; host and
            # provider failures do not, and a committed Turn clears the count.
            failures = self.state.setdefault("orchestrator_action_failures", {})
            action_key = f"{goal_id}/{run['todo_id']}"
            if outcome["outcome"] == policy.OUTCOME_FAILED:
                failures[action_key] = int(failures.get(action_key) or 0) + 1
            elif outcome["outcome"] == policy.OUTCOME_COMMITTED:
                failures.pop(action_key, None)
        if outcome["outcome"] not in {policy.OUTCOME_CRASHED, policy.OUTCOME_COMMITTED}:
            self._keep_unsettled_turn(goal_id, agent_id, run, stdout_text, outcome, now)
        if run.get("role") == policy.ROLE_ORCHESTRATOR:
            self.state.setdefault("orchestrator_baselines", {})[
                _agent_key(goal_id, agent_id)
            ] = self.goal_fingerprint(goal_id)
        review = settle_acceptor_review(self._goal(goal_id), run, self.runtime_root)
        if review and review.get("modified") and run.get("todo_id"):
            # The role board shows this warning on the todo's card (G12).
            warnings = self.state.setdefault("review_warnings", {})
            warnings[f"{goal_id}/{run['todo_id']}"] = {"agent_id": agent_id, "at": now, "repos": review.get("repos")}
            for stale in list(warnings)[:-100]:
                warnings.pop(stale, None)
        record = {**run, **outcome, "finished_at": now, "run_id": run_id,
                  **({"review_checkout_settled": review} if review is not None else {})}
        self._running().pop(run_id, None)
        self.state.setdefault("history", []).append(record)
        prompt = Path(str(run.get("stdout_path") or "")).with_name(f"{run_id}.system-prompt.md")
        try:
            prompt.unlink()
        except OSError:
            pass
        reaped = {key: record.get(key) for key in ("run_id", "goal_id", "agent_id", "todo_id", "outcome", "failure_kind", "returncode")}
        # A failed run says why in the pass log, not only in its runs/<id>.out.json.
        reaped.update({key: record[key] for key in ("error_code", "error") if record.get(key)})
        return reaped

    def _keep_unsettled_turn(
        self,
        goal_id: str,
        agent_id: str,
        run: Mapping[str, Any],
        stdout_text: str,
        outcome: dict[str, Any],
        now: float,
    ) -> None:
        """Keep the Turn identity of a settlement that failed after its host completed.

        Like the crash branch: the next launch resumes the journaled Turn, so
        run-once settles the cached host result instead of a new Turn redoing
        the work or losing the quota spend (dispatch.settlement_retry).
        """

        unsettled = settlement_retry.unsettled_turn(self.runtime_root, goal_id, stdout_text)
        if unsettled is None or not run.get("turn_instance_id"):
            return
        count = int(run.get("settlement_retries") or 0) + 1
        if count > settlement_retry.SETTLEMENT_RETRY_LIMIT:
            # Still failing after repeated resumes: fall back to a new Turn.
            outcome["settlement_retry"] = {**unsettled, "exhausted": True, "attempts": count - 1}
            return
        self.state.setdefault("retry_turns", {})[_retry_key(goal_id, agent_id, run.get("todo_id"))] = {
            "turn_instance_id": run.get("turn_instance_id"),
            "todo_id": run.get("todo_id"),
            "settlement": True,
            "project": run.get("project"),
            "turn_key": unsettled["turn_key"],
            "failed_phase": unsettled["failed_phase"],
            "failed_at": now,
            "settlement_retries": count,
            "crashes": int(run.get("crashes") or 0),
        }
        outcome["settlement_retry"] = {**unsettled, "attempt": count}

    def _expire_cooldowns(self, now: float) -> None:
        for key in ("agent_cooldowns",):
            bucket = self.state.setdefault(key, {})
            for name in [name for name, item in bucket.items() if item.get("until", 0) <= now]:
                bucket.pop(name, None)

    def _provider_backoff(self, goal_id: str, agent_id: str, provider: str, kind: str) -> None:
        bucket = self.state.setdefault("provider_cooldowns", {})
        current = bucket.get(provider) or {}
        failures = int(current.get("failures") or 0) + 1
        seconds = policy.backoff_seconds(
            failures, base=self.config.backoff_base_seconds, cap=self.config.backoff_cap_seconds
        )
        bucket[provider] = {
            "until": self.clock() + seconds,
            "failures": failures,
            "kind": kind,
            "seconds": seconds,
        }
        if seconds >= self.config.long_cooldown_seconds:
            self._open_gate(
                goal_id,
                key=f"provider_cooldown:{provider}",
                text=(
                    f"Provider {provider} is in a long cooldown ({kind}); agent {agent_id} "
                    "is paused. Check the plan/quota or switch the agent's provider, then close this gate."
                ),
                blocks_agent=agent_id,
                report=None,
            )

    def _agent_unavailable(
        self, goal_id: str, agent_id: str, status: str, report: dict[str, Any] | None
    ) -> None:
        self.state.setdefault("agent_cooldowns", {})[agent_id] = {
            "until": self.clock() + self.config.auth_cooldown_seconds,
            "reason": "auth_preflight_failed",
            "status": status,
        }
        self._open_gate(
            goal_id,
            key=f"relogin:{agent_id}",
            text=(
                f"Agent {agent_id} needs re-login (auth preflight: {status}). "
                f"Run `loopx agent show {agent_id} --check-auth`, log in again, then close this gate."
            ),
            blocks_agent=agent_id,
            report=report,
        )

    def _agent_available(self, goal_id: str, agent_id: str) -> None:
        self.state.setdefault("agent_cooldowns", {}).pop(agent_id, None)

    # ------------------------------------------------------------------
    # user gates (one per episode)
    # ------------------------------------------------------------------

    def _open_user_todo_ids(self, goal_id: str) -> dict[str, str]:
        from ..todos import list_goal_todos

        listed = list_goal_todos(
            registry_path=self.registry_path,
            goal_id=goal_id,
            role="user",
            status="open",
            runtime_root_arg=str(self.runtime_root),
        )
        return {
            str(item.get("todo_id")): str(item.get("text") or "")
            for item in listed.get("todos") or []
            if isinstance(item, Mapping) and item.get("todo_id")
        }

    def _open_gate(
        self,
        goal_id: str,
        *,
        key: str,
        text: str,
        blocks_agent: str,
        report: dict[str, Any] | None,
    ) -> str | None:
        gates = self.state.setdefault("gates", {})
        gate_key = f"{goal_id}:{key}"
        try:
            open_todos = self._open_user_todo_ids(goal_id)
        except Exception:  # noqa: BLE001 - without a readback we must not duplicate gates
            return None
        existing = gates.get(gate_key)
        if existing and existing.get("todo_id") in open_todos:
            return None
        marker = text.split(" (", 1)[0]
        for todo_id, todo_text in open_todos.items():
            if marker and marker in todo_text:
                gates[gate_key] = {"todo_id": todo_id, "opened_at": self.clock(), "adopted": True}
                return None
        from ..todos import add_goal_todo

        try:
            payload = add_goal_todo(
                registry_path=self.registry_path,
                goal_id=goal_id,
                runtime_root_arg=str(self.runtime_root),
                role="user",
                text=text,
                task_class="user_gate",
                blocks_agent=blocks_agent,
                note="Opened by the LoopX dispatcher.",
            )
        except Exception as exc:  # noqa: BLE001 - report and retry next pass
            if report is not None:
                report["errors"].append({"goal_id": goal_id, "error": f"gate: {exc}"[:400]})
            return None
        todo_id = payload.get("todo_id")
        gates[gate_key] = {"todo_id": todo_id, "opened_at": self.clock()}
        if report is not None:
            report["gates_opened"].append({"goal_id": goal_id, "key": key, "todo_id": todo_id})
        return str(todo_id) if todo_id else None

    # ------------------------------------------------------------------
    # loops
    # ------------------------------------------------------------------

    def wait_for_children(self, run_ids: list[str], *, timeout: float | None = None) -> list[dict[str, Any]]:
        deadline = None if timeout is None else time.monotonic() + timeout
        finished: list[dict[str, Any]] = []
        while any(run_id in self.children for run_id in run_ids):
            finished.extend(self.reap())
            if deadline is not None and time.monotonic() > deadline:
                break
            time.sleep(0.05)
        finished.extend(self.reap())
        return finished

    def run_once(self, *, wait: bool = True) -> dict[str, Any]:
        """One reconcile pass (for tests and cron). Waits for its own children."""

        with DispatchLock(self.runtime_root):
            report = self.reconcile(trigger="once")
            if wait:
                run_ids = [item["run_id"] for item in report["launched"]]
                report["finished"] = self.wait_for_children(run_ids)
                save_state(self.runtime_root, self.state)
        return report

    def serve(
        self,
        *,
        stop: Callable[[], bool] = lambda: False,
        on_pass: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Resident loop: reconcile on file events, child exits and every tick."""

        with DispatchLock(self.runtime_root):
            self.state["serve_pid"] = os.getpid()
            next_tick = 0.0
            last_pass = 0.0
            self.changed_goals()  # prime fingerprints
            while not stop():
                finished = self.reap()
                changed = self.changed_goals()
                now = time.monotonic()
                trigger = None
                if now >= next_tick:
                    trigger = "tick"
                elif finished:
                    trigger = "child_exit"
                elif changed and now - last_pass >= self.config.event_debounce_seconds:
                    trigger = "state_event"
                if trigger:
                    report = self.reconcile(trigger=trigger)
                    if finished:
                        report["reaped"] = finished + report["reaped"]
                    last_pass = time.monotonic()
                    next_tick = last_pass + self.config.tick_seconds
                    self.changed_goals()
                    if on_pass is not None:
                        on_pass(report)
                time.sleep(self.config.poll_seconds)
            self.state.pop("serve_pid", None)
            save_state(self.runtime_root, self.state)


def _is_plan_todo(todo: Mapping[str, Any]) -> bool:
    return str(todo.get("action_kind") or "") == "plan"


def _default_preflight(definition: Any, environ: Mapping[str, str]) -> Mapping[str, Any]:
    from ..agent_config import preflight_agent

    return preflight_agent(definition, environ=environ)


def dispatch_status(runtime_root: Path) -> dict[str, Any]:
    """Read-only view of running turns, cooldowns and per-agent slots."""

    from .state import dispatch_lock_path, lock_is_held, read_lock_holder

    runtime_root = Path(runtime_root).expanduser()
    state = load_state(runtime_root)
    now = time.time()
    runs = []
    for run in (state.get("runs") or {}).values():
        runs.append(
            {
                **{key: run.get(key) for key in ("run_id", "goal_id", "agent_id", "role", "todo_id", "pid", "turn_instance_id")},
                "alive": pid_alive(run.get("pid")),
                "running_seconds": round(now - float(run.get("started_at") or now), 1),
            }
        )
    slots = {}
    for agent_id, info in sorted((state.get("agent_slots") or {}).items()):
        used = sum(1 for run in runs if run.get("agent_id") == agent_id)
        slots[agent_id] = {**info, "running": used}

    def active(bucket: Mapping[str, Any]) -> dict[str, Any]:
        return {
            name: {**item, "remaining_seconds": round(float(item.get("until") or 0) - now, 1)}
            for name, item in sorted(bucket.items())
            if float(item.get("until") or 0) > now
        }

    held = lock_is_held(runtime_root)
    return {
        "schema_version": "loopx_dispatch_status_v0",
        "ok": True,
        "runtime_root": str(runtime_root),
        "serving": held,
        "lock_holder_pid": read_lock_holder(dispatch_lock_path(runtime_root)) if held else None,
        "last_pass": state.get("last_pass"),
        "running": runs,
        "agent_slots": slots,
        "provider_cooldowns": active(state.get("provider_cooldowns") or {}),
        "agent_cooldowns": active(state.get("agent_cooldowns") or {}),
        "todo_cooldowns": active(state.get("todo_cooldowns") or {}),
        "gates": state.get("gates") or {},
        "retry_turns": state.get("retry_turns") or {},
        "recent": [
            {key: item.get(key) for key in ("run_id", "goal_id", "agent_id", "todo_id", "outcome", "failure_kind", "finished_at")}
            for item in (state.get("history") or [])[-10:]
        ],
    }
