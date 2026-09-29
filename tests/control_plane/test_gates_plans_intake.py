"""Fork slice S6: gate discussion threads, orchestrator-only gates, plan cards, goal intake."""
from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from canonical_authority_fixture import initialize_canonical_authority

from loopx.chat_action_store import ChatActionStore
from loopx.chat_actions import ChatActionService
from loopx.cli import main
from loopx.control_plane.coordination.runtime_shadow import build_todo_runtime_shadow_projection
from loopx.gate_threads import (
    AWAITING_ORCHESTRATOR,
    AWAITING_USER,
    GateThreadError,
    gate_view,
    gates_awaiting_orchestrator,
    list_gates,
    read_gate_index,
    read_gate_thread,
    render_gate_markdown,
    reply_to_gate,
)
from loopx.plan_cards import PlanCardError, apply_plan, plan_path, propose_plan, read_plan, render_plan_markdown
from loopx.rollout_event_log import load_rollout_events, rollout_event_log_path
from loopx.todos import add_goal_todo, complete_goal_todo, list_goal_todos

GOAL = "goal-a"
ORCH, DEV, ACC = "orch", "dev", "acc"


def fixture(tmp_path: Path, provider: str | None = None, *, agent_model: str = "role_v1") -> tuple[Path, Path]:
    project = tmp_path / "project"
    project.mkdir(parents=True)
    for repo in ("api", "web"):
        (tmp_path / repo).mkdir()
    state = project / "ACTIVE_GOAL_STATE.md"
    state.write_text("# Goal\n\n## User Todo\n\n## Agent Todo\n\n## Completed Work Archive\n", encoding="utf-8")
    runtime = tmp_path / "runtime"
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"schema_version": 1, "common_runtime_root": str(runtime), "goals": [{
        "id": GOAL, "status": "active", "repo": str(project), "state_file": state.name,
        "repos": [{"name": "api", "path": str(tmp_path / "api")}, {"name": "web", "path": str(tmp_path / "web")}],
        "coordination": {"agent_model": agent_model, "registered_agents": [ORCH, DEV, ACC],
                         "agent_roles": {ORCH: "orchestrator", DEV: "developer", ACC: "acceptor"}},
    }]}), encoding="utf-8")
    if provider:
        # Seed one todo so the canonical projection is non-trivial.
        add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text="Seed work", claimed_by=DEV)
        todos = list_goal_todos(registry_path=registry, goal_id=GOAL)["todos"]
        projection = build_todo_runtime_shadow_projection(goal_id=GOAL, todos=todos, handoff_mode="soft_claim")
        initialize_canonical_authority(runtime, GOAL, projection, state_path=state, provider=provider)
    return registry, runtime


def open_gate(registry: Path, text: str = "Which database?") -> str:
    gate = add_goal_todo(registry_path=registry, goal_id=GOAL, role="user", task_class="user_gate",
                         agent_id=ORCH, text=text)
    return str(gate["todo_id"])


def rows(registry: Path) -> dict[str, dict]:
    return {row["todo_id"]: row for row in list_goal_todos(registry_path=registry, goal_id=GOAL)["todos"]}


PLAN = {
    "title": "Todo app v1",
    "summary": "Contract first, then API and web, then integration.",
    "todos": [
        {"key": "contract", "text": "Write the API contract", "bound_agent": DEV, "acceptor_agent": ACC,
         "task_repositories": ["api"], "acceptance": "openapi covers CRUD",
         "validation_command": "test -f openapi.yaml", "estimated_effort": "1h", "priority": "P1"},
        {"key": "api", "text": "Implement the API", "bound_agent": DEV, "depends_on": ["contract"],
         "task_repositories": ["api"]},
        {"key": "web", "text": "Implement the web UI", "depends_on": ["contract"], "task_repositories": ["web"]},
        {"key": "integrate", "text": "Integrate web and API", "depends_on": ["api", "web"],
         "task_repositories": ["api", "web"], "requires_acceptance": True},
        {"key": "readme", "text": "Write the README", "requires_acceptance": False},
    ],
}


# --- gate threads --------------------------------------------------------------


def test_thread_appends_in_order_with_authorship_and_flags(tmp_path: Path) -> None:
    registry, runtime = fixture(tmp_path)
    gate_id = open_gate(registry)
    assert gate_view(registry_path=registry, runtime_root=runtime, goal_id=GOAL, todo_id=gate_id)["awaiting"] == AWAITING_USER

    first = reply_to_gate(registry_path=registry, runtime_root=runtime, goal_id=GOAL, todo_id=gate_id,
                          text="What are the tradeoffs?")
    assert first["awaiting"] == AWAITING_ORCHESTRATOR
    assert gates_awaiting_orchestrator(runtime, GOAL) == [gate_id]
    assert [row["todo_id"] for row in list_gates(registry_path=registry, runtime_root=runtime, goal_id=GOAL,
                                                 awaiting=AWAITING_ORCHESTRATOR)["gates"]] == [gate_id]
    second = reply_to_gate(registry_path=registry, runtime_root=runtime, goal_id=GOAL, todo_id=gate_id,
                           text="SQLite is simpler. Recommend SQLite.", author="orchestrator", agent_id=ORCH)
    assert second["awaiting"] == AWAITING_USER
    assert gates_awaiting_orchestrator(runtime, GOAL) == []
    reply_to_gate(registry_path=registry, runtime_root=runtime, goal_id=GOAL, todo_id=gate_id, text="OK, but why?")

    view = gate_view(registry_path=registry, runtime_root=runtime, goal_id=GOAL, todo_id=gate_id)
    assert [(m["seq"], m["author"], m["agent_id"]) for m in view["messages"]] == [
        (1, "user", None), (2, "orchestrator", ORCH), (3, "user", None)]
    assert view["messages"][1]["text"] == "SQLite is simpler. Recommend SQLite."
    assert view["awaiting"] == AWAITING_ORCHESTRATOR and view["status"] == "open"
    assert len({m["message_id"] for m in view["messages"]}) == 3

    # Every reply is a durable event; the event carries state, never the text.
    events = [e for e in load_rollout_events(rollout_event_log_path(runtime, GOAL))
              if e["event_kind"] == "gate_thread_reply"]
    assert [e["state_transition"]["to_state"] for e in events] == [
        AWAITING_ORCHESTRATOR, AWAITING_USER, AWAITING_ORCHESTRATOR]
    assert all(e["todo_id"] == gate_id for e in events)
    assert "tradeoffs" not in json.dumps(events)


