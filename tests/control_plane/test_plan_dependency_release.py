"""Fork gap G6: dependency release needs accept+merge; replacements use todo supersede.

In the E2E pilot the orchestrator replaced a twice-rejected frontend todo and
closed the old one as done, which released the dependent integration todo
before the replacement was accepted and merged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loopx.cli import main
from loopx.control_plane.status.role_board_projection import build_goal_role_board
from loopx.dispatch.prompts import dispatch_prompt_addendum
from loopx.plan_cards import propose_plan
from loopx.plan_dependencies import (
    DEPENDENCY_REWRITE_EVENT_KIND,
    SupersessionError,
    dependency_waits,
    plan_dependency_states,
    read_supersessions,
    supersede_goal_todo_by,
    supersession_map,
    supersessions_path,
)
from loopx.plan_cards import resume_ready_plan_todos
from loopx.rollout_event_log import load_rollout_events, rollout_event_log_path
from loopx.todo_acceptance import accept_goal_todo, reject_goal_todo
from loopx.todos import add_goal_todo, complete_goal_todo, list_goal_todos
from loopx.workspace import git_workspace
from tests.dispatch.dispatch_fixtures import git, git_env, make_repo

GOAL = "g6-release"
ORCH, DEV, ACC = "orch", "dev", "acc"


def _fixture(tmp_path: Path, monkeypatch, *, agent_model: str = "role_v1") -> tuple[Path, Path, dict]:
    for key, value in git_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    home = tmp_path / "progress"
    home.mkdir()
    state = home / "ACTIVE_GOAL_STATE.md"
    state.write_text("# Goal\n\n## User Todo\n\n## Agent Todo\n\n## Completed Work Archive\n", encoding="utf-8")
    api, web = make_repo(tmp_path, "api"), make_repo(tmp_path, "web")
    runtime = tmp_path / "runtime"
    goal = {
        "id": GOAL, "status": "active", "repo": str(home), "state_file": state.name,
        "repos": [{"name": "api", "path": str(api), "default_branch": "main", "merge_target": "task_branch"},
                  {"name": "web", "path": str(web), "default_branch": "main", "merge_target": "task_branch"}],
        "coordination": {"agent_model": agent_model, "registered_agents": [ORCH, DEV, ACC],
                         "agent_roles": {ORCH: "orchestrator", DEV: "developer", ACC: "acceptor"}},
    }
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"schema_version": 1, "common_runtime_root": str(runtime), "goals": [goal]}),
                        encoding="utf-8")
    return registry, runtime, goal


PLAN = {
    "title": "Todo app", "summary": "Notes, frontend, then integration.",
    "todos": [
        {"key": "notes", "text": "Write release notes", "bound_agent": DEV, "requires_acceptance": False},
        {"key": "front", "text": "Build the frontend", "bound_agent": DEV, "task_repositories": ["web"]},
        {"key": "integrate", "text": "Integrate", "bound_agent": DEV, "depends_on": ["notes", "front"],
         "task_repositories": ["api", "web"]},
    ],
}


def _apply(registry: Path, runtime: Path, plan: dict = PLAN) -> dict[str, str]:
    proposed = propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=ORCH, plan=plan)
    done = complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=proposed["plan"]["gate_todo_id"],
                              role="user", decision_outcome="approve", note="Go", no_followup=True, agent_id=ORCH)
    return dict(done["plan_card"]["todo_id_map"])


def _rows(registry: Path) -> dict[str, dict]:
    return {row["todo_id"]: row for row in list_goal_todos(registry_path=registry, goal_id=GOAL)["todos"]}


def _status(registry: Path, todo_id: str) -> str:
    return _rows(registry)[todo_id]["status"]


def _resume(registry: Path, runtime: Path) -> list[str]:
    return resume_ready_plan_todos(registry_path=registry, goal_id=GOAL, runtime_root=runtime)


def _waits(registry: Path, runtime: Path) -> dict[str, list[str]]:
    return dependency_waits(registry_path=registry, goal_id=GOAL, runtime_root=runtime)


def _deliver(registry: Path, runtime: Path, goal: dict, todo_id: str, files: dict[str, str]) -> None:
    """Commit ``files`` in the todo's workspace and deliver it for review."""

    paths = git_workspace.prepare(goal, todo_id, None, runtime)["paths"]
    for name, content in files.items():
        repo, rel = name.split("/", 1)
        (Path(paths[repo]) / rel).write_text(content, encoding="utf-8")
        git(Path(paths[repo]), "add", ".")
        git(Path(paths[repo]), "commit", "-qm", f"work on {todo_id}")
    delivered = complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=todo_id, role="agent",
                                   evidence="built", agent_id=DEV)
    assert delivered.get("in_review") is True, delivered


