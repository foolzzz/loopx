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
from .prompts import compose_system_prompt, dispatch_prompt_addendum, replace_system_prompt_argument
from .state import (
    DispatchLock,
    dispatch_dir,
    load_state,
    pid_alive,
    save_state,
)

DISPATCH_PASS_SCHEMA_VERSION = "loopx_dispatch_pass_v0"
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
        self.registry_path = Path(config.registry_path).expanduser()
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
            if child.name in {"workspaces"}:
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
        for agent_id, registry_role in self._agents_for_goal(goal):
            skip = lambda reason, **extra: report["skipped"].append(  # noqa: E731
                {"goal_id": goal_id, "agent_id": agent_id, "reason": reason, **extra}
            )
            try:
                definition = resolve_agent(agent_id, project, runtime_root=self.runtime_root)
            except AgentConfigError as exc:
                skip("agent_config_invalid", issues=list(exc.issues)[:5])
                continue
            if not definition.enabled:
                skip("agent_disabled")
                continue
            role = registry_role or definition.role
            limit = policy.slot_limit(role, definition.max_concurrency)
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
            while True:
                runs = self._running()
                if len(runs) >= self.config.max_global:
                    skip("global_cap")
                    break
                agent_runs = [run for run in runs.values() if run.get("agent_id") == agent_id]
                if len(agent_runs) >= limit:
                    skip("slots_full", running=len(agent_runs), max=limit)
                    break
                if role == policy.ROLE_ORCHESTRATOR and any(
                    run.get("goal_id") == goal_id and run.get("role") == policy.ROLE_ORCHESTRATOR
                    for run in runs.values()
                ):
                    skip("orchestrator_running")
                    break
                try:
                    payload = self._should_run(goal_id, agent_id)
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
                if not decision["launch"]:
                    skip(
                        decision["reason"],
                        **{key: value for key, value in decision.items() if key not in {"launch", "reason"}},
                    )
                    break
                todo_id = decision.get("todo_id")
                in_flight = {
                    run.get("todo_id")
                    for run in runs.values()
                    if run.get("goal_id") == goal_id and run.get("todo_id")
                }
                if todo_id and todo_id in in_flight:
                    skip("todo_in_flight", todo_id=todo_id)
                    break
                todo_cooldown = self.state.get("todo_cooldowns", {}).get(f"{goal_id}/{todo_id}")
                if todo_id and todo_cooldown and todo_cooldown.get("until", 0) > now:
                    skip("todo_cooldown", todo_id=todo_id)
                    break
                if not preflight_done:
                    record = self._preflight(definition, self.environ)
                    preflight_done = True
                    if not record.get("ok"):
                        self._agent_unavailable(goal_id, agent_id, str(record.get("status") or "unknown"), report)
                        break
                    self._agent_available(goal_id, agent_id)
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
    ) -> tuple[bool, Path | None, dict[str, str]]:
        """Return ``(ok, cwd, repo_paths)``; cwd is None when no workspace applies."""

        if role not in {policy.ROLE_DEVELOPER, policy.ROLE_ACCEPTOR, None}:
            return True, None, {}
        goal_id = str(goal.get("id"))
        repos = [str(item) for item in (todo.get("task_repositories") or []) if item]
        if not repos:
            return True, None, {}
        from ..workspace import git_workspace

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
                    "error": "workspace_prepare_failed",
                    "repos": [
                        {"name": item.get("name"), "error_code": item.get("error_code")}
                        for item in prepared.get("repos") or []
                        if not item.get("ok")
                    ]
                    or prepared.get("error_code"),
                }
            )
            return False, None, {}
        paths = {str(name): str(path) for name, path in (prepared.get("paths") or {}).items()}
        if len(paths) == 1:
            return True, Path(next(iter(paths.values()))), paths
        return True, Path(str(prepared["workspace_root"])), paths

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
        if todo_id and decision.get("todo_is_agent_todo", True):
            todo = self._todo_record(goal_id, str(todo_id)) or {}
            ok, cwd, repo_paths = self._prepare_workspace(goal, str(todo_id), todo, role, report)
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

        retry_key = _agent_key(goal_id, agent_id)
        retry = self.state.setdefault("retry_turns", {}).get(retry_key)
        crashes = 0
        if retry and retry.get("todo_id") == todo_id:
            turn_instance_id = str(retry["turn_instance_id"])
            crashes = int(retry.get("crashes") or 0)
            reused = True
        else:
            turn_instance_id = mint_turn_instance_id(prefix="dispatch")
            reused = False
        self.state["retry_turns"].pop(retry_key, None)

        run_id = f"{int(self.clock())}-{uuid.uuid4().hex[:8]}"
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
                    awaiting_gates=(
                        gates_awaiting_orchestrator(self.runtime_root, goal_id)
                        if role == "orchestrator" else None
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
            if cwd is not None and todo_id:
                argv.extend(["--todo-id", str(todo_id)])
        if not validation_argv and self.config.default_validation_argv:
            validation_argv = list(self.config.default_validation_argv)
        if validation_argv:
            argv.extend(["--validation-command-json", json.dumps(validation_argv)])
            if validation_timeout:
                argv.extend(["--validation-timeout-seconds", str(validation_timeout)])
        if self.config.no_global_sync:
            argv.append("--no-global-sync")
        argv.extend(self.config.extra_turn_args)
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
            "reason": decision.get("reason"),
            "pid": child.pid,
            "started_at": self.clock(),
            "project": str(run_project),
            "workspace_repos": sorted(repo_paths),
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
            self.state.setdefault("retry_turns", {})[_agent_key(goal_id, agent_id)] = {
                "turn_instance_id": run.get("turn_instance_id"),
                "todo_id": run.get("todo_id"),
                "crashed_at": now,
                "crashes": crashes,
            }
            if crashes >= 3 and run.get("todo_id"):
                self.state.setdefault("todo_cooldowns", {})[f"{goal_id}/{run['todo_id']}"] = {
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
                self.state.setdefault("todo_cooldowns", {}).pop(f"{goal_id}/{run['todo_id']}", None)
        elif run.get("todo_id"):
            # A Turn that keeps failing on the same todo backs off instead of
            # relaunching every pass; LoopX's repair/replan routing still owns
            # what happens to the todo itself.
            bucket = self.state.setdefault("todo_cooldowns", {})
            todo_key = f"{goal_id}/{run['todo_id']}"
            failures = int((bucket.get(todo_key) or {}).get("failures") or 0) + 1
            bucket[todo_key] = {
                "until": now
                + policy.backoff_seconds(
                    failures, base=self.config.backoff_base_seconds, cap=self.config.backoff_cap_seconds
                ),
                "failures": failures,
                "reason": outcome["outcome"],
            }
        if run.get("role") == policy.ROLE_ORCHESTRATOR:
            self.state.setdefault("orchestrator_baselines", {})[
                _agent_key(goal_id, agent_id)
            ] = self.goal_fingerprint(goal_id)
        record = {**run, **outcome, "finished_at": now, "run_id": run_id}
        self._running().pop(run_id, None)
        self.state.setdefault("history", []).append(record)
        prompt = Path(str(run.get("stdout_path") or "")).with_name(f"{run_id}.system-prompt.md")
        try:
            prompt.unlink()
        except OSError:
            pass
        return {key: record.get(key) for key in ("run_id", "goal_id", "agent_id", "todo_id", "outcome", "failure_kind", "returncode")}

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
        "gates": state.get("gates") or {},
        "retry_turns": state.get("retry_turns") or {},
        "recent": [
            {key: item.get(key) for key in ("run_id", "goal_id", "agent_id", "todo_id", "outcome", "failure_kind", "finished_at")}
            for item in (state.get("history") or [])[-10:]
        ],
    }