def test_thread_authorization_and_closed_gate(tmp_path: Path) -> None:
    registry, runtime = fixture(tmp_path)
    gate_id = open_gate(registry)
    kwargs = {"registry_path": registry, "runtime_root": runtime, "goal_id": GOAL, "todo_id": gate_id}
    with pytest.raises(GateThreadError, match="not the orchestrator"):
        reply_to_gate(**kwargs, text="hi", author="orchestrator", agent_id=DEV)
    with pytest.raises(GateThreadError, match="require --agent-id"):
        reply_to_gate(**kwargs, text="hi", author="orchestrator")
    with pytest.raises(GateThreadError, match="owner actions"):
        reply_to_gate(**kwargs, text="hi", agent_id=DEV)
    with pytest.raises(GateThreadError, match="must not be empty"):
        reply_to_gate(**kwargs, text="   ")
    agent_todo = add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text="Some work")
    with pytest.raises(GateThreadError, match="not a user gate"):
        reply_to_gate(**{**kwargs, "todo_id": agent_todo["todo_id"]}, text="hi")
    assert read_gate_thread(runtime, GOAL, gate_id) == []

    reply_to_gate(**kwargs, text="Please decide")
    done = complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=gate_id, role="user",
                              decision_outcome="approve", note="SQLite", no_followup=True, agent_id=ORCH)
    assert done["ok"] is True
    assert read_gate_index(runtime, GOAL)["gates"][gate_id]["awaiting"] == "closed"
    assert gates_awaiting_orchestrator(runtime, GOAL) == []
    with pytest.raises(GateThreadError, match="read-only"):
        reply_to_gate(**kwargs, text="too late")
    view = gate_view(**kwargs)
    assert (view["awaiting"], view["decision_outcome"], len(view["messages"])) == ("closed", "approve", 1)


def test_cli_gate_reply_and_show(tmp_path: Path, capsys) -> None:
    registry, runtime = fixture(tmp_path)
    gate_id = open_gate(registry)
    base = ["--registry", str(registry), "--runtime-root", str(runtime), "--format", "json", "gate"]
    assert main([*base, "reply", "--goal-id", GOAL, "--todo-id", gate_id, "--text", "Question?"]) == 0
    assert json.loads(capsys.readouterr().out)["awaiting"] == AWAITING_ORCHESTRATOR
    assert main([*base, "reply", "--goal-id", GOAL, "--todo-id", gate_id, "--text", "Answer.",
                 "--as", "orchestrator", "--agent-id", DEV]) == 1
    assert "not the orchestrator" in json.loads(capsys.readouterr().out)["error"]
    assert main([*base, "reply", "--goal-id", GOAL, "--todo-id", gate_id, "--text", "Answer.",
                 "--as", "orchestrator", "--agent-id", ORCH]) == 0
    capsys.readouterr()
    assert main([*base, "show", "--goal-id", GOAL, "--todo-id", gate_id]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert [m["author"] for m in shown["messages"]] == ["user", "orchestrator"]
    assert shown["awaiting"] == AWAITING_USER


# --- decision 11: only the orchestrator opens user gates ----------------------------


@pytest.mark.parametrize("agent", [DEV, ACC])
def test_non_orchestrator_gate_creation_is_refused(tmp_path: Path, agent: str) -> None:
    registry, _runtime = fixture(tmp_path)
    before = rows(registry)
    with pytest.raises(ValueError, match="only the goal orchestrator opens user gates") as raised:
        add_goal_todo(registry_path=registry, goal_id=GOAL, role="user", task_class="user_gate",
                      agent_id=agent, text="Pick a database")
    assert "--task-class blocker" in str(raised.value)
    work = add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text="Build it", claimed_by=agent)
    with pytest.raises(ValueError, match="only the goal orchestrator opens user gates"):
        complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=work["todo_id"], role="agent",
                           evidence="done", next_user_todo="Please pick a database", agent_id=agent)
    assert set(rows(registry)) == set(before) | {work["todo_id"]}
    # The supported route: a blocker addressed to the orchestrator.
    blocker = add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", task_class="blocker",
                            text="Which database should the API use?")
    assert blocker["ok"] is True


def test_orchestrator_owner_and_peer_v1_may_open_gates(tmp_path: Path) -> None:
    registry, _runtime = fixture(tmp_path)
    assert open_gate(registry, "Orchestrator gate")
    owner = add_goal_todo(registry_path=registry, goal_id=GOAL, role="user", task_class="user_gate",
                          blocks_agent=DEV, text="Owner gate")
    assert owner["ok"] is True
    peer_registry, _ = fixture(tmp_path / "peer", agent_model="peer_v1")
    peer = add_goal_todo(registry_path=peer_registry, goal_id=GOAL, role="user", task_class="user_gate",
                         agent_id=DEV, text="Peer gate")
    assert peer["ok"] is True


# --- plan cards --------------------------------------------------------------------


def _propose(registry: Path, runtime: Path, plan: dict | None = None) -> dict:
    return propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=ORCH,
                        plan=plan or PLAN)["plan"]