def _finish_notes(registry: Path, todo_id: str) -> None:
    # requires_acceptance=false: the developer's completion is final.
    complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=todo_id, role="agent",
                       evidence="written", agent_id=DEV)
    assert _status(registry, todo_id) == "done"


def _escalate(registry: Path, runtime: Path, goal: dict, todo_id: str) -> None:
    """Deliver and reject twice: the todo is blocked and escalated (the pilot's shape)."""

    _deliver(registry, runtime, goal, todo_id, {"web/ui.txt": "v1\n"})
    reject_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=todo_id, agent_id=ACC, note="broken layout")
    complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=todo_id, role="agent",
                       evidence="rebuilt", agent_id=DEV)
    reject_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=todo_id, agent_id=ACC, note="still broken")
    assert _status(registry, todo_id) == "blocked"


def _replacement(registry: Path, text: str = "Rebuild the frontend") -> str:
    added = add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text=text, claimed_by=DEV,
                          task_class="advancement_task",
                          role_contract={"required_role": "developer", "task_repositories": ["web"]})
    return str(added["todo_id"])


def test_a_done_dependency_without_accept_and_merge_keeps_its_dependent_waiting(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    registry, runtime, goal = _fixture(tmp_path, monkeypatch)
    ids = _apply(registry, runtime)
    _finish_notes(registry, ids["notes"])
    _escalate(registry, runtime, goal, ids["front"])
    # A manual path: the blocked (escalated) todo is closed as done without a verdict.
    closed = complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=ids["front"], role="agent",
                                evidence="replaced by hand", agent_id=DEV)
    assert closed.get("ok", True) and _status(registry, ids["front"]) == "done"

    assert _resume(registry, runtime) == []
    assert _status(registry, ids["integrate"]) == "deferred"
    waits = _waits(registry, runtime)
    assert list(waits) == [ids["integrate"]]
    assert "without an accept+merge record" in waits[ids["integrate"]][0]
    assert ids["front"] in waits[ids["integrate"]][0]

    # The wait is visible in `todo list` (JSON and Markdown) ...
    base = ["--registry", str(registry), "--runtime-root", str(runtime)]
    assert main([*base, "--format", "json", "todo", "list", "--goal-id", GOAL]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["dependency_waits"] == [{"todo_id": ids["integrate"], "reasons": waits[ids["integrate"]]}]
    row = next(item for item in listed["todos"] if item["todo_id"] == ids["integrate"])
    assert "without an accept+merge record" in row["dependency_wait"]
    assert main([*base, "todo", "list", "--goal-id", GOAL]) == 0
    assert "## Dependency waits" in capsys.readouterr().out
    # ... and on the role board card.
    board = build_goal_role_board(goal=goal, todos=list(_rows(registry).values()), runtime_root=runtime,
                                  dispatcher={"available": True})
    card = next(item for item in board["todos"] if item["todo_id"] == ids["integrate"])
    assert "without an accept+merge record" in card["dependency_wait"]


def test_accept_and_merge_release_the_dependent(tmp_path: Path, monkeypatch) -> None:
    registry, runtime, goal = _fixture(tmp_path, monkeypatch)
    ids = _apply(registry, runtime)
    _deliver(registry, runtime, goal, ids["front"], {"web/ui.txt": "ui\n"})
    accepted = accept_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=ids["front"], agent_id=ACC)
    assert accepted["merge"]["ok"] is True and "resumed_todo_ids" not in accepted
    # front is accepted and merged; notes (no acceptance needed) is still open.
    assert _status(registry, ids["integrate"]) == "deferred"
    assert "waiting for dependency" in _waits(registry, runtime)[ids["integrate"]][0]
    _finish_notes(registry, ids["notes"])
    assert _resume(registry, runtime) == [ids["integrate"]]
    assert _status(registry, ids["integrate"]) == "open"
    assert git(tmp_path / "repos" / "web", "show", f"loopx-task/{GOAL}:ui.txt") == "ui"
    assert _waits(registry, runtime) == {}


