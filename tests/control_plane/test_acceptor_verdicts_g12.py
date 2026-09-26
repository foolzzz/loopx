"""Fork gap G12: acceptor isolation and the three acceptor verdicts.

Covers the delivered-sha record, the detached review checkout and its
modification warning, the pinned accept merge (``delivery_moved``), the
acceptor's role-based sandbox defaults, reject-feedback enforcement, the
``blocked`` verdict with its user gate and four options (CLI and web), the
dispatcher's relaunch guard, and that peer_v1 goals are unaffected.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from loopx.chat_action_store import ChatActionStore
from loopx.chat_actions import ChatActionService
from loopx.cli import main as cli_main
from loopx.dispatch.review_checkouts import (
    blocked_review_todo_ids,
    prepare_acceptor_review,
    review_run_record,
    settle_acceptor_review,
)
from loopx.gate_threads import read_gate_index
from loopx.rollout_event_log import rollout_event_log_path
from loopx.todo_acceptance import accept_goal_todo, reject_delivery_from_turn, reject_goal_todo
from loopx.todo_review_blocked import (
    REVIEW_GATE_OPTIONS,
    block_goal_todo_review,
    settle_turn_stop_verdict,
)
from loopx.todos import add_goal_todo, complete_goal_todo, list_goal_todos
from loopx.workspace import git_workspace
from loopx.workspace.review_checkout import parse_delivered_shas, review_checkout_root
from tests.dispatch.dispatch_fixtures import git, git_env, make_repo

GOAL = "g12-goal"
ROLES = {"orch": "orchestrator", "dev": "developer", "acc": "acceptor"}


# --- fixture -----------------------------------------------------------------


def _fixture(tmp_path: Path, monkeypatch, *, model: str = "role_v1") -> dict:
    for key, value in git_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    home = tmp_path / "progress"
    home.mkdir()
    state = home / "ACTIVE_GOAL_STATE.md"
    state.write_text("# Goal\n\n## User Todo\n\n## Agent Todo\n\n## Completed Work Archive\n", encoding="utf-8")
    api = make_repo(tmp_path, "api")
    runtime = tmp_path / "runtime"
    coordination: dict = {"agent_model": model, "registered_agents": sorted(ROLES)}
    if model == "role_v1":
        coordination["agent_roles"] = dict(ROLES)
    goal = {
        "id": GOAL, "status": "active", "repo": str(home), "state_file": state.name,
        "repos": [{"name": "api", "path": str(api), "default_branch": "main", "merge_target": "task_branch"}],
        "coordination": coordination,
    }
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"schema_version": 1, "common_runtime_root": str(runtime), "goals": [goal]}),
                        encoding="utf-8")
    return {"registry": registry, "runtime": runtime, "goal": goal, "api": api}


def _deliver_new_todo(fx: dict, *, content: str = "feature\n") -> tuple[str, str]:
    """Add a todo, commit work on its branch and deliver it; returns (todo_id, sha)."""

    added = add_goal_todo(registry_path=fx["registry"], goal_id=GOAL, role="agent", text="Build the feature",
                          task_class="advancement_task", claimed_by="dev",
                          validation_command_json=json.dumps(["true"]),
                          role_contract={"task_repositories": ["api"]})
    todo_id = str(added["todo_id"])
    worktree = Path(git_workspace.prepare(fx["goal"], todo_id, None, fx["runtime"])["paths"]["api"])
    (worktree / "feature.txt").write_text(content, encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-qm", "feature")
    delivered = complete_goal_todo(registry_path=fx["registry"], goal_id=GOAL, todo_id=todo_id, role="agent",
                                   agent_id="dev", evidence="built")
    assert delivered.get("in_review") is True, delivered
    return todo_id, git(worktree, "rev-parse", "HEAD")


def _todo(fx: dict, todo_id: str) -> dict:
    return next(row for row in list_goal_todos(registry_path=fx["registry"], goal_id=GOAL)["todos"]
                if row["todo_id"] == todo_id)


def _events(fx: dict, kind: str) -> list[dict]:
    path = rollout_event_log_path(fx["runtime"], GOAL)
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if row.get("event_kind") == kind]


def _cli(fx: dict, *argv: str) -> tuple[int, dict]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli_main(["--registry", str(fx["registry"]), "--runtime-root", str(fx["runtime"]),
                         "--format", "json", *argv])
    return code, json.loads(buffer.getvalue())


def _block(fx: dict, todo_id: str, reason: str = "the test toolchain is not installed") -> str:
    blocked = block_goal_todo_review(registry_path=fx["registry"], goal_id=GOAL, todo_id=todo_id,
                                     agent_id="acc", reason=reason)
    return str(blocked["acceptance"]["gate_todo_id"])


# --- delivery, review checkout and merge ----------------------------------------------


def test_review_checkout_at_the_delivered_sha_is_isolated_from_the_todo_branch(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    todo_id, sha = _deliver_new_todo(fx)
    todo = _todo(fx, todo_id)
    assert parse_delivered_shas(todo["evidence"]) == {"api": sha}

    prepared = prepare_acceptor_review(fx["goal"], todo, fx["runtime"], attempt="run-1")
    assert prepared["ok"] is True, prepared
    checkout = Path(prepared["cwd"])
    assert checkout == review_checkout_root(fx["runtime"], GOAL, todo_id, "run-1") / "api"
    assert git(checkout, "rev-parse", "HEAD") == sha
    assert git(checkout, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"  # detached

    # Whatever the acceptor does there never reaches the todo branch.
    (checkout / "feature.txt").write_text("acceptor edit\n", encoding="utf-8")
    git(checkout, "commit", "-qam", "acceptor commit")
    (checkout / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    branch = f"loopx/{GOAL}/{todo_id}"
    assert git(fx["api"], "rev-parse", branch) == sha

    run = {"agent_id": "acc", "todo_id": todo_id, "turn_instance_id": "dispatch-1",
           "review_checkout": review_run_record(prepared)}
    settled = settle_acceptor_review(fx["goal"], run, fx["runtime"])
    assert settled == {"modified": True, "removed": True, "repos": ["api"]}
    warning = _events(fx, "acceptor_modified_review_checkout")
    assert len(warning) == 1 and warning[0]["todo_id"] == todo_id
    assert warning[0]["details"]["new_commit_repos"] == "api"
    assert warning[0]["details"]["uncommitted_file_count"] == 1
    assert not checkout.exists()
    assert str(checkout) not in git(fx["api"], "worktree", "list")
    # Removal is idempotent; an untouched checkout raises no warning.
    assert settle_acceptor_review(fx["goal"], run, fx["runtime"])["removed"] is True
    clean = prepare_acceptor_review(fx["goal"], todo, fx["runtime"], attempt="run-2")
    quiet = settle_acceptor_review(fx["goal"], {**run, "review_checkout": review_run_record(clean)}, fx["runtime"])
    assert quiet["modified"] is False and len(_events(fx, "acceptor_modified_review_checkout")) == 1

    # The accept merges the delivered sha, not the acceptor's commit.
    accepted = accept_goal_todo(registry_path=fx["registry"], goal_id=GOAL, todo_id=todo_id, agent_id="acc")
    assert accepted["merge"]["ok"] is True and _todo(fx, todo_id)["status"] == "done"
    target = f"loopx-task/{GOAL}"
    assert git(fx["api"], "show", f"{target}:feature.txt") == "feature"
    assert git(fx["api"], "merge-base", "--is-ancestor", sha, target) == ""


def test_merge_uses_the_delivered_sha_and_a_moved_branch_blocks(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    todo_id, sha = _deliver_new_todo(fx)
    worktree = git_workspace.todo_workspace_root(fx["runtime"], GOAL, todo_id) / "api"
    (worktree / "late.txt").write_text("after delivery\n", encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-qm", "late change")

    blocked = accept_goal_todo(registry_path=fx["registry"], goal_id=GOAL, todo_id=todo_id, agent_id="acc")
    acceptance = blocked["acceptance"]
    assert (acceptance["transition"], acceptance["reason"]) == ("merge_blocked", "delivery_moved")
    blocker = blocked["merge"]["conflict_report"]["repos"][0]["blockers"][0]
    assert blocker["error_code"] == "delivery_moved" and blocker["delivered_sha"] == sha
    todo = _todo(fx, todo_id)
    assert todo["status"] == "open" and not todo.get("reject_count")
    assert "moved after delivery" in todo["review_feedback"]
    assert git(fx["api"], "rev-parse", "--verify", "-q", f"loopx-task/{GOAL}") != git(worktree, "rev-parse", "HEAD")

    # Delivering again records the new tip, which then merges.
    redelivered = complete_goal_todo(registry_path=fx["registry"], goal_id=GOAL, todo_id=todo_id, role="agent",
                                     agent_id="dev", evidence="rebuilt")
    new_sha = git(worktree, "rev-parse", "HEAD")
    assert redelivered["acceptance"]["delivery_refs"]["delivered_shas"] == f"api@{new_sha}"
    accepted = accept_goal_todo(registry_path=fx["registry"], goal_id=GOAL, todo_id=todo_id, agent_id="acc")
    assert accepted["merge"]["ok"] is True
    assert git(fx["api"], "show", f"loopx-task/{GOAL}:late.txt") == "after delivery"


# --- reject and blocked --------------------------------------------------------------


def test_reject_without_feedback_is_refused_and_a_blank_turn_reject_becomes_blocked(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    todo_id, _sha = _deliver_new_todo(fx)
    with pytest.raises(ValueError, match="failed acceptance criterion"):
        reject_goal_todo(registry_path=fx["registry"], goal_id=GOAL, todo_id=todo_id, agent_id="acc", note="  ")
    code, payload = _cli(fx, "todo", "reject", "--goal-id", GOAL, "--todo-id", todo_id, "--agent-id", "acc")
    assert code == 1 and "requires --note" in payload["error"]

    assert reject_delivery_from_turn(registry_path=fx["registry"], goal_id=GOAL, todo_id=todo_id,
                                     agent_id="acc", feedback=" \n ") is True
    todo = _todo(fx, todo_id)
    assert todo["status"] == "in_review" and not todo.get("reject_count")
    assert blocked_review_todo_ids(fx["registry"], fx["runtime"], GOAL) == {todo_id}
    assert len(_events(fx, "todo_review_blocked")) == 1

    # A reject naming the failed criterion still counts.
    assert reject_delivery_from_turn(registry_path=fx["registry"], goal_id=GOAL, todo_id=todo_id,
                                     agent_id="acc", feedback="criterion 2 fails: no pagination") is True
    assert _todo(fx, todo_id)["reject_count"] == 1


def test_blocked_opens_one_gate_keeps_the_count_and_blocks_the_acceptor_lane(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    todo_id, _sha = _deliver_new_todo(fx)
    code, payload = _cli(fx, "todo", "block-review", "--goal-id", GOAL, "--todo-id", todo_id,
                         "--agent-id", "acc", "--reason", "npm registry unreachable")
    assert code == 0, payload
    acceptance = payload["acceptance"]
    assert (acceptance["verdict"], acceptance["transition"]) == ("blocked", "review_blocked")
    gate_id = acceptance["gate_todo_id"]
    gate = _todo(fx, gate_id)
    assert (gate["role"], gate["task_class"], gate["blocks_agent"]) == ("user", "user_gate", "acc")
    assert "npm registry unreachable" in gate["text"]
    entry = read_gate_index(fx["runtime"], GOAL)["gates"][gate_id]
    assert (entry["kind"], entry["review_todo_id"], entry["acceptor_agent"]) == ("acceptor_blocked", todo_id, "acc")
    assert entry["options"] == list(REVIEW_GATE_OPTIONS)
    todo = _todo(fx, todo_id)
    assert todo["status"] == "in_review" and not todo.get("reject_count")

    # Idempotent: a second blocked verdict reuses the open gate.
    again = block_goal_todo_review(registry_path=fx["registry"], goal_id=GOAL, todo_id=todo_id,
                                   agent_id="acc", reason="still broken")
    assert again["acceptance"]["gate_todo_id"] == gate_id and again["acceptance"]["idempotent_replay"]
    assert len([row for row in list_goal_todos(registry_path=fx["registry"], goal_id=GOAL, role="user")["todos"]
                if row["status"] == "open"]) == 1
    # Only the resolved acceptor records it, and it needs a reason.
    with pytest.raises(ValueError, match="cannot block-review"):
        block_goal_todo_review(registry_path=fx["registry"], goal_id=GOAL, todo_id=todo_id, agent_id="dev",
                               reason="x")
    code, payload = _cli(fx, "todo", "block-review", "--goal-id", GOAL, "--todo-id", todo_id, "--agent-id", "acc")
    assert code == 1 and "--reason" in payload["error"]

    # The gate blocks the acceptor's lane, not the developer's or the orchestrator's.
    from loopx.control_plane.agents.identity import build_quota_agent_identity
    from loopx.control_plane.todos.quota_summary import summarize_user_todos_for_quota

    users = list_goal_todos(registry_path=fx["registry"], goal_id=GOAL, role="user")["user_todos"]
    for agent, blocking in (("acc", 1), ("dev", 0), ("orch", 0)):
        identity = build_quota_agent_identity(fx["goal"], agent_id=agent)
        summary = summarize_user_todos_for_quota(users, agent_identity=identity, filter_user_gate_blocks_agent=True)
        assert summary["open_count"] == blocking, agent
    assert blocked_review_todo_ids(fx["registry"], fx["runtime"], GOAL) == {todo_id}


def test_an_acceptor_user_action_required_turn_is_the_blocked_verdict(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    todo_id, _sha = _deliver_new_todo(fx)
    turn_key = "sha256:" + "a" * 64
    journal = fx["runtime"] / "goals" / GOAL / "turns" / f"{'a' * 64}.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({"schema_version": "loopx_turn_journal_v0",
                                   "host_result": {"summary": "pytest is missing in the review env"}}))
    payload = {"status": "stopped", "result_kind": "user_action_required", "resume_turn_key": turn_key}
    selected = {"todo_id": todo_id, "status": "in_review"}
    verdict = settle_turn_stop_verdict(payload, registry_path=fx["registry"], runtime_root=fx["runtime"],
                                       runtime_root_arg=None, goal_id=GOAL, agent_id="acc", selected_todo=selected)
    assert verdict["verdict"] == "blocked" and "pytest is missing" in verdict["reason"]
    assert "pytest is missing" in _todo(fx, verdict["gate_todo_id"])["text"]
    # Other stops, other agents and other todos are untouched.
    assert settle_turn_stop_verdict({**payload, "result_kind": "wait"}, registry_path=fx["registry"],
                                    runtime_root=fx["runtime"], runtime_root_arg=None, goal_id=GOAL,
                                    agent_id="acc", selected_todo=selected) is None
    assert settle_turn_stop_verdict(payload, registry_path=fx["registry"], runtime_root=fx["runtime"],
                                    runtime_root_arg=None, goal_id=GOAL, agent_id="dev",
                                    selected_todo=selected) is None


# --- gate options ---------------------------------------------------------------------


def _assert_option_applied(fx: dict, todo_id: str, sha: str, gate_id: str, option: str) -> None:
    gate = _todo(fx, gate_id)
    assert gate["status"] == "done"
    assert read_gate_index(fx["runtime"], GOAL)["gates"][gate_id]["decision_option"] == option
    todo = _todo(fx, todo_id)
    assert not todo.get("reject_count")
    assert blocked_review_todo_ids(fx["registry"], fx["runtime"], GOAL) == set()
    target = f"loopx-task/{GOAL}"
    if option == "retry_acceptance":
        assert todo["status"] == "in_review"
    elif option == "accept_manually":
        assert todo["status"] == "done" and "accepted_by=owner" in todo["evidence"]
        assert git(fx["api"], "merge-base", "--is-ancestor", sha, target) == ""
    elif option == "return_to_developer":
        assert todo["status"] == "open" and todo["claimed_by"] == "dev"
        assert "install the toolchain first" in todo["review_feedback"]
    else:
        assert todo["status"] == "done" and todo.get("note") == "superseded"
        assert git(fx["api"], "rev-parse", "--verify", "-q", target) == git(fx["api"], "rev-parse", "main")
    [decided] = _events(fx, "review_gate_decided")
    assert decided["status"] == option and decided["details"]["applied"] is True


@pytest.mark.parametrize("option", REVIEW_GATE_OPTIONS)
def test_each_gate_option_applies_via_the_cli(tmp_path, monkeypatch, option: str) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    todo_id, sha = _deliver_new_todo(fx)
    gate_id = _block(fx, todo_id)
    code, payload = _cli(fx, "gate", "resolve", "--goal-id", GOAL, "--todo-id", gate_id, "--option", option,
                         "--note", "install the toolchain first")
    assert code == 0, payload
    assert payload["review_gate"]["option"] == option and payload["review_gate"]["applied"] is True
    _assert_option_applied(fx, todo_id, sha, gate_id, option)


@pytest.mark.parametrize("option", REVIEW_GATE_OPTIONS)
def test_each_gate_option_applies_via_the_web_resolve_path(tmp_path, monkeypatch, option: str) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    todo_id, sha = _deliver_new_todo(fx)
    gate_id = _block(fx, todo_id)
    service = ChatActionService(store=ChatActionStore(tmp_path / "actions"), registry_path=fx["registry"])
    proposal = service.preview({
        "action_kind": "gate.resolve", "summary": f"{option} the blocked review",
        "normalized_parameters": {"goal_id": GOAL, "todo_id": gate_id, "option": option,
                                  "note": "install the toolchain first"},
        "context": {}, "idempotency_key": f"g12-{option}",
    })
    assert proposal["status"] == "preview_ready", proposal
    applied = service.apply(proposal["proposal_id"])["proposal"]
    assert applied["status"] == "applied", applied
    assert applied["receipt"]["decision_option"] == option
    _assert_option_applied(fx, todo_id, sha, gate_id, option)


def test_plain_decisions_map_to_options_and_mismatches_are_refused(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    todo_id, _sha = _deliver_new_todo(fx)
    gate_id = _block(fx, todo_id)
    code, payload = _cli(fx, "gate", "resolve", "--goal-id", GOAL, "--todo-id", gate_id,
                         "--decision", "reject", "--option", "accept_manually")
    assert code == 1 and "records decision approve" in payload["error"]
    assert _todo(fx, gate_id)["status"] == "open"
    # `todo complete --role user --decision-outcome reject` returns the todo to its developer.
    code, payload = _cli(fx, "todo", "complete", "--goal-id", GOAL, "--todo-id", gate_id, "--role", "user",
                         "--decision-outcome", "reject", "--agent-id", "acc", "--note", "fix env")
    assert code == 0, payload
    assert payload["review_gate"]["option"] == "return_to_developer"
    assert _todo(fx, todo_id)["status"] == "open"


def test_accept_manually_is_refused_when_the_todo_left_review(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    todo_id, _sha = _deliver_new_todo(fx)
    gate_id = _block(fx, todo_id)
    reject_goal_todo(registry_path=fx["registry"], goal_id=GOAL, todo_id=todo_id, agent_id="acc",
                     note="criterion 1 fails")
    code, payload = _cli(fx, "gate", "resolve", "--goal-id", GOAL, "--todo-id", gate_id,
                         "--option", "accept_manually")
    assert code == 1 and "not in_review" in payload["error"]
    assert _todo(fx, gate_id)["status"] == "open"


def test_an_option_on_an_ordinary_gate_is_refused(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    gate = add_goal_todo(registry_path=fx["registry"], goal_id=GOAL, role="user", text="Pick a database",
                         task_class="user_gate", blocks_agent="orch")
    code, payload = _cli(fx, "gate", "resolve", "--goal-id", GOAL, "--todo-id", gate["todo_id"],
                         "--option", "retry_acceptance")
    assert code == 1 and "only to an acceptor-blocked gate" in payload["error"]
    code, payload = _cli(fx, "gate", "resolve", "--goal-id", GOAL, "--todo-id", gate["todo_id"],
                         "--decision", "approve")
    assert code == 0 and _todo(fx, gate["todo_id"])["status"] == "done"


# --- peer_v1 ---------------------------------------------------------------------------


def test_peer_v1_goals_are_unaffected(tmp_path, monkeypatch) -> None:
    fx = _fixture(tmp_path, monkeypatch, model="peer_v1")
    added = add_goal_todo(registry_path=fx["registry"], goal_id=GOAL, role="agent", text="Build it",
                          task_class="advancement_task", claimed_by="dev",
                          role_contract={"task_repositories": ["api"]}, validation_command_json='["true"]')
    todo_id = str(added["todo_id"])
    git_workspace.prepare(fx["goal"], todo_id, None, fx["runtime"])
    completed = complete_goal_todo(registry_path=fx["registry"], goal_id=GOAL, todo_id=todo_id, role="agent",
                                   agent_id="dev", evidence="built")
    assert completed.get("in_review") is not True
    todo = _todo(fx, todo_id)
    assert todo["status"] == "done" and "delivered_shas=" not in (todo.get("evidence") or "")
    with pytest.raises(ValueError, match="role_v1"):
        block_goal_todo_review(registry_path=fx["registry"], goal_id=GOAL, todo_id=todo_id, agent_id="acc",
                               reason="x")
    assert reject_delivery_from_turn(registry_path=fx["registry"], goal_id=GOAL, todo_id=todo_id,
                                     agent_id="acc", feedback="") is False
    assert settle_turn_stop_verdict(
        {"status": "stopped", "result_kind": "user_action_required"}, registry_path=fx["registry"],
        runtime_root=fx["runtime"], runtime_root_arg=None, goal_id=GOAL, agent_id="acc",
        selected_todo={"todo_id": todo_id, "status": "in_review"},
    ) is None
    assert _events(fx, "todo_review_blocked") == []


# --- role board ------------------------------------------------------------------------


def test_role_board_flags_a_modified_review_checkout_and_the_blocked_gate(tmp_path, monkeypatch) -> None:
    from loopx.control_plane.status.role_board_projection import build_goal_role_board

    fx = _fixture(tmp_path, monkeypatch)
    todo_id, _sha = _deliver_new_todo(fx)
    gate_id = _block(fx, todo_id)
    todos = list_goal_todos(registry_path=fx["registry"], goal_id=GOAL)["todos"]
    dispatcher = {"available": True, "runs": [], "review_warnings": {f"{GOAL}/{todo_id}": {"agent_id": "acc"}}}
    board = build_goal_role_board(goal=fx["goal"], todos=todos, runtime_root=fx["runtime"], dispatcher=dispatcher)
    card = next(card for card in board["todos"] if card["todo_id"] == todo_id)
    assert card["review_checkout_modified"] is True
    assert card["review_blocked_gate_todo_id"] == gate_id
    gate = next(gate for gate in board["gates"] if gate["todo_id"] == gate_id)
    assert gate["kind"] == "acceptor_blocked" and gate["review_todo_id"] == todo_id
    assert gate["options"] == list(REVIEW_GATE_OPTIONS)