@pytest.mark.parametrize("provider", [None, "file", "sqlite"])
def test_plan_propose_approve_applies_todos_exactly_once(tmp_path: Path, provider: str | None, monkeypatch) -> None:
    if provider == "sqlite":
        from canonical_authority_fixture import isolate_sqlite_runtime
        isolate_sqlite_runtime(tmp_path, monkeypatch)
    registry, runtime = fixture(tmp_path, provider)
    before = set(rows(registry))
    plan = _propose(registry, runtime)
    assert plan["status"] == "pending"
    gate_id = plan["gate_todo_id"]
    gate = rows(registry)[gate_id]
    assert (gate["role"], gate["task_class"], gate["status"]) == ("user", "user_gate", "open")
    assert set(rows(registry)) == before | {gate_id}  # nothing applied yet
    view = gate_view(registry_path=registry, runtime_root=runtime, goal_id=GOAL, todo_id=gate_id)
    assert (view["kind"], view["plan_id"], view["awaiting"]) == ("plan_approval", plan["plan_id"], AWAITING_USER)
    # Re-proposing the identical plan reuses the pending card.
    assert _propose(registry, runtime)["plan_id"] == plan["plan_id"]

    done = complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=gate_id, role="user",
                              decision_outcome="approve", note="Go", no_followup=True, agent_id=ORCH)
    assert done["ok"] is True, done
    assert done["plan_card"]["status"] == "applied", done["plan_card"]
    ids = done["plan_card"]["todo_id_map"]
    assert list(ids) == ["contract", "api", "web", "integrate", "readme"]
    state = rows(registry)
    assert set(state) == before | {gate_id} | set(ids.values())
    contract, api, integrate, readme = (state[ids[key]] for key in ("contract", "api", "integrate", "readme"))
    assert contract["status"] == "open" and contract["claimed_by"] == DEV
    assert (contract["required_role"], contract["acceptor_agent"], contract["task_repositories"]) == ("developer", ACC, ["api"])
    # G2: the plan's acceptance lands in the orchestrator-owned field, not the note.
    assert contract["acceptance_criteria"] == "openapi covers CRUD"
    assert "Acceptance:" not in contract["note"] and "Estimate: 1h" in contract["note"]
    assert "acceptance_criteria" not in api
    assert (api["status"], api["resume_when"]) == ("deferred", f"todo_done:{ids['contract']}")
    assert (integrate["resume_when"], integrate["requires_acceptance"]) == (f"todo_done:{ids['web']}", True)
    assert readme["requires_acceptance"] is False and readme["status"] == "open"

    # Exactly once: a retry and a second settlement are no-ops.
    again = apply_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, plan_id=plan["plan_id"])
    assert again["already_applied"] is True
    assert rows(registry) == state
    assert read_plan(runtime, GOAL, plan["plan_id"])["todo_id_map"] == ids


@pytest.mark.parametrize("decision", ["reject", "cancel"])
def test_plan_reject_or_cancel_applies_nothing(tmp_path: Path, decision: str) -> None:
    registry, runtime = fixture(tmp_path)
    plan = _propose(registry, runtime)
    before = set(rows(registry))
    done = complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=plan["gate_todo_id"], role="user",
                              decision_outcome=decision, note="No", no_followup=True, agent_id=ORCH)
    assert done["ok"] is True
    assert done["plan_card"]["status"] == {"reject": "rejected", "cancel": "cancelled"}[decision]
    assert set(rows(registry)) == before
    with pytest.raises(PlanCardError, match="nothing to apply"):
        apply_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, plan_id=plan["plan_id"])


def test_plan_approve_through_dashboard_gate_resolve(tmp_path: Path) -> None:
    registry, runtime = fixture(tmp_path)
    plan = _propose(registry, runtime)
    service = ChatActionService(store=ChatActionStore(tmp_path / "actions"), registry_path=registry)
    proposal = service.preview({"action_kind": "gate.resolve", "summary": "approve plan", "context": {},
        "idempotency_key": "plan-gate", "normalized_parameters": {
            "goal_id": GOAL, "todo_id": plan["gate_todo_id"], "decision": "approve", "note": "Ship"}})
    applied = service.apply(proposal["proposal_id"])["proposal"]
    assert applied["status"] == "applied", applied
    record = read_plan(runtime, GOAL, plan["plan_id"])
    assert record["status"] == "applied" and len(record["todo_id_map"]) == 5


def _dashboard_plan_approve(tmp_path: Path, registry: Path, gate_id: str) -> tuple[ChatActionService, str]:
    service = ChatActionService(store=ChatActionStore(tmp_path / "actions"), registry_path=registry)
    proposal = service.preview({"action_kind": "gate.resolve", "summary": "approve plan", "context": {},
        "idempotency_key": "plan-gate", "normalized_parameters": {
            "goal_id": GOAL, "todo_id": gate_id, "decision": "approve", "note": "Ship"}})
    assert proposal["status"] == "preview_ready", proposal
    return service, str(proposal["proposal_id"])


@pytest.mark.parametrize("provider", [None, "file"])
def test_dashboard_plan_approve_reports_and_recovers_an_interrupted_plan_apply(
    tmp_path: Path, provider: str | None, monkeypatch,
) -> None:
    import loopx.plan_cards as plan_cards
    import loopx.todos as todos

    registry, runtime = fixture(tmp_path, provider)
    plan = _propose(registry, runtime)
    before = set(rows(registry))
    service, proposal_id = _dashboard_plan_approve(tmp_path, registry, plan["gate_todo_id"])
    with monkeypatch.context() as patch:
        if provider:  # canonical: crash at the third plan todo, after two were created
            real_add, calls = todos.add_goal_todo, []

            def crashing_add(**kwargs):
                if str(kwargs.get("operation_id") or "").startswith("plan-"):
                    calls.append(kwargs["operation_id"])
                    if len(calls) == 3:
                        raise OSError("Synthetic crash during the plan apply")
                return real_add(**kwargs)

            patch.setattr(todos, "add_goal_todo", crashing_add)
        else:  # Markdown: the batch is all or nothing
            def crashing_batch(*args, **kwargs):
                raise OSError("Synthetic crash during the plan apply")

            patch.setattr(plan_cards, "_apply_legacy_batch", crashing_batch)
        failed = service.apply(proposal_id)["proposal"]
    assert failed["status"] == "failed", failed
    failure = failed["failure"]
    assert failure["error_code"] == "plan_apply_recovery_required", failure
    assert failure["details"]["plan_id"] == plan["plan_id"]
    assert failure["details"]["recovery"] == f"loopx plan apply --goal-id {GOAL} --plan-id {plan['plan_id']}"
    assert read_plan(runtime, GOAL, plan["plan_id"])["status"] == "applying"
    assert rows(registry)[plan["gate_todo_id"]]["status"] == "done"

    recovered = service.apply(proposal_id)["proposal"]
    assert recovered["status"] == "applied", recovered
    record = read_plan(runtime, GOAL, plan["plan_id"])
    assert record["status"] == "applied" and len(record["todo_id_map"]) == 5
    assert set(rows(registry)) == before | set(record["todo_id_map"].values()), "each plan todo exactly once"
    assert service.apply(proposal_id)["proposal"]["receipt"] == recovered["receipt"]