def test_done_suffices_for_a_dependency_without_acceptance(tmp_path: Path, monkeypatch) -> None:
    registry, runtime, _goal = _fixture(tmp_path, monkeypatch)
    plan = {**PLAN, "todos": [PLAN["todos"][0], {"key": "after", "text": "After the notes", "bound_agent": DEV,
                                                 "depends_on": ["notes"], "requires_acceptance": False}]}
    ids = _apply(registry, runtime, plan)
    assert _status(registry, ids["after"]) == "deferred"
    _finish_notes(registry, ids["notes"])
    assert _resume(registry, runtime) == [ids["after"]]


def test_supersede_rewires_dependents_and_only_the_replacement_releases_them(
    tmp_path: Path, monkeypatch,
) -> None:
    registry, runtime, goal = _fixture(tmp_path, monkeypatch)
    ids = _apply(registry, runtime)
    _finish_notes(registry, ids["notes"])
    _escalate(registry, runtime, goal, ids["front"])
    new = _replacement(registry)

    result = supersede_goal_todo_by(registry_path=registry, goal_id=GOAL, todo_id=ids["front"], by=[new],
                                    agent_id=ORCH, note="rebuild from scratch")
    assert result["ok"] is True and result["rewired_todo_ids"] == [ids["integrate"]]
    old = _rows(registry)[ids["front"]]
    assert (old["status"], old["note"]) == ("done", "superseded")
    integrate = _rows(registry)[ids["integrate"]]
    # The kernel's single resume_when condition follows the rewire.
    assert integrate["resume_when"] == f"todo_done:{new}"

    # The superseded todo does not release the dependent; it waits on the replacement.
    assert _resume(registry, runtime) == [] and _status(registry, ids["integrate"]) == "deferred"
    reasons = _waits(registry, runtime)[ids["integrate"]]
    assert reasons == [f"waiting for dependency {new} (status open)"]

    _deliver(registry, runtime, goal, new, {"web/ui.txt": "v2\n"})
    accepted = accept_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=new, agent_id=ACC)
    assert accepted["merge"]["ok"] is True
    assert accepted["resumed_todo_ids"] == [ids["integrate"]]
    assert _status(registry, ids["integrate"]) == "open"
    assert git(tmp_path / "repos" / "web", "show", f"loopx-task/{GOAL}:ui.txt") == "v2"


def test_supersession_log_replays_and_retries_are_idempotent(tmp_path: Path, monkeypatch) -> None:
    registry, runtime, _goal = _fixture(tmp_path, monkeypatch)
    ids = _apply(registry, runtime)
    new = _replacement(registry)
    supersede_goal_todo_by(registry_path=registry, goal_id=GOAL, todo_id=ids["front"], by=new, agent_id=ORCH)
    again = supersede_goal_todo_by(registry_path=registry, goal_id=GOAL, todo_id=ids["front"], by=new, agent_id=ORCH)
    assert again["already_superseded"] is True and again["changed"] is False
    with pytest.raises(SupersessionError, match="already superseded"):
        supersede_goal_todo_by(registry_path=registry, goal_id=GOAL, todo_id=ids["front"],
                               by=_replacement(registry, "Other"), agent_id=ORCH)

    records = read_supersessions(runtime, GOAL)
    assert len(records) == 1
    assert (records[0]["superseded_todo_id"], records[0]["by_todo_ids"], records[0]["actor"]) == (
        ids["front"], [new], ORCH)
    # The effective graph is a pure replay of the applied plan plus the log.
    assert supersession_map(records) == {ids["front"]: [new]}
    states = plan_dependency_states(registry_path=registry, goal_id=GOAL, runtime_root=runtime)
    assert [(s["todo_id"], s["depends_on"]) for s in states] == [(ids["integrate"], [ids["notes"], new])]
    # A torn trailing line (interrupted append) does not break the replay.
    with supersessions_path(runtime, GOAL).open("a", encoding="utf-8") as handle:
        handle.write('{"schema_version": "loopx_todo_supersession_v0", "superseded')
    assert supersession_map(read_supersessions(runtime, GOAL)) == {ids["front"]: [new]}

    events = [event for event in load_rollout_events(rollout_event_log_path(runtime, GOAL))
              if event["event_kind"] == DEPENDENCY_REWRITE_EVENT_KIND]
    assert len(events) == 1
    assert events[0]["details"] == {"superseded_todo_id": ids["front"], "by_todo_ids": new,
                                    "rewired_todo_ids": ids["integrate"]}


