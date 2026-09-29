"""The deterministic goal_complete gate (design decision 42, E2E pilot v1 gap N2).

When a role_v1 goal's work is accepted, merged and its push resolved, the
dispatcher opens one ``goal_complete`` user gate, with no model Turn: per
repo the merge target, merged todo commits and push result, the todo counts,
the usage report and open follow-ups. The owner closes the goal, adds work
(the note becomes an orchestrator follow-up todo) or leaves it open.
Every remote here is a local bare repo; should-run is LoopX's real one.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from loopx.agent_registry import load_goal_from_registry
from loopx.chat_action_store import ChatActionStore
from loopx.chat_actions import ChatActionService
from loopx.cli import main as cli_main
from loopx.control_plane.goals.activation import goal_is_stopped
from loopx.control_plane.goals.activation_service import set_goal_activation_state
from loopx.dispatch import DispatchConfig, Dispatcher
from loopx.gate_threads import gate_index_path, gate_view, read_gate_index
from loopx.goal_complete_gate import (
    GOAL_COMPLETE_GATE_OPTIONS,
    GOAL_COMPLETE_GATE_TEXT_PREFIX,
    GOAL_COMPLETE_HOLD_CLOSED,
    goal_completion_snapshot,
    settle_goal_complete_gate,
)
from loopx.rollout_event_log import rollout_event_log_path
from loopx.todo_acceptance import accept_goal_todo
from loopx.todos import add_goal_todo, complete_goal_todo, list_goal_todos
from loopx.usage_accounting import record_turn_usage
from loopx.workspace import git_workspace
from tests.dispatch.dispatch_fixtures import GOAL_ID, git, git_env, make_repo, read_jsonl, write_fixture
from tests.dispatch.test_loopx_dispatcher import Clock
from tests.usage_accounting.test_usage_ledger_report import _claude_usage

STALE_NEXT_ACTION = "Settle the integration todo as accepted; no developer repair is required."
AGENTS = {"orch": {"role": "orchestrator"}, "dev": {"role": "developer"}, "acc": {"role": "acceptor"}}


# --- fixture -------------------------------------------------------------------------


def _fixture(tmp_path: Path, monkeypatch, *, remote: bool = True, model: str = "role_v1") -> dict[str, Any]:
    for key, value in git_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    api = make_repo(tmp_path, "api")
    web = make_repo(tmp_path, "web")  # never has a remote
    bare = None
    if remote:
        bare = tmp_path / "remotes" / "api.git"
        bare.parent.mkdir(parents=True)
        git(tmp_path, "init", "-q", "--bare", str(bare))
        git(api, "remote", "add", "origin", str(bare))
        git(api, "push", "-q", "origin", "main")
    fixture = write_fixture(tmp_path, agents=AGENTS, repos={"api": api, "web": web}, agent_model=model)
    # The last Turn's Next Action, like the pilot's acceptor wrote it (N2).
    state = fixture["state"]
    state.write_text(state.read_text(encoding="utf-8") + f"\n## Next Action\n\n- {STALE_NEXT_ACTION}\n",
                     encoding="utf-8")
    return {**fixture, "api": api, "web": web, "bare": bare, "tmp": tmp_path}


def _goal(fx: dict[str, Any]) -> dict[str, Any]:
    return load_goal_from_registry(fx["registry"], GOAL_ID) or {}


def _deliver(fx: dict[str, Any], repos: list[str], *, name: str) -> str:
    added = add_goal_todo(registry_path=fx["registry"], goal_id=GOAL_ID, role="agent", text=f"Build {name}",
                          task_class="advancement_task", claimed_by="dev", runtime_root_arg=str(fx["runtime"]),
                          validation_command_json=json.dumps(["true"]),
                          role_contract={"task_repositories": repos, "required_role": "developer"})
    todo_id = str(added["todo_id"])
    paths = git_workspace.prepare(_goal(fx), todo_id, repos, fx["runtime"])["paths"]
    for repo in repos:
        worktree = Path(paths[repo])
        (worktree / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
        git(worktree, "add", ".")
        git(worktree, "commit", "-qm", name)
    delivered = complete_goal_todo(registry_path=fx["registry"], goal_id=GOAL_ID, todo_id=todo_id, role="agent",
                                   agent_id="dev", evidence="built", runtime_root_arg=str(fx["runtime"]))
    assert delivered.get("in_review") is True, delivered
    return todo_id


def _accept(fx: dict[str, Any], todo_id: str) -> None:
    accepted = accept_goal_todo(registry_path=fx["registry"], goal_id=GOAL_ID, todo_id=todo_id, agent_id="acc",
                                runtime_root_arg=str(fx["runtime"]))
    assert accepted["merge"]["ok"] is True, accepted


def _merged(fx: dict[str, Any], repos: list[str], *, name: str) -> str:
    todo_id = _deliver(fx, repos, name=name)
    _accept(fx, todo_id)
    return todo_id


def _dispatcher(fx: dict[str, Any]) -> Dispatcher:
    """The real should-run; Turns go to the fake ``turn run-once``."""

    config = DispatchConfig(registry_path=fx["registry"], runtime_root=fx["runtime"], goal_ids=[GOAL_ID],
                            no_global_sync=True, environ=fx["environ"],
                            loopx_argv=(sys.executable, str(fx["fake_loopx"])))
    return Dispatcher(config, clock=Clock())


def _gates(fx: dict[str, Any], kind: str, *, open_only: bool = True) -> list[dict[str, Any]]:
    index = read_gate_index(fx["runtime"], GOAL_ID)["gates"]
    rows = list_goal_todos(registry_path=fx["registry"], goal_id=GOAL_ID, role="user",
                           runtime_root_arg=str(fx["runtime"]))["todos"]
    return [row for row in rows if (index.get(row["todo_id"]) or {}).get("kind") == kind
            and (row["status"] == "open" or not open_only)]


def _opened(report: dict[str, Any]) -> list[str]:
    return [item["key"] for item in report["gates_opened"]]


def _cli(fx: dict[str, Any], *argv: str) -> tuple[int, dict[str, Any]]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli_main(["--registry", str(fx["registry"]), "--runtime-root", str(fx["runtime"]),
                         "--format", "json", *argv])
    return code, json.loads(buffer.getvalue())


def _resolve(fx: dict[str, Any], gate_id: str, *args: str) -> dict[str, Any]:
    code, payload = _cli(fx, "gate", "resolve", "--goal-id", GOAL_ID, "--todo-id", gate_id, *args)
    assert code == 0, payload
    return payload


def _events(fx: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [row for row in read_jsonl(rollout_event_log_path(fx["runtime"], GOAL_ID))
            if row.get("event_kind") == kind]


def _snapshot(fx: dict[str, Any]) -> dict[str, Any]:
    return goal_completion_snapshot(registry_path=fx["registry"], runtime_root=fx["runtime"], goal_id=GOAL_ID)


def _finished_and_pushed(fx: dict[str, Any], dispatcher: Dispatcher) -> tuple[list[str], dict[str, Any]]:
    """Two todos merged (api+web, api), the push gate approved; return (todos, next pass)."""

    first = _merged(fx, ["api", "web"], name="first")
    second = _merged(fx, ["api"], name="second")
    report = dispatcher.run_once()
    assert _opened(report) == ["push_request"], report
    [push] = _gates(fx, "push_request")
    assert _resolve(fx, push["todo_id"], "--decision", "approve")["push"]["pushed"] is True
    return [first, second], dispatcher.run_once()


# --- opening -----------------------------------------------------------------------------


def test_after_the_push_approval_the_gate_opens_without_a_turn(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    for index, (role, agent) in enumerate((("developer", "dev"), ("acceptor", "acc"), ("orchestrator", "orch"))):
        record_turn_usage(fx["runtime"], GOAL_ID, _claude_usage(), agent_id=agent, role=role,
                          todo_id=f"todo_u{index}", turn_key="sha256:" + f"{index:x}".rjust(64, "0"),
                          status="committed")
    dispatcher = _dispatcher(fx)
    (first, second), report = _finished_and_pushed(fx, dispatcher)

    assert _opened(report) == ["goal_complete"], report
    assert report["launched"] == [] and "orchestrator_todos_opened" not in report, "no model Turn"
    assert not fx["turn_log"].exists()
    [gate] = _gates(fx, "goal_complete")
    assert gate["todo_id"] == report["gates_opened"][0]["todo_id"]
    assert gate["task_class"] == "user_gate" and gate.get("blocks_agent") == "orch"
    text = gate["text"]
    assert text.startswith(GOAL_COMPLETE_GATE_TEXT_PREFIX)
    assert "Todos: 2 accepted, 0 reject(s), 0 superseded" in text and "3 Turn(s)" in text
    assert "--option close_goal|add_work|leave_open" in text
    assert "api: 2 merge(s) on main, pushed to origin; web: 1 merge(s) on main, local only." in text

    view = gate_view(registry_path=fx["registry"], runtime_root=fx["runtime"], goal_id=GOAL_ID,
                     todo_id=gate["todo_id"])
    assert view["kind"] == "goal_complete" and view["options"] == list(GOAL_COMPLETE_GATE_OPTIONS)
    repos = {repo["name"]: repo for repo in view["completion_repos"]}
    assert repos["api"]["push"] == "pushed" and repos["api"]["head"] == git(fx["api"], "rev-parse", "main")
    assert [commit["todo_id"] for commit in repos["api"]["merged_todo_commits"]] == [second, first]
    assert repos["web"]["push"] == "local_only"
    assert [commit["todo_id"] for commit in repos["web"]["merged_todo_commits"]] == [first]
    assert view["completion_todos"] == {"accepted": 2, "done_without_review": 0, "rejects": 0, "superseded": 0,
                                        "orchestrator_todos": 0}
    usage = view["completion_usage"]
    assert usage["turns"] == 3 and usage["accepted_todos"] == 0  # the ledger's todos are not the merged ones
    assert {row["role"] for row in usage["by_role"]} == {"developer", "acceptor", "orchestrator"}
    assert {"cost_reported_usd", "cost_estimated_usd", "agent_hours", "cost_per_accepted_todo_usd"} <= set(usage)
    [event] = _events(fx, "goal_complete_opened")
    assert event["todo_id"] == gate["todo_id"]

    # The gate holds the orchestrator; nobody is launched while it is open.
    again = dispatcher.run_once()
    assert again["launched"] == [] and _opened(again) == []


def test_the_gate_waits_for_pending_todos_and_a_pending_push(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    dispatcher = _dispatcher(fx)
    first = _merged(fx, ["api"], name="first")
    pending = _deliver(fx, ["api"], name="second")  # in_review
    assert _snapshot(fx)["reason"] == "todos_pending"
    assert "goal_complete" not in _opened(dispatcher.run_once())

    _accept(fx, pending)
    report = dispatcher.run_once()
    assert _opened(report) == ["push_request"], "the push gate comes first"
    waiting = _snapshot(fx)
    assert waiting["reason"] == "user_gate_open" and waiting["open_gate_ids"] == [_gates(fx, "push_request")[0]["todo_id"]]
    assert _opened(dispatcher.run_once()) == []
    assert _gates(fx, "goal_complete") == []

    # A push the user rejected is resolved: the completion gate opens and says so.
    [push] = _gates(fx, "push_request")
    _resolve(fx, push["todo_id"], "--decision", "reject")
    assert _opened(dispatcher.run_once()) == ["goal_complete"]
    [gate] = _gates(fx, "goal_complete")
    assert "not pushed (reject at the push gate)" in gate["text"]
    assert first in json.dumps(read_gate_index(fx["runtime"], GOAL_ID)["gates"][gate["todo_id"]])


def test_an_unpushed_merge_without_a_push_gate_is_still_pending(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    _merged(fx, ["api"], name="first")
    snapshot = _snapshot(fx)
    assert snapshot["complete"] is False and snapshot["reason"] == "push_pending"
    assert snapshot["pending_repos"] == ["api"]


def test_without_a_remote_the_gate_opens_right_after_the_merge(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch, remote=False)
    dispatcher = _dispatcher(fx)
    assert _snapshot(fx)["reason"] == "no_finished_work"
    assert _opened(dispatcher.run_once()) == [], "a goal with no finished work is not complete"
    _merged(fx, ["api", "web"], name="only")
    report = dispatcher.run_once()
    assert _opened(report) == ["goal_complete"], "no push gate: nothing to push"
    assert report["launched"] == []
    view = gate_view(registry_path=fx["registry"], runtime_root=fx["runtime"], goal_id=GOAL_ID,
                     todo_id=report["gates_opened"][0]["todo_id"])
    assert {repo["name"]: repo["push"] for repo in view["completion_repos"]} == {"api": "local_only",
                                                                                "web": "local_only"}


def test_superseded_work_never_counts_and_unaccepted_work_blocks(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch, remote=False)
    _merged(fx, ["api"], name="kept")
    replaced = add_goal_todo(registry_path=fx["registry"], goal_id=GOAL_ID, role="agent", text="Replaced",
                             task_class="advancement_task", claimed_by="dev", runtime_root_arg=str(fx["runtime"]),
                             role_contract={"required_role": "developer"})["todo_id"]
    assert _snapshot(fx)["reason"] == "todos_pending"
    code, payload = _cli(fx, "todo", "supersede", "--goal-id", GOAL_ID, "--todo-id", replaced, "--agent-id", "orch")
    assert code == 0, payload
    snapshot = _snapshot(fx)
    assert snapshot["complete"] is True, snapshot
    assert snapshot["todos"]["accepted"] == 1 and snapshot["todos"]["superseded"] == 1

    # A todo that requires acceptance but is done without an accept record
    # (for example a state edited by hand) is not accepted and merged
    # (decision 37): no completion.
    state = fx["state"]
    state.write_text(state.read_text(encoding="utf-8").replace("## Agent Todo\n", (
        "## Agent Todo\n\n- [x] Manual\n  <!-- loopx:todo todo_id=todo_manual status=done claimed_by=dev "
        "required_role=developer requires_acceptance=true acceptor_agent=acc -->\n"
    ), 1), encoding="utf-8")
    snapshot = _snapshot(fx)
    assert snapshot["reason"] == "acceptance_missing" and snapshot["unaccepted_todo_ids"] == ["todo_manual"]


# --- options -----------------------------------------------------------------------------


def test_close_goal_stops_the_goal_and_the_dispatcher_stops_considering_it(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch, remote=False)
    dispatcher = _dispatcher(fx)
    _merged(fx, ["api"], name="only")
    [opened] = dispatcher.run_once()["gates_opened"]
    payload = _resolve(fx, opened["todo_id"], "--option", "close_goal")
    settled = payload["goal_complete"]
    assert settled["ok"] is True and settled["option"] == "close_goal" and settled["decision"] == "approve"
    assert "--operation resume" in settled["resume"]
    assert goal_is_stopped(_goal(fx))
    [decided] = _events(fx, "goal_complete_decided")
    assert decided["status"] == "close_goal"

    report = dispatcher.run_once()
    held = {(item["agent_id"], item["reason"]) for item in report["skipped"]}
    assert held == {(agent, GOAL_COMPLETE_HOLD_CLOSED) for agent in AGENTS}
    assert report["launched"] == [] and report["gates_opened"] == [] and report["errors"] == []

    # Resuming through goal-lifecycle makes it eligible again; the same
    # completion is not offered a second time.
    set_goal_activation_state(registry_path=fx["registry"], goal_id=GOAL_ID, state="active",
                              runtime_root_override=str(fx["runtime"]), actor_kind="owner", execute=True)
    resumed = dispatcher.run_once()
    assert not any(item["reason"] == GOAL_COMPLETE_HOLD_CLOSED for item in resumed["skipped"])
    assert resumed["gates_opened"] == [] and len(_gates(fx, "goal_complete", open_only=False)) == 1


def test_markdown_show_resolve_and_status_report_a_closed_goal(tmp_path, monkeypatch) -> None:
    """Without --format json the owner sees the completion summary, what closing did and the stopped goal.

    The fixture goal lives only in its project registry, as ``goal create
    --no-global-sync`` leaves it, so closing it must not create a shared registry.
    """

    fx = _fixture(tmp_path, monkeypatch, remote=False)
    _merged(fx, ["api"], name="only")
    [opened] = _dispatcher(fx).run_once()["gates_opened"]

    def markdown(*argv: str) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(["--registry", str(fx["registry"]), "--runtime-root", str(fx["runtime"]), *argv])
        assert code == 0, buffer.getvalue()
        return buffer.getvalue()

    shown = markdown("gate", "show", "--goal-id", GOAL_ID, "--todo-id", opened["todo_id"])
    assert "## Completion" in shown and shown.index("## Completion") < shown.index("## Thread")
    assert "- todos: 1 accepted, 0 reject(s), 0 superseded" in shown
    assert "`api`: 1 merge(s) on main, local only" in shown and "`web`: 0 merge(s) on main, local only" in shown
    assert "- usage: $0.00 ($0.00 estimated), 0 Turn(s)" in shown
    assert "- options: close_goal, add_work, leave_open" in shown

    resolved = markdown("gate", "resolve", "--goal-id", GOAL_ID, "--todo-id", opened["todo_id"],
                        "--option", "close_goal")
    assert f"Resolved gate `{opened['todo_id']}`: approve (close_goal)" in resolved
    assert "- outcome: close_goal, goal stopped" in resolved and "--operation resume" in resolved
    assert goal_is_stopped(_goal(fx))
    assert "- outcome: close_goal, goal stopped" in markdown("gate", "show", "--goal-id", GOAL_ID, "--todo-id",
                                                           opened["todo_id"])

    # The registry status stays "active"; the typed activation state says the goal is stopped.
    status = markdown("status", "--goal-id", GOAL_ID)
    assert f"`{GOAL_ID}`: status=active activation=stopped " in status
    code, payload = _cli(fx, "status", "--goal-id", GOAL_ID)
    assert code == 0
    assert [(row["id"], row["activation_state"]) for row in payload["run_history"]["goals"]] == [
        (GOAL_ID, "stopped")]
    assert not (fx["runtime"] / "registry.global.json").exists()


def test_add_work_opens_an_orchestrator_follow_up_that_launches_a_turn(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch, remote=False)
    dispatcher = _dispatcher(fx)
    _merged(fx, ["api"], name="only")
    [opened] = dispatcher.run_once()["gates_opened"]
    gate_id = opened["todo_id"]
    code, payload = _cli(fx, "gate", "resolve", "--goal-id", GOAL_ID, "--todo-id", gate_id, "--option", "add_work")
    assert code == 1 and "add_work needs a note" in payload["error"]
    assert len(_gates(fx, "goal_complete")) == 1, "refused: the gate stays open"

    settled = _resolve(fx, gate_id, "--option", "add_work", "--note", "Also add a DELETE endpoint")["goal_complete"]
    assert settled["ok"] is True and settled["decision"] == "reject"
    follow_up = settled["follow_up_todo_id"]
    [row] = list_goal_todos(registry_path=fx["registry"], goal_id=GOAL_ID, todo_id=follow_up,
                            runtime_root_arg=str(fx["runtime"]))["todos"]
    assert row["status"] == "open" and row["claimed_by"] == "orch"
    assert row["text"].startswith("Orchestrator action: User follow-up: Also add a DELETE endpoint")
    assert f"(goal_complete gate {gate_id})" in row["text"]
    assert not goal_is_stopped(_goal(fx))
    # Replaying the settle never adds a second follow-up.
    replay = settle_goal_complete_gate(registry_path=fx["registry"], runtime_root=fx["runtime"], goal_id=GOAL_ID,
                                       gate_todo_id=gate_id, decision="reject", option="add_work", note="x")
    assert replay["replayed"] is True and replay["follow_up_todo_id"] == follow_up

    report = dispatcher.run_once()
    assert [(item["agent_id"], item["todo_id"]) for item in report["launched"]] == [("orch", follow_up)]
    assert report["gates_opened"] == []
    [turn] = read_jsonl(fx["turn_log"])
    validator = json.loads(turn["argv"][turn["argv"].index("--validation-command-json") + 1])
    assert "todos-changed-since" in validator, "the orchestrator must change some other todo"

    # Once the follow-up is done, the new completion opens a new gate.
    complete_goal_todo(registry_path=fx["registry"], goal_id=GOAL_ID, todo_id=follow_up, role="agent",
                       agent_id="orch", evidence="planned: nothing more", runtime_root_arg=str(fx["runtime"]))
    assert _opened(dispatcher.run_once()) == ["goal_complete"]
    assert len(_gates(fx, "goal_complete", open_only=False)) == 2


def test_leave_open_changes_nothing_until_new_work_finishes(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch, remote=False)
    dispatcher = _dispatcher(fx)
    _merged(fx, ["api"], name="first")
    [opened] = dispatcher.run_once()["gates_opened"]
    agents_before = list_goal_todos(registry_path=fx["registry"], goal_id=GOAL_ID, role="agent",
                                    runtime_root_arg=str(fx["runtime"]))["todos"]
    settled = _resolve(fx, opened["todo_id"], "--decision", "cancel")["goal_complete"]
    assert settled["option"] == "leave_open" and settled["ok"] is True
    assert not goal_is_stopped(_goal(fx))
    assert list_goal_todos(registry_path=fx["registry"], goal_id=GOAL_ID, role="agent",
                           runtime_root_arg=str(fx["runtime"]))["todos"] == agents_before
    quiet = dispatcher.run_once()
    assert quiet["gates_opened"] == [] and quiet["launched"] == []
    _merged(fx, ["api"], name="second")
    assert _opened(dispatcher.run_once()) == ["goal_complete"]


def test_a_gate_that_no_longer_describes_the_goal_cannot_close_it(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch, remote=False)
    dispatcher = _dispatcher(fx)
    _merged(fx, ["api"], name="first")
    [opened] = dispatcher.run_once()["gates_opened"]
    pending = _deliver(fx, ["api"], name="second")  # new work arrived while the gate was open
    code, payload = _cli(fx, "gate", "resolve", "--goal-id", GOAL_ID, "--todo-id", opened["todo_id"],
                         "--option", "close_goal")
    assert code == 1 and "changed after this gate opened (todos_pending)" in payload["error"]
    _accept(fx, pending)
    code, payload = _cli(fx, "gate", "resolve", "--goal-id", GOAL_ID, "--todo-id", opened["todo_id"],
                         "--option", "close_goal")
    assert code == 1 and "changed after this gate opened (complete)" in payload["error"], "a new merge"
    assert not goal_is_stopped(_goal(fx)) and len(_gates(fx, "goal_complete")) == 1
    _resolve(fx, opened["todo_id"], "--option", "leave_open")
    [reopened] = dispatcher.run_once()["gates_opened"]
    assert reopened["key"] == "goal_complete" and reopened["todo_id"] != opened["todo_id"]
    assert _resolve(fx, reopened["todo_id"], "--option", "close_goal")["goal_complete"]["ok"] is True
    assert goal_is_stopped(_goal(fx))


def test_mismatched_options_are_refused(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch, remote=False)
    _merged(fx, ["api"], name="only")
    [opened] = _dispatcher(fx).run_once()["gates_opened"]
    code, payload = _cli(fx, "gate", "resolve", "--goal-id", GOAL_ID, "--todo-id", opened["todo_id"],
                         "--decision", "approve", "--option", "leave_open")
    assert code == 1 and "records decision cancel" in payload["error"]
    code, payload = _cli(fx, "gate", "resolve", "--goal-id", GOAL_ID, "--todo-id", opened["todo_id"],
                         "--option", "raise_budget")
    assert code == 1
    assert len(_gates(fx, "goal_complete")) == 1


@pytest.mark.parametrize("option", GOAL_COMPLETE_GATE_OPTIONS)
def test_each_option_applies_via_the_web_resolve_path(tmp_path, monkeypatch, option: str) -> None:
    fx = _fixture(tmp_path, monkeypatch, remote=False)
    dispatcher = _dispatcher(fx)
    _merged(fx, ["api"], name="only")
    [opened] = dispatcher.run_once()["gates_opened"]
    gate_id = opened["todo_id"]
    service = ChatActionService(store=ChatActionStore(tmp_path / "actions"), registry_path=fx["registry"])
    proposal = service.preview({
        "action_kind": "gate.resolve", "summary": f"{option} the goal",
        "normalized_parameters": {"goal_id": GOAL_ID, "todo_id": gate_id, "option": option,
                                  "note": "Add CSV export"},
        "context": {}, "idempotency_key": f"goal-complete-{option}",
    })
    assert proposal["status"] == "preview_ready", proposal
    assert not goal_is_stopped(_goal(fx)), "the preview is a dry run"
    applied = service.apply(proposal["proposal_id"])["proposal"]
    assert applied["status"] == "applied", applied
    assert applied["receipt"]["decision_option"] == option
    outcome = read_gate_index(fx["runtime"], GOAL_ID)["gates"][gate_id]["completion_outcome"]
    assert outcome["option"] == option and outcome["ok"] is True
    assert goal_is_stopped(_goal(fx)) is (option == "close_goal")
    orch_open = [row for row in list_goal_todos(registry_path=fx["registry"], goal_id=GOAL_ID, role="agent",
                                                runtime_root_arg=str(fx["runtime"]))["todos"]
                 if row.get("claimed_by") == "orch" and row["status"] == "open"]
    assert len(orch_open) == (1 if option == "add_work" else 0)
    if option == "add_work":
        assert "User follow-up: Add CSV export" in orch_open[0]["text"]


def test_a_second_goal_complete_settlement_replays_the_first_option(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch, remote=False)
    dispatcher = _dispatcher(fx)
    _merged(fx, ["api"], name="only")
    [opened] = dispatcher.run_once()["gates_opened"]
    gate_id = opened["todo_id"]
    _resolve(fx, gate_id, "--option", "leave_open")
    replayed = settle_goal_complete_gate(
        registry_path=fx["registry"], runtime_root=fx["runtime"], goal_id=GOAL_ID, gate_todo_id=gate_id,
        decision="approve", option="close_goal", note=None,
    )
    assert replayed["replayed"] is True and replayed["option"] == "leave_open", replayed
    assert read_gate_index(fx["runtime"], GOAL_ID)["gates"][gate_id]["decision_option"] == "leave_open"
    assert not goal_is_stopped(_goal(fx))
    assert len(_events(fx, "goal_complete_decided")) == 1


def test_an_interrupted_add_work_is_retried_with_its_pinned_note(tmp_path, monkeypatch) -> None:
    import loopx.goal_complete_gate as goal_complete_gate

    fx = _fixture(tmp_path, monkeypatch, remote=False)
    dispatcher = _dispatcher(fx)
    _merged(fx, ["api"], name="only")
    [opened] = dispatcher.run_once()["gates_opened"]
    gate_id = opened["todo_id"]

    def settle(note: str) -> dict[str, Any]:
        return settle_goal_complete_gate(registry_path=fx["registry"], runtime_root=fx["runtime"],
                                         goal_id=GOAL_ID, gate_todo_id=gate_id, decision="reject",
                                         option="add_work", note=note)

    with monkeypatch.context() as patch:
        def interrupted(*args, **kwargs):  # the intent is recorded; the process dies before the effect
            raise RuntimeError("Synthetic interruption before the follow-up is added")

        patch.setattr(goal_complete_gate, "_add_follow_up", interrupted)
        with pytest.raises(RuntimeError):
            settle("Add CSV export")
    retried = settle("Something else entirely")
    assert (retried["ok"], retried["option"]) == (True, "add_work"), retried
    texts = [str(row.get("text") or "") for row in list_goal_todos(
        registry_path=fx["registry"], goal_id=GOAL_ID, role="agent", runtime_root_arg=str(fx["runtime"]))["todos"]
        if "User follow-up:" in str(row.get("text") or "")]
    assert len(texts) == 1 and "User follow-up: Add CSV export" in texts[0], texts
    assert "Something else entirely" not in texts[0]


def test_a_failed_goal_complete_effect_is_surfaced_and_a_web_retry_applies_it_once(tmp_path, monkeypatch) -> None:
    import loopx.goal_complete_gate as goal_complete_gate

    fx = _fixture(tmp_path, monkeypatch, remote=False)
    dispatcher = _dispatcher(fx)
    _merged(fx, ["api"], name="only")
    [opened] = dispatcher.run_once()["gates_opened"]
    gate_id = opened["todo_id"]
    service = ChatActionService(store=ChatActionStore(tmp_path / "actions"), registry_path=fx["registry"])
    proposal = service.preview({
        "action_kind": "gate.resolve", "summary": "add work",
        "normalized_parameters": {"goal_id": GOAL_ID, "todo_id": gate_id, "option": "add_work",
                                  "note": "Add CSV export"},
        "context": {}, "idempotency_key": "goal-complete-add-work",
    })

    def orchestrator_follow_ups() -> list[dict[str, Any]]:
        rows = list_goal_todos(registry_path=fx["registry"], goal_id=GOAL_ID, role="agent",
                               runtime_root_arg=str(fx["runtime"]))["todos"]
        return [row for row in rows if "User follow-up: Add CSV export" in str(row.get("text") or "")]

    with monkeypatch.context() as patch:
        def unavailable(*args, **kwargs):
            raise OSError("Synthetic todo store failure")

        patch.setattr(goal_complete_gate, "_add_follow_up", unavailable)
        failed = service.apply(proposal["proposal_id"])["proposal"]
    assert failed["status"] == "failed", failed
    failure = failed["failure"]
    assert failure["error_code"] == "gate_settlement_retry_required", failure
    assert (failure["details"]["gate_todo_id"], failure["details"]["settlement"], failure["details"]["option"]) == (
        gate_id, "goal_complete", "add_work")
    assert orchestrator_follow_ups() == []
    assert read_gate_index(fx["runtime"], GOAL_ID)["gates"][gate_id]["completion_outcome"]["ok"] is False

    retried = service.apply(proposal["proposal_id"])["proposal"]
    assert retried["status"] == "applied", retried
    assert len(orchestrator_follow_ups()) == 1
    outcome = read_gate_index(fx["runtime"], GOAL_ID)["gates"][gate_id]["completion_outcome"]
    assert (outcome["ok"], outcome["option"]) == (True, "add_work")


# --- idempotency ------------------------------------------------------------------------------


def test_replayed_and_restarted_dispatchers_open_one_gate_per_completion(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch, remote=False)
    _merged(fx, ["api"], name="only")
    first = _dispatcher(fx)
    assert _opened(first.run_once()) == ["goal_complete"]
    assert _opened(first.run_once()) == []
    # A fresh dispatcher, and one whose state file is lost.
    assert _opened(_dispatcher(fx).run_once()) == []
    (fx["runtime"] / "dispatch" / "state.json").unlink()
    assert _opened(_dispatcher(fx).run_once()) == []
    [gate] = _gates(fx, "goal_complete")
    # The open gate is adopted by its text even if its index entry was lost.
    index_path = gate_index_path(fx["runtime"], GOAL_ID)
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["gates"].pop(gate["todo_id"])
    index_path.write_text(json.dumps(index), encoding="utf-8")
    assert _opened(_dispatcher(fx).run_once()) == []
    assert read_gate_index(fx["runtime"], GOAL_ID)["gates"][gate["todo_id"]]["kind"] == "goal_complete"
    assert len(_gates(fx, "goal_complete", open_only=False)) == 1
    assert len(_events(fx, "goal_complete_opened")) == 1


# --- peer_v1 -------------------------------------------------------------------------------------


def test_peer_v1_goals_get_no_goal_complete_gate(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch, model="peer_v1")
    todo_id = add_goal_todo(registry_path=fx["registry"], goal_id=GOAL_ID, role="agent", text="Peer work",
                            task_class="advancement_task", claimed_by="dev",
                            runtime_root_arg=str(fx["runtime"]))["todo_id"]
    complete_goal_todo(registry_path=fx["registry"], goal_id=GOAL_ID, todo_id=todo_id, role="agent",
                       agent_id="dev", evidence="done", no_followup=True, runtime_root_arg=str(fx["runtime"]))
    assert _snapshot(fx)["reason"] == "not_role_v1"
    report = _dispatcher(fx).run_once()
    assert "goal_complete" not in _opened(report)
    assert _gates(fx, "goal_complete", open_only=False) == []


# --- prompts -------------------------------------------------------------------------------------


def test_the_orchestrator_is_told_not_to_open_a_closure_gate() -> None:
    from loopx.dispatch.orchestrator_actions import action_todo_text
    from loopx.dispatch.prompts import dispatch_prompt_addendum

    addendum = dispatch_prompt_addendum(goal_id=GOAL_ID, agent_id="orch", role="orchestrator", todo_id="todo_x",
                                        workspace_repos=None)
    assert "Never open a gate to confirm that the goal is complete" in addendum
    assert "LoopX opens a goal_complete gate itself" in addendum
    assert "User follow-up" in addendum
    text = action_todo_text("autonomous_replan_required", [])
    assert "terminal outcome" not in text and "LoopX opens the goal_complete gate itself" in text
    developer = dispatch_prompt_addendum(goal_id=GOAL_ID, agent_id="dev", role="developer", todo_id="todo_x",
                                         workspace_repos=None)
    assert "goal_complete" not in developer