@pytest.mark.parametrize("provider", [None, "file"])
def test_dashboard_plan_approve_settles_after_a_crash_between_closure_and_settlement(
    tmp_path: Path, provider: str | None, monkeypatch,
) -> None:
    import loopx.plan_cards as plan_cards

    registry, runtime = fixture(tmp_path, provider)
    plan = _propose(registry, runtime)
    before = set(rows(registry))
    service, proposal_id = _dashboard_plan_approve(tmp_path, registry, plan["gate_todo_id"])
    with monkeypatch.context() as patch:
        def crash(**kwargs):
            raise RuntimeError("Synthetic crash after the gate closed, before its settlement")

        patch.setattr(plan_cards, "settle_gate_decision", crash)
        with pytest.raises(RuntimeError):
            service.apply(proposal_id)
    assert rows(registry)[plan["gate_todo_id"]]["status"] == "done"
    assert read_plan(runtime, GOAL, plan["plan_id"])["status"] == "pending"
    settled = service.apply(proposal_id)["proposal"]
    assert settled["status"] == "applied", settled
    record = read_plan(runtime, GOAL, plan["plan_id"])
    assert record["status"] == "applied" and len(record["todo_id_map"]) == 5
    assert set(rows(registry)) == before | set(record["todo_id_map"].values())


def test_plan_thread_carries_change_requests_and_revision(tmp_path: Path) -> None:
    registry, runtime = fixture(tmp_path)
    plan = _propose(registry, runtime)
    gate_id = plan["gate_todo_id"]
    reply_to_gate(registry_path=registry, runtime_root=runtime, goal_id=GOAL, todo_id=gate_id,
                  text="Drop the README item")
    revised_plan = {**PLAN, "todos": PLAN["todos"][:4]}
    with pytest.raises(PlanCardError, match="not the orchestrator"):
        propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=DEV,
                     plan=revised_plan, revise_plan_id=plan["plan_id"])
    revised = propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=ORCH,
                           plan=revised_plan, revise_plan_id=plan["plan_id"])["plan"]
    assert (revised["plan_id"], revised["revision"], revised["gate_todo_id"]) == (plan["plan_id"], 2, gate_id)
    view = gate_view(registry_path=registry, runtime_root=runtime, goal_id=GOAL, todo_id=gate_id)
    assert [m["author"] for m in view["messages"]] == ["orchestrator", "user", "orchestrator"]
    assert view["awaiting"] == AWAITING_USER
    done = complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=gate_id, role="user",
                              decision_outcome="approve", note="ok", no_followup=True, agent_id=ORCH)
    assert list(done["plan_card"]["todo_id_map"]) == ["contract", "api", "web", "integrate"]


def test_plan_validation_and_approve_preflight(tmp_path: Path) -> None:
    registry, runtime = fixture(tmp_path)
    with pytest.raises(PlanCardError, match="not the orchestrator"):
        propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=DEV, plan=PLAN)
    bad_order = {**PLAN, "todos": [PLAN["todos"][1], PLAN["todos"][0]]}
    with pytest.raises(PlanCardError, match="dependency order"):
        _propose(registry, runtime, bad_order)
    with pytest.raises(PlanCardError, match="not a registered agent"):
        _propose(registry, runtime, {**PLAN, "todos": [{"key": "a", "text": "A", "bound_agent": "ghost"}]})
    with pytest.raises(PlanCardError, match="unknown Goal repos"):
        _propose(registry, runtime, {**PLAN, "todos": [{"key": "a", "text": "A", "task_repositories": ["ios"]}]})
    with pytest.raises(PlanCardError, match="has role 'acceptor'"):
        _propose(registry, runtime, {**PLAN, "todos": [{"key": "a", "text": "A", "bound_agent": ACC}]})
    assert not [row for row in rows(registry).values() if row["role"] == "user"]  # no gate opened

    plan = _propose(registry, runtime)
    # The goal drops the 'web' repo after the proposal: approving must not close the gate.
    data = json.loads(registry.read_text(encoding="utf-8"))
    data["goals"][0]["repos"] = data["goals"][0]["repos"][:1]
    registry.write_text(json.dumps(data), encoding="utf-8")
    before = rows(registry)
    with pytest.raises(ValueError, match="unknown Goal repos"):
        complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=plan["gate_todo_id"], role="user",
                           decision_outcome="approve", note="Go", no_followup=True, agent_id=ORCH)
    assert rows(registry) == before
    assert read_plan(runtime, GOAL, plan["plan_id"])["status"] == "pending"


# --- agent Turns cannot decide user gates --------------------------------------------


def _cli_json(registry: Path, runtime: Path, capsys, *argv: str) -> tuple[int, dict]:
    code = main(["--registry", str(registry), "--runtime-root", str(runtime), "--format", "json", *argv])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("provider", [None, "file"])