def test_split_supersede_makes_dependents_wait_for_every_replacement(tmp_path: Path, monkeypatch) -> None:
    registry, runtime, goal = _fixture(tmp_path, monkeypatch)
    ids = _apply(registry, runtime)
    _finish_notes(registry, ids["notes"])
    first, second = _replacement(registry, "Frontend list view"), _replacement(registry, "Frontend form")
    result = supersede_goal_todo_by(registry_path=registry, goal_id=GOAL, todo_id=ids["front"],
                                    by=f"{first},{second}", agent_id=ORCH)
    assert result["superseded_by"] == [first, second]
    state = plan_dependency_states(registry_path=registry, goal_id=GOAL, runtime_root=runtime)[0]
    assert state["depends_on"] == [ids["notes"], first, second]

    _deliver(registry, runtime, goal, first, {"web/list.txt": "list\n"})
    accept_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=first, agent_id=ACC)
    assert _status(registry, ids["integrate"]) == "deferred"
    assert _waits(registry, runtime)[ids["integrate"]] == [f"waiting for dependency {second} (status open)"]
    _deliver(registry, runtime, goal, second, {"web/form.txt": "form\n"})
    accepted = accept_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=second, agent_id=ACC)
    assert accepted["resumed_todo_ids"] == [ids["integrate"]]


def test_supersede_authority_and_guards(tmp_path: Path, monkeypatch) -> None:
    registry, runtime, _goal = _fixture(tmp_path, monkeypatch)
    ids = _apply(registry, runtime)
    new = _replacement(registry)
    for agent in (DEV, ACC):
        with pytest.raises(SupersessionError, match="only its orchestrator") as error:
            supersede_goal_todo_by(registry_path=registry, goal_id=GOAL, todo_id=ids["front"], by=new, agent_id=agent)
        assert error.value.code == "not_orchestrator"
    assert _status(registry, ids["front"]) == "open" and read_supersessions(runtime, GOAL) == []
    with pytest.raises(SupersessionError, match="depends on"):
        # integrate depends on front; it cannot replace it.
        supersede_goal_todo_by(registry_path=registry, goal_id=GOAL, todo_id=ids["front"], by=ids["integrate"])
    with pytest.raises(SupersessionError, match="itself"):
        supersede_goal_todo_by(registry_path=registry, goal_id=GOAL, todo_id=ids["front"], by=ids["front"])
    with pytest.raises(SupersessionError, match="already finished"):
        _finish_notes(registry, ids["notes"])
        supersede_goal_todo_by(registry_path=registry, goal_id=GOAL, todo_id=ids["front"], by=ids["notes"])
    preview = supersede_goal_todo_by(registry_path=registry, goal_id=GOAL, todo_id=ids["front"], by=new, dry_run=True)
    assert preview["rewired_todo_ids"] == [ids["integrate"]] and _status(registry, ids["front"]) == "open"
    # The owner (no agent id) may supersede.
    owner = supersede_goal_todo_by(registry_path=registry, goal_id=GOAL, todo_id=ids["front"], by=new)
    assert owner["ok"] is True and read_supersessions(runtime, GOAL)[0]["actor"] is None