@pytest.mark.parametrize("surface", ["gate_resolve", "todo_complete"])
def test_an_agent_turn_cannot_decide_its_own_plan_gate(
    tmp_path: Path, capsys, monkeypatch, provider: str | None, surface: str,
) -> None:
    registry, runtime = fixture(tmp_path, provider)
    plan = _propose(registry, runtime)
    gate_id = plan["gate_todo_id"]
    before = rows(registry)
    monkeypatch.setenv("LOOPX_AGENT_TURN", ORCH)  # what the Turn host exports to the model process
    decide = {
        "gate_resolve": ["gate", "resolve", "--goal-id", GOAL, "--todo-id", gate_id, "--decision", "approve"],
        "todo_complete": ["todo", "complete", "--goal-id", GOAL, "--todo-id", gate_id, "--role", "user",
                          "--decision-outcome", "approve", "--agent-id", ORCH],
    }[surface]
    code, refused = _cli_json(registry, runtime, capsys, *decide)
    assert code == 1
    assert refused["error_code"] == "gate_decision_refused_in_agent_turn"
    assert "loopx gate reply" in refused["error"]
    # Refused before anything is written: the gate stays open and the plan is not applied.
    assert rows(registry) == before
    assert read_plan(runtime, GOAL, plan["plan_id"])["status"] == "pending"
    assert "closed_by" not in read_gate_index(runtime, GOAL)["gates"][gate_id]
    with pytest.raises(PlanCardError, match="is not approved"):
        apply_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, plan_id=plan["plan_id"])
    # Replying stays allowed, for the orchestrator and for the owner.
    code, _ = _cli_json(registry, runtime, capsys, "gate", "reply", "--goal-id", GOAL, "--todo-id", gate_id,
                        "--as", "orchestrator", "--agent-id", ORCH, "--text", "Waiting for your approval.")
    assert code == 0
    code, replied = _cli_json(registry, runtime, capsys, "gate", "reply", "--goal-id", GOAL, "--todo-id", gate_id,
                              "--text", "Looks good.")
    assert code == 0 and replied["awaiting"] == AWAITING_ORCHESTRATOR


def test_closing_a_plan_gate_without_a_decision_never_applies_the_plan(tmp_path: Path, capsys, monkeypatch) -> None:
    """Superseding a gate (or updating it to done) is not a decision; plan apply needs a recorded approve."""

    registry, runtime = fixture(tmp_path)
    monkeypatch.setenv("LOOPX_AGENT_TURN", ORCH)
    superseded, updated = _propose(registry, runtime), _propose(registry, runtime, {**PLAN, "title": "Other"})
    code, _ = _cli_json(registry, runtime, capsys, "todo", "supersede", "--goal-id", GOAL, "--todo-id",
                        superseded["gate_todo_id"], "--role", "user", "--agent-id", ORCH, "--reason", "stale")
    assert code == 0
    code, _ = _cli_json(registry, runtime, capsys, "todo", "update", "--goal-id", GOAL, "--todo-id",
                        updated["gate_todo_id"], "--role", "user", "--status", "done", "--agent-id", ORCH)
    assert code == 0
    state = rows(registry)
    for plan in (superseded, updated):
        gate = state[plan["gate_todo_id"]]
        assert gate["status"] == "done" and not gate.get("decision_outcome")
        assert read_plan(runtime, GOAL, plan["plan_id"])["status"] == "pending"
        code, refused = _cli_json(registry, runtime, capsys, "plan", "apply", "--goal-id", GOAL,
                                  "--plan-id", plan["plan_id"])
        assert code == 1 and refused["error_code"] == "plan_not_approved"
    assert not [row for row in state.values() if row["role"] == "agent"]  # no plan todo was created


def test_an_owner_gate_decision_records_closed_by(tmp_path: Path, capsys) -> None:
    registry, runtime = fixture(tmp_path)
    plan = _propose(registry, runtime)
    gate_id = plan["gate_todo_id"]
    code, payload = _cli_json(registry, runtime, capsys, "gate", "resolve", "--goal-id", GOAL, "--todo-id", gate_id,
                              "--decision", "approve")
    assert code == 0, payload
    assert payload["plan_card"]["status"] == "applied"
    closed_by = read_gate_index(runtime, GOAL)["gates"][gate_id]["closed_by"]
    # The lifecycle actor defaults to the agent the gate blocks; the surface says who decided.
    assert {key: closed_by[key] for key in ("surface", "actor", "agent_turn")} == {
        "surface": "cli", "actor": ORCH, "agent_turn": None}
    assert closed_by["at"]
    code, shown = _cli_json(registry, runtime, capsys, "gate", "show", "--goal-id", GOAL, "--todo-id", gate_id)
    assert code == 0 and shown["closed_by"] == closed_by
    assert main(["--registry", str(registry), "--runtime-root", str(runtime), "gate", "show", "--goal-id", GOAL,
                 "--todo-id", gate_id]) == 0
    assert f"- closed by: cli (actor {ORCH}) at {closed_by['at']}" in capsys.readouterr().out


def test_a_plain_gate_decision_records_closed_by_without_a_thread(tmp_path: Path, capsys) -> None:
    """A gate nobody replied to has no index entry yet; its decision still records who made it."""

    registry, runtime = fixture(tmp_path)
    gate_id = open_gate(registry)
    assert gate_id not in read_gate_index(runtime, GOAL)["gates"]
    code, payload = _cli_json(registry, runtime, capsys, "todo", "complete", "--goal-id", GOAL, "--todo-id", gate_id,
                              "--role", "user", "--decision-outcome", "reject", "--agent-id", ORCH)
    assert code == 0, payload
    entry = read_gate_index(runtime, GOAL)["gates"][gate_id]
    assert (entry["closed"], entry["decision_outcome"]) == (True, "reject")
    assert (entry["closed_by"]["surface"], entry["closed_by"]["actor"], entry["closed_by"]["agent_turn"]) == (
        "cli", ORCH, None)


def test_cli_plan_propose_show_and_list(tmp_path: Path, capsys) -> None:
    registry, runtime = fixture(tmp_path)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(PLAN), encoding="utf-8")
    base = ["--registry", str(registry), "--runtime-root", str(runtime), "--format", "json", "plan"]
    assert main([*base, "propose", "--goal-id", GOAL, "--agent-id", ORCH, "--plan-file", str(plan_file)]) == 0
    plan_id = json.loads(capsys.readouterr().out)["plan"]["plan_id"]
    assert main([*base, "show", "--goal-id", GOAL, "--plan-id", plan_id]) == 0
    assert json.loads(capsys.readouterr().out)["plan"]["status"] == "pending"
    assert main([*base, "list", "--goal-id", GOAL]) == 0
    assert [p["plan_id"] for p in json.loads(capsys.readouterr().out)["plans"]] == [plan_id]


# --- goal intake -------------------------------------------------------------------