def test_cli_todo_supersede_by(tmp_path: Path, monkeypatch, capsys) -> None:
    registry, runtime, _goal = _fixture(tmp_path, monkeypatch)
    ids = _apply(registry, runtime)
    new = _replacement(registry)
    base = ["--registry", str(registry), "--runtime-root", str(runtime), "--format", "json", "todo"]
    assert main([*base, "supersede", "--goal-id", GOAL, "--todo-id", ids["front"], "--by", new,
                 "--agent-id", DEV]) == 1
    assert "only its orchestrator" in json.loads(capsys.readouterr().out)["error"]
    assert main([*base, "supersede", "--goal-id", GOAL, "--todo-id", ids["front"], "--by", new,
                 "--next-agent-todo", "x", "--agent-id", ORCH]) == 1
    assert "--by names existing replacement todos" in json.loads(capsys.readouterr().out)["error"]
    assert main([*base, "update", "--goal-id", GOAL, "--todo-id", ids["front"], "--by", new]) == 1
    assert "--by is supported only by todo supersede" in json.loads(capsys.readouterr().out)["error"]
    assert main([*base, "supersede", "--goal-id", GOAL, "--todo-id", ids["front"], "--by", new,
                 "--agent-id", ORCH, "--note", "rebuild"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rewired_todo_ids"] == [ids["integrate"]] and payload["supersession"]["note"] == "rebuild"
    kinds = [event["event_kind"] for event in load_rollout_events(rollout_event_log_path(runtime, GOAL))]
    assert kinds.count("todo_supersede") == 1 and kinds.count(DEPENDENCY_REWRITE_EVENT_KIND) == 1


def test_peer_v1_release_and_supersede_are_unchanged(tmp_path: Path, monkeypatch) -> None:
    registry, runtime, goal = _fixture(tmp_path, monkeypatch)
    ids = _apply(registry, runtime)  # plans are proposed under role_v1 ...
    data = json.loads(registry.read_text(encoding="utf-8"))
    data["goals"][0]["coordination"]["agent_model"] = "peer_v1"  # ... then the goal runs as peer_v1.
    registry.write_text(json.dumps(data), encoding="utf-8")
    new = _replacement(registry)
    with pytest.raises(SupersessionError, match="requires a role_v1 goal"):
        supersede_goal_todo_by(registry_path=registry, goal_id=GOAL, todo_id=ids["front"], by=new)
    for key in ("notes", "front"):
        # On peer_v1 a developer completion is final; no acceptance, no merge record.
        complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=ids[key], role="agent",
                           evidence="done", agent_id=DEV)
        assert _status(registry, ids[key]) == "done"
    assert _waits(registry, runtime) == {}
    assert _resume(registry, runtime) == [ids["integrate"]]


def test_orchestrator_prompt_says_supersede_and_never_done() -> None:
    text = dispatch_prompt_addendum(goal_id=GOAL, agent_id=ORCH, role="orchestrator", todo_id=None,
                                    workspace_repos=None)
    assert f"loopx todo supersede --goal-id {GOAL} --todo-id OLD --by NEW[,NEW2] --agent-id {ORCH}" in text
    assert "Never mark a replaced todo done" in text


@pytest.mark.parametrize("provider", ["file", "sqlite"])
def test_supersede_by_on_canonical_authority_goals(tmp_path: Path, provider: str, monkeypatch) -> None:
    from test_gates_plans_intake import GOAL as PLAN_GOAL
    from test_gates_plans_intake import PLAN as INTAKE_PLAN
    from test_gates_plans_intake import fixture, rows

    if provider == "sqlite":
        from canonical_authority_fixture import isolate_sqlite_runtime
        isolate_sqlite_runtime(tmp_path, monkeypatch)
    registry, runtime = fixture(tmp_path, provider)
    plan = {**INTAKE_PLAN, "todos": [{k: v for k, v in item.items() if k != "validation_command"}
                                     for item in INTAKE_PLAN["todos"]]}
    proposed = propose_plan(registry_path=registry, runtime_root=runtime, goal_id=PLAN_GOAL, agent_id=ORCH, plan=plan)
    done = complete_goal_todo(registry_path=registry, goal_id=PLAN_GOAL, todo_id=proposed["plan"]["gate_todo_id"],
                              role="user", decision_outcome="approve", note="Go", no_followup=True, agent_id=ORCH)
    ids = done["plan_card"]["todo_id_map"]
    new = str(add_goal_todo(registry_path=registry, goal_id=PLAN_GOAL, role="agent", text="Web v2", claimed_by=DEV,
                            role_contract={"required_role": "developer"})["todo_id"])
    result = supersede_goal_todo_by(registry_path=registry, goal_id=PLAN_GOAL, todo_id=ids["web"], by=new,
                                    agent_id=ORCH)
    assert result["rewired_todo_ids"] == [ids["integrate"]]
    state = rows(registry)
    assert (state[ids["web"]]["status"], state[ids["web"]]["note"]) == ("done", "superseded")
    assert state[ids["integrate"]]["resume_when"] == f"todo_done:{new}"