def test_goal_create_end_to_end_with_separate_state_home(tmp_path: Path, capsys) -> None:
    import subprocess

    state_home = tmp_path / "progress"
    runtime = tmp_path / "runtime"
    for repo in ("api", "web"):
        subprocess.run(["git", "init", "-q", str(tmp_path / repo)], check=True)
    doc = tmp_path / "inbox" / "requirements.md"
    doc.parent.mkdir()
    doc.write_text("# Todo app\n\nAn API and a web UI.\n", encoding="utf-8")
    argv = ["--runtime-root", str(runtime), "--format", "json", "goal", "create",
            "--project", str(state_home), "--goal-id", "todo-app", "--doc", str(doc),
            "--repo", f"api={tmp_path / 'api'}", "--repo", f"web={tmp_path / 'web'},default_branch=main",
            "--agent", "orch=orchestrator", "--agent", "dev=developer", "--agent", "acc=acceptor",
            "--no-global-sync"]
    assert main(argv) == 0, capsys.readouterr().out
    created = json.loads(capsys.readouterr().out)
    registry = state_home / ".loopx" / "registry.json"
    assert created["registry"] == str(registry.resolve())
    stored_doc = state_home / "docs" / "goals" / "todo-app" / "requirements.md"
    assert stored_doc.read_text(encoding="utf-8") == doc.read_text(encoding="utf-8")
    goal = json.loads(registry.read_text(encoding="utf-8"))["goals"][0]
    assert goal["coordination"]["agent_model"] == "role_v1"
    assert goal["coordination"]["agent_roles"] == {"acc": "acceptor", "dev": "developer", "orch": "orchestrator"}
    assert [repo["name"] for repo in goal["repos"]] == ["api", "web"]
    assert any(source.get("id") == "goal-requirements" and source.get("role") == "requirements"
               for source in goal["authority_sources"])
    todos = list_goal_todos(registry_path=registry, goal_id="todo-app")["todos"]
    first = next(todo for todo in todos if todo["todo_id"] == created["orchestrator_todo_id"])
    assert (first["claimed_by"], first["required_role"], first["action_kind"], first["status"]) == (
        "orch", "orchestrator", "plan", "open")
    events = load_rollout_events(rollout_event_log_path(runtime, "todo-app"))
    assert [e["event_kind"] for e in events if e["event_kind"] == "goal_intake"] == ["goal_intake"]
    # Re-running is an upsert, not a second goal or planning todo.
    assert main(argv) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["orchestrator_todo_id"] == created["orchestrator_todo_id"]
    assert len(json.loads(registry.read_text(encoding="utf-8"))["goals"]) == 1
    # A goal without an orchestrator is refused.
    assert main([*argv[:10], "--goal-id", "other", "--doc", str(doc), "--agent", "dev=developer",
                 "--no-global-sync"]) == 1
    assert "exactly one --agent ID=orchestrator" in json.loads(capsys.readouterr().out)["error"]


# --- web endpoint -------------------------------------------------------------------


@contextmanager
def _chat_server(registry: Path, runtime: Path) -> Iterator[str]:
    from loopx.chat_server import ChatHTTPServer, ChatRequestHandler

    server = ChatHTTPServer(("127.0.0.1", 0), ChatRequestHandler)
    server.registry_path = registry
    server.runtime_root_override = str(runtime)
    server.verbose = False
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_chat_gate_thread_endpoints(tmp_path: Path) -> None:
    registry, runtime = fixture(tmp_path)
    gate_id = open_gate(registry)
    with _chat_server(registry, runtime) as base:
        def post(body: dict) -> dict:
            request = Request(f"{base}/api/chat/gate-thread/reply", data=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request) as response:
                return json.load(response)

        replied = post({"goal_id": GOAL, "todo_id": gate_id, "text": "Can we use SQLite?"})
        assert replied["awaiting"] == AWAITING_ORCHESTRATOR and replied["message"]["author"] == "user"
        with urlopen(f"{base}/api/chat/gate-thread?goal_id={GOAL}&todo_id={gate_id}") as response:
            view = json.load(response)
        assert [m["text"] for m in view["messages"]] == ["Can we use SQLite?"]
        assert view["awaiting"] == AWAITING_ORCHESTRATOR
        with pytest.raises(HTTPError) as rejected:
            post({"goal_id": GOAL, "todo_id": gate_id, "text": "x", "author": "orchestrator"})
        assert rejected.value.code == 400
        with pytest.raises(HTTPError) as foreign:
            urlopen(Request(f"{base}/api/chat/gate-thread?goal_id={GOAL}&todo_id={gate_id}",
                            headers={"Origin": "https://unrelated.example"}))
        assert foreign.value.code == 403
        complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=gate_id, role="user",
                           decision_outcome="approve", note="ok", no_followup=True, agent_id=ORCH,
                           runtime_root_arg=str(runtime))
        with pytest.raises(HTTPError) as closed:
            post({"goal_id": GOAL, "todo_id": gate_id, "text": "late"})
        assert closed.value.code == 409
        with pytest.raises(HTTPError) as missing:
            urlopen(f"{base}/api/chat/gate-thread?goal_id={GOAL}&todo_id=todo_missing000")
        assert missing.value.code == 404


def test_chat_gate_thread_returns_the_plan_card_for_review(tmp_path: Path) -> None:
    """A plan_approval gate carries the card's summary and todo contracts, so it is decidable in the drawer."""
    registry, runtime = fixture(tmp_path)
    plan = _propose(registry, runtime)
    decision = open_gate(registry)
    with _chat_server(registry, runtime) as base:
        with urlopen(f"{base}/api/chat/gate-thread?goal_id={GOAL}&todo_id={plan['gate_todo_id']}") as response:
            view = json.load(response)
        with urlopen(f"{base}/api/chat/gate-thread?goal_id={GOAL}&todo_id={decision}") as response:
            plain = json.load(response)
    card = view["plan"]
    assert (card["plan_id"], card["status"], card["revision"], card["title"], card["summary"]) == (
        plan["plan_id"], "pending", 1, "Todo app v1", "Contract first, then API and web, then integration.")
    assert [todo["key"] for todo in card["todos"]] == ["contract", "api", "web", "integrate", "readme"]
    assert card["todos"][0] == {
        "key": "contract", "text": "Write the API contract", "required_role": "developer", "depends_on": [],
        "task_repositories": ["api"], "acceptance": "openapi covers CRUD", "validation_command": "test -f openapi.yaml",
    }
    assert (card["todos"][3]["depends_on"], card["todos"][3]["task_repositories"]) == (["api", "web"], ["api", "web"])
    assert card["todos"][4]["acceptance"] is None and card["todos"][4]["validation_command"] is None
    # The card is a bounded read model: agent bindings and estimates stay in `loopx plan show`.
    assert all(set(todo) == set(card["todos"][0]) for todo in card["todos"])
    assert "plan" not in plain and "plan_id" not in plain

    # A missing card file degrades to the thread alone and says so, instead of failing the drawer.
    plan_path(runtime, GOAL, plan["plan_id"]).unlink()
    degraded = gate_view(registry_path=registry, runtime_root=runtime, goal_id=GOAL, todo_id=plan["gate_todo_id"])
    assert degraded["plan_id"] == plan["plan_id"] and "plan" not in degraded and "criteria_changes" not in degraded
    assert degraded["plan_error"] == "plan_not_found" and degraded["messages"]


def _rewrite_card(runtime: Path, plan_id: str, mutate) -> None:
    path = plan_path(runtime, GOAL, plan_id)
    record = json.loads(path.read_text(encoding="utf-8"))
    mutate(record)
    path.write_text(json.dumps(record), encoding="utf-8")


@pytest.mark.parametrize("mutate", [
    lambda record: record["plan"].update(todos=5),
    lambda record: record["plan"].update(todos=["contract", "api"]),
    lambda record: record["plan"]["todos"][1].update(depends_on="contract"),
    lambda record: record["plan"]["todos"][0].update(text=None),
    lambda record: record.update(plan="Todo app v1"),
], ids=["todos-not-iterable", "todos-not-objects", "depends-on-not-a-list", "todo-without-text", "body-not-an-object"])
def test_a_malformed_plan_card_is_reported_unavailable_not_empty(tmp_path: Path, mutate) -> None:
    registry, runtime = fixture(tmp_path)
    plan = _propose(registry, runtime)
    _rewrite_card(runtime, plan["plan_id"], mutate)
    view = gate_view(registry_path=registry, runtime_root=runtime, goal_id=GOAL, todo_id=plan["gate_todo_id"])
    assert view["plan_id"] == plan["plan_id"] and "plan" not in view
    assert view["plan_error"] == "plan_malformed"
    assert [m["author"] for m in view["messages"]] == ["orchestrator"]  # the thread stays readable
    assert "plan card unavailable (plan_malformed)" in render_gate_markdown(view)


@pytest.mark.parametrize("mutate", [
    lambda record: record["plan"].update(criteria_changes=7),
    lambda record: record["plan"].update(criteria_changes=["openapi covers paging"]),
    lambda record: record.update(criteria_change_results=["applied"]),
    lambda record: record["plan"].update(criteria_changes=[
        {"old": "openapi covers CRUD", "new": "openapi covers paging", "reason": "paging"}]),
    lambda record: record["plan"].update(criteria_changes=[
        {"todo_id": "todo_0123456789ab", "old": "openapi covers CRUD", "new": 5, "reason": "paging"}]),
], ids=["changes-not-a-list", "changes-not-objects", "results-not-objects", "change-without-todo-id",
        "new-not-text"])
def test_malformed_criteria_changes_are_reported_unavailable_not_empty(tmp_path: Path, mutate) -> None:
    registry, runtime = fixture(tmp_path)
    plan = _propose(registry, runtime)
    _rewrite_card(runtime, plan["plan_id"], mutate)
    view = gate_view(registry_path=registry, runtime_root=runtime, goal_id=GOAL, todo_id=plan["gate_todo_id"])
    assert "criteria_changes" not in view and view["criteria_changes_error"] == "criteria_changes_malformed"
    assert view["plan"]["title"] == "Todo app v1" and "plan_error" not in view  # the rest of the card stays readable
    assert "Acceptance-criteria changes unavailable (criteria_changes_malformed)" in render_gate_markdown(view)
    shown = render_plan_markdown({"ok": True, "plan": read_plan(runtime, GOAL, plan["plan_id"])})
    assert "Acceptance-criteria changes unavailable" in shown


def test_cli_gate_show_markdown_renders_the_plan_card(tmp_path: Path, capsys) -> None:
    """The default (Markdown) `gate show` reviews a plan the same way the dashboard drawer does."""
    registry, runtime = fixture(tmp_path)
    plan = _propose(registry, runtime)
    argv = ["--registry", str(registry), "--runtime-root", str(runtime), "gate", "show", "--goal-id", GOAL,
            "--todo-id", plan["gate_todo_id"]]
    assert main(argv) == 0
    shown = capsys.readouterr().out
    assert f"## Plan `{plan['plan_id']}`: Todo app v1 (revision 1, pending)" in shown
    assert "Contract first, then API and web, then integration." in shown
    assert "1. `contract` Write the API contract (developer; repos: api)" in shown
    assert "   - acceptance: openapi covers CRUD" in shown
    assert "   - validation: `test -f openapi.yaml`" in shown
    assert "2. `api` Implement the API (developer; repos: api; after: contract)" in shown
    assert "4. `integrate` Integrate web and API (developer; repos: api, web; after: api, web)" in shown
    assert "5. `readme` Write the README (developer)" in shown
    assert shown.index("## Plan") < shown.index("## Thread")


def test_dispatcher_orchestrator_prompt_names_gates_awaiting_it(tmp_path: Path) -> None:
    from loopx.dispatch.prompts import dispatch_prompt_addendum

    registry, runtime = fixture(tmp_path)
    gate_id = open_gate(registry)
    reply_to_gate(registry_path=registry, runtime_root=runtime, goal_id=GOAL, todo_id=gate_id, text="Why?")
    prompt = dispatch_prompt_addendum(goal_id=GOAL, agent_id=ORCH, role="orchestrator", todo_id=None,
                                      workspace_repos=None,
                                      awaiting_gates=gates_awaiting_orchestrator(runtime, GOAL))
    assert f"`{gate_id}`" in prompt and "loopx gate show|reply" in prompt
    developer = dispatch_prompt_addendum(goal_id=GOAL, agent_id=DEV, role="developer", todo_id=None,
                                         workspace_repos=None)
    assert "loopx gate" not in developer


def test_dispatcher_orchestrator_prompt_pins_state_home_and_gate_open(tmp_path: Path) -> None:
    """E2E pilot: a bare `loopx` in the orchestrator prompt reached the default registry."""

    from loopx.dispatch.dispatcher import DispatchConfig, Dispatcher
    from loopx.dispatch.prompts import dispatch_prompt_addendum

    registry, runtime = fixture(tmp_path)
    dispatcher = Dispatcher(DispatchConfig(registry_path=registry, runtime_root=runtime, goal_ids=[GOAL],
                                           loopx_argv=("/py", "-m", "loopx.cli")))
    command = dispatcher._loopx_command()
    assert command == f"/py -m loopx.cli --registry {registry} --runtime-root {runtime}"
    prompt = dispatch_prompt_addendum(goal_id=GOAL, agent_id=ORCH, role="orchestrator", todo_id=None,
                                      workspace_repos=None, loopx_command=command)
    assert f"`{command}`" in prompt
    assert "--role user --task-class user_gate" in prompt


def test_user_reply_unblocks_the_orchestrator_lane_until_it_answers(tmp_path: Path) -> None:
    """E2E pilot: the orchestrator's own clarification gate kept its lane blocked after a reply."""

    import io
    from contextlib import redirect_stdout

    registry, runtime = fixture(tmp_path)
    add_goal_todo(registry_path=registry, goal_id=GOAL, role="agent", text="Clarify and plan",
                  action_kind="plan", claimed_by=ORCH,
                  role_contract={"required_role": "orchestrator", "requires_acceptance": False})
    gate_id = open_gate(registry)

    def should_run(agent: str) -> dict:
        out = io.StringIO()
        with redirect_stdout(out):
            main(["--registry", str(registry), "--runtime-root", str(runtime), "--format", "json",
                  "quota", "should-run", "--goal-id", GOAL, "--agent-id", agent])
        return json.loads(out.getvalue())

    # The fixture goal has no adapter, so should_run stays health-gated; the
    # quota state is what the gate decides.
    assert should_run(ORCH)["state"] == "operator_gate"
    reply_to_gate(registry_path=registry, runtime_root=runtime, goal_id=GOAL, todo_id=gate_id, text="Use sqlite")
    answered = should_run(ORCH)
    assert answered["state"] == "eligible"
    assert answered["agent_identity"]["awaiting_orchestrator_gate_ids"] == [gate_id]
    assert answered["selected_todo"]["action_kind"] == "plan"
    # The developer lane is not affected by the thread state.
    assert "awaiting_orchestrator_gate_ids" not in (should_run(DEV).get("agent_identity") or {})
    reply_to_gate(registry_path=registry, runtime_root=runtime, goal_id=GOAL, todo_id=gate_id,
                  text="Noted.", author="orchestrator", agent_id=ORCH)
    assert should_run(ORCH)["state"] == "operator_gate"


def test_plan_dependents_resume_only_when_every_dependency_is_done(tmp_path: Path) -> None:
    """E2E pilot: applied plan todos stayed deferred after their dependency finished."""

    from loopx.plan_cards import resume_ready_plan_todos

    registry, runtime = fixture(tmp_path)
    # The repos here are plain directories with no S5 workspace, so a declared
    # validation would fail closed (workspace_unverified); this test is about
    # dependency resume only.
    plan = {**PLAN, "todos": [{k: v for k, v in item.items() if k != "validation_command"} for item in PLAN["todos"]]}
    proposed = propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=ORCH, plan=plan)
    plan_id = proposed["plan"]["plan_id"]
    complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=proposed["plan"]["gate_todo_id"], role="user",
                       decision_outcome="approve", note="Go", no_followup=True, agent_id=ORCH)
    ids = read_plan(runtime, GOAL, plan_id)["todo_id_map"]

    def status(key: str) -> str:
        return rows(registry)[ids[key]]["status"]

    def finish(key: str) -> None:
        from loopx.todo_acceptance import accept_goal_todo

        todo_id = ids[key]
        owner = rows(registry)[todo_id].get("claimed_by") or DEV
        delivered = complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=todo_id, role="agent",
                                       evidence="built", agent_id=owner)
        if delivered.get("in_review"):
            accept_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=todo_id, agent_id=ACC)
        assert rows(registry)[todo_id]["status"] == "done"

    def resume() -> list[str]:
        return resume_ready_plan_todos(registry_path=registry, goal_id=GOAL, runtime_root=runtime)

    assert resume() == []
    assert status("api") == "deferred"
    finish("contract")  # the accept verdict resumes the dependents
    assert status("api") == "open" and status("web") == "open" and status("integrate") == "deferred"
    finish("web")  # integrate's resume_when names web, but api is still open
    assert resume() == [] and status("integrate") == "deferred"
    finish("api")
    assert status("integrate") == "open" and resume() == []


def test_only_the_role_v1_orchestrator_skips_the_peer_worktree_guard() -> None:
    """E2E pilot: the orchestrator's plan-todo completion failed the peer worktree guard.

    It works from the state home and delivers no code; developers and
    acceptors keep refreshing from their per-todo worktrees.
    """

    from loopx.state_refresh import _role_v1_orchestrator

    coordination = {"registered_agents": [ORCH, DEV, ACC],
                    "agent_roles": {ORCH: "orchestrator", DEV: "developer", ACC: "acceptor"}}
    role_v1 = {"id": GOAL, "coordination": {"agent_model": "role_v1", **coordination}}
    peer_v1 = {"id": GOAL, "coordination": {"agent_model": "peer_v1", **coordination}}
    assert _role_v1_orchestrator(role_v1, ORCH) is True
    assert _role_v1_orchestrator(role_v1, DEV) is False and _role_v1_orchestrator(role_v1, ACC) is False
    assert _role_v1_orchestrator(peer_v1, ORCH) is False
    assert _role_v1_orchestrator(role_v1, None) is False
