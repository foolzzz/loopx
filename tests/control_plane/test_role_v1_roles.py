"""role_v1 fork slice S1: roles in the kernel.

Covers role registration (configure-goal and register-agent), the
one-orchestrator rule, the identity packet role, the removed anti-hierarchy
ban, the todo role contract fields, role-aware quota selection, and replan
routing to the orchestrator.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loopx.agent_registry import (
    agent_roles_for_goal,
    lifecycle_agent_for_owner_write,
    normalize_agent_roles,
    orchestrator_agent_for_goal,
)
from loopx.cli import main
from loopx.configure_goal import configure_goal
from loopx.control_plane.agents.identity import build_quota_agent_identity
from loopx.control_plane.agents.legacy_migration import legacy_agent_hierarchy_present
from loopx.control_plane.agents.profile import normalize_agent_profile
from loopx.control_plane.agents.runtime_model import (
    AgentRuntimeModel,
    agent_runtime_model_for_goal,
)
from loopx.control_plane.goals.goal_frontier import (
    autonomous_replan_scope_decision,
    select_autonomous_replan_obligation,
)
from loopx.control_plane.quota.task_orchestration_admission import (
    build_adaptive_task_orchestration_contract as build_task_orchestration_admission,
)
from loopx.control_plane.todos.contract import (
    todo_effective_required_role,
    todo_requires_acceptance,
)
from loopx.control_plane.todos.quota_summary import select_quota_todo_summary
from loopx.todos import add_goal_todo, list_goal_todos, update_goal_todo


GOAL_ID = "role-v1-goal"
ORCH = "fable-orch"
DEV = "opus-dev"
DEV2 = "opus-dev-2"
ACC = "codex-acceptor"


def _registry(tmp_path: Path, coordination: dict | None = None) -> Path:
    (tmp_path / "ACTIVE_GOAL_STATE.md").write_text(
        "---\n"
        f"goal_id: {GOAL_ID}\n"
        "---\n\n"
        "## User Todo\n\n"
        "## Agent Todo\n\n",
        encoding="utf-8",
    )
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "goals": [
                    {
                        "id": GOAL_ID,
                        "repo": str(tmp_path),
                        "state_file": "ACTIVE_GOAL_STATE.md",
                        "repos": [
                            {"name": "backend", "path": str(tmp_path / "backend")},
                            {"name": "Frontend", "path": str(tmp_path / "frontend")},
                        ],
                        "coordination": coordination
                        if coordination is not None
                        else {"registered_agents": [ORCH, DEV, ACC]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return registry


def _goal(registry: Path) -> dict:
    return json.loads(registry.read_text(encoding="utf-8"))["goals"][0]


def _role_goal() -> dict:
    return {
        "id": GOAL_ID,
        "coordination": {
            "agent_model": "role_v1",
            "registered_agents": [ORCH, DEV, ACC],
            "agent_roles": {ORCH: "orchestrator", DEV: "developer", ACC: "acceptor"},
        },
    }


# --- runtime model -----------------------------------------------------------


def test_new_goals_default_to_role_v1_and_peer_v1_stays_readable() -> None:
    assert agent_runtime_model_for_goal({}) is AgentRuntimeModel.ROLE_V1
    assert agent_runtime_model_for_goal(
        {"coordination": {"agent_model": "peer_v1"}}
    ) is AgentRuntimeModel.PEER_V1
    assert agent_runtime_model_for_goal(
        {"coordination": {"agent_model": "role_v1"}}
    ) is AgentRuntimeModel.ROLE_V1
    with pytest.raises(ValueError, match="role_v1 or peer_v1"):
        agent_runtime_model_for_goal({"coordination": {"agent_model": "flat"}})


# --- registration ------------------------------------------------------------


def test_configure_goal_writes_roles_and_stamps_role_v1(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    payload = configure_goal(
        registry_path=registry,
        goal_id=GOAL_ID,
        agent_roles={ORCH: "orchestrator", DEV: "developer", ACC: "Acceptor"},
        execute=True,
    )
    assert payload["written"] is True
    assert payload["after"]["agent_model"] == "role_v1"
    assert payload["after"]["agent_roles"] == {
        ACC: "acceptor",
        ORCH: "orchestrator",
        DEV: "developer",
    }
    coordination = _goal(registry)["coordination"]
    assert coordination["agent_model"] == "role_v1"
    assert coordination["agent_roles"][ORCH] == "orchestrator"
    assert orchestrator_agent_for_goal(_goal(registry)) == ORCH


def test_configure_goal_keeps_an_existing_peer_v1_goal_on_peer_v1(tmp_path: Path) -> None:
    registry = _registry(
        tmp_path,
        {"agent_model": "peer_v1", "registered_agents": [ORCH, DEV]},
    )
    configure_goal(
        registry_path=registry, goal_id=GOAL_ID,
        registered_agents=[ORCH, DEV, ACC], execute=True,
    )
    assert _goal(registry)["coordination"]["agent_model"] == "peer_v1"
    # Explicit opt-in switches the goal to role_v1.
    configure_goal(
        registry_path=registry, goal_id=GOAL_ID, agent_model="role_v1",
        agent_roles={ORCH: "orchestrator"}, execute=True,
    )
    assert _goal(registry)["coordination"]["agent_model"] == "role_v1"
    # peer_v1 goals never expose an orchestrator route.
    assert orchestrator_agent_for_goal(
        {"coordination": {"agent_model": "peer_v1", "registered_agents": [ORCH],
                          "agent_roles": {ORCH: "orchestrator"}}}
    ) is None


def test_at_most_one_orchestrator_per_goal(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    with pytest.raises(ValueError, match="at most one orchestrator"):
        configure_goal(
            registry_path=registry, goal_id=GOAL_ID,
            agent_roles={ORCH: "orchestrator", DEV: "orchestrator"}, execute=True,
        )
    configure_goal(
        registry_path=registry, goal_id=GOAL_ID,
        agent_roles={ORCH: "orchestrator"}, execute=True,
    )
    # A second orchestrator added in a later call is rejected on the merge.
    with pytest.raises(ValueError, match="at most one orchestrator"):
        configure_goal(
            registry_path=registry, goal_id=GOAL_ID,
            agent_roles={DEV: "orchestrator"}, execute=True,
        )
    # Reassigning works once the old orchestrator is cleared in the same call.
    configure_goal(
        registry_path=registry, goal_id=GOAL_ID,
        agent_roles={DEV: "orchestrator"}, clear_agent_roles=[ORCH], execute=True,
    )
    assert agent_roles_for_goal(_goal(registry)) == {DEV: "orchestrator"}


def test_roles_require_registered_agents_and_known_roles() -> None:
    with pytest.raises(ValueError, match="registered agent"):
        normalize_agent_roles({"ghost": "developer"}, registered_agents=[DEV])
    with pytest.raises(ValueError, match="must be one of"):
        normalize_agent_roles({DEV: "manager"}, registered_agents=[DEV])


def test_unregistering_an_agent_drops_its_role(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    configure_goal(
        registry_path=registry, goal_id=GOAL_ID,
        agent_roles={ORCH: "orchestrator", DEV: "developer"}, execute=True,
    )
    configure_goal(
        registry_path=registry, goal_id=GOAL_ID,
        registered_agents=[ORCH, ACC], execute=True,
    )
    assert _goal(registry)["coordination"]["agent_roles"] == {ORCH: "orchestrator"}


def test_configure_goal_cli_accepts_repeatable_agent_role(tmp_path: Path, capsys) -> None:
    registry = _registry(tmp_path)
    code = main([
        "--registry", str(registry), "--runtime-root", str(tmp_path / "runtime"),
        "--format", "json", "configure-goal",
        "--goal-id", GOAL_ID,
        "--agent-role", f"{ORCH}=orchestrator",
        "--agent-role", f"{DEV}=developer",
        "--execute",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert _goal(registry)["coordination"]["agent_roles"] == {
        ORCH: "orchestrator", DEV: "developer",
    }
    code = main([
        "--registry", str(registry), "--runtime-root", str(tmp_path / "runtime"),
        "--format", "json", "configure-goal",
        "--goal-id", GOAL_ID, "--agent-role", f"{DEV}-bad", "--execute",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code != 0
    assert "AGENT_ID=" in payload["error"]


def test_register_agent_records_role(tmp_path: Path, capsys) -> None:
    runtime_root = tmp_path / "runtime"
    project = tmp_path / "project"
    source_registry = project / ".loopx" / "registry.json"
    state_file = project / "ACTIVE_GOAL_STATE.md"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("# Active Goal State\n\n## Agent Todo\n\n", encoding="utf-8")
    goal = {
        "id": GOAL_ID, "status": "active", "repo": str(project),
        "state_file": "ACTIVE_GOAL_STATE.md",
        "adapter": {"kind": "fixture", "status": "connected-read-only"},
        "coordination": {"registered_agents": [ORCH]},
        "source_registry": str(source_registry),
    }
    payload = {"schema_version": "0.1", "common_runtime_root": str(runtime_root),
               "goals": [goal]}
    source_registry.parent.mkdir(parents=True, exist_ok=True)
    source_registry.write_text(json.dumps(payload), encoding="utf-8")
    runtime_root.mkdir()
    (runtime_root / "registry.global.json").write_text(
        json.dumps({**payload, "registry_role": "global-local"}), encoding="utf-8"
    )
    code = main([
        "--runtime-root", str(runtime_root), "--format", "json", "register-agent",
        "--goal-id", GOAL_ID, "--agent-id", DEV, "--role", "developer", "--execute",
    ])
    result = json.loads(capsys.readouterr().out)
    assert result["written"] is True, result
    assert result["agent_roles"] == {DEV: "developer"}
    stored = json.loads(source_registry.read_text(encoding="utf-8"))["goals"][0]
    assert stored["coordination"]["agent_model"] == "role_v1"
    assert stored["coordination"]["agent_roles"] == {DEV: "developer"}
    # A second orchestrator is refused by register-agent too.
    main([
        "--runtime-root", str(runtime_root), "--format", "json", "register-agent",
        "--goal-id", GOAL_ID, "--agent-id", ORCH, "--role", "orchestrator", "--execute",
    ])
    capsys.readouterr()
    code = main([
        "--runtime-root", str(runtime_root), "--format", "json", "register-agent",
        "--goal-id", GOAL_ID, "--agent-id", ACC, "--role", "orchestrator", "--execute",
    ])
    refused = json.loads(capsys.readouterr().out)
    assert code != 0
    assert "at most one orchestrator" in refused["error"]


# --- identity packet ---------------------------------------------------------


def test_identity_packet_carries_role_and_orchestrator() -> None:
    goal = _role_goal()
    identity = build_quota_agent_identity(goal, agent_id=DEV)
    assert identity["agent_model"] == "role_v1"
    assert identity["role"] == "developer"
    assert identity["orchestrator_agent_id"] == ORCH
    assert build_quota_agent_identity(goal, agent_id=ORCH)["role"] == "orchestrator"
    peer_goal = {"coordination": {"agent_model": "peer_v1",
                                  "registered_agents": [ORCH, DEV],
                                  "agent_roles": {DEV: "developer"}}}
    peer_identity = build_quota_agent_identity(peer_goal, agent_id=DEV)
    assert "role" not in peer_identity
    assert "orchestrator_agent_id" not in peer_identity


def test_owner_writes_are_attributed_to_a_registered_agent() -> None:
    goal = _role_goal()
    # The claim owner wins, then the first registered fallback, then the orchestrator.
    assert lifecycle_agent_for_owner_write(goal, DEV, ACC) == DEV
    assert lifecycle_agent_for_owner_write(goal, None, ACC) == ACC
    assert lifecycle_agent_for_owner_write(goal, None, None, "owner", ACC) == ACC
    assert lifecycle_agent_for_owner_write(goal, None, "owner") == ORCH
    assert lifecycle_agent_for_owner_write(goal, None) == ORCH
    peer_goal = {"coordination": {"agent_model": "peer_v1", "registered_agents": [ORCH, DEV]}}
    assert lifecycle_agent_for_owner_write(peer_goal, None, "owner") is None
    assert lifecycle_agent_for_owner_write(peer_goal, DEV) == DEV


# --- removed anti-hierarchy rules --------------------------------------------


@pytest.mark.parametrize("role", ["orchestrator", "manager", "worker", "primary-agent"])
def test_profile_role_accepts_hierarchy_names(role: str) -> None:
    profile = normalize_agent_profile(
        {"agent_id": ORCH, "profile_role": role}, registered_agents=[ORCH],
    )
    assert profile["profile_role"] == role


def test_role_key_is_not_a_legacy_hierarchy_marker_for_role_v1() -> None:
    profiles = {ORCH: {"role": "orchestrator"}}
    assert legacy_agent_hierarchy_present(
        {"coordination": {"agent_profiles": profiles}}
    )
    assert not legacy_agent_hierarchy_present(
        {"coordination": {"agent_model": "role_v1", "agent_profiles": profiles}}
    )


# --- todo contract fields ----------------------------------------------------


def test_todo_role_fields_round_trip_through_add_and_update(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    added = add_goal_todo(
        registry_path=registry, goal_id=GOAL_ID, role="agent",
        text="implement the orders API", task_class="advancement_task",
        role_contract={
            "required_role": "developer",
            "acceptor_agent": ACC,
            "task_repositories": ["backend", "Frontend", "backend"],
            "requires_acceptance": True,
        },
    )
    assert added["required_role"] == "developer"
    assert added["task_repositories"] == ["backend", "Frontend"]
    todo_id = added["todo_id"]
    state = (tmp_path / "ACTIVE_GOAL_STATE.md").read_text(encoding="utf-8")
    assert "required_role=developer" in state
    assert "task_repositories=backend%2CFrontend" in state

    updated = update_goal_todo(
        registry_path=registry, goal_id=GOAL_ID, todo_id=todo_id,
        agent_id=ORCH, role_contract={"reject_count": 2, "acceptor_agent": None,
                       "requires_acceptance": False},
    )
    assert updated["reject_count"] == 2
    assert updated["requires_acceptance"] is False
    assert "acceptor_agent" not in updated
    items = list_goal_todos(registry_path=registry, goal_id=GOAL_ID, role="agent")
    item = next(entry for entry in items["agent_todos"]["items"]
                if entry["todo_id"] == todo_id)
    assert item["reject_count"] == 2
    assert item["required_role"] == "developer"
    assert item["task_repositories"] == ["backend", "Frontend"]
    assert "acceptor_agent" not in item

    # Resetting the reject count to the default clears it.
    reset = update_goal_todo(
        registry_path=registry, goal_id=GOAL_ID, todo_id=todo_id,
        agent_id=ORCH, role_contract={"reject_count": 0},
    )
    assert "reject_count" not in reset


def test_todo_role_fields_reject_invalid_values(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    with pytest.raises(ValueError, match="required_role"):
        add_goal_todo(registry_path=registry, goal_id=GOAL_ID, role="agent",
                      text="implement x", role_contract={"required_role": "boss"})
    with pytest.raises(ValueError, match="registered"):
        add_goal_todo(registry_path=registry, goal_id=GOAL_ID, role="agent",
                      text="implement y", role_contract={"acceptor_agent": "ghost"})
    with pytest.raises(ValueError, match="task_repositories"):
        add_goal_todo(registry_path=registry, goal_id=GOAL_ID, role="agent",
                      text="implement z",
                      role_contract={"task_repositories": ["backend", "Not Valid!"]})
    with pytest.raises(ValueError, match="unknown Goal repos: mobile"):
        add_goal_todo(registry_path=registry, goal_id=GOAL_ID, role="agent",
                      text="implement w",
                      role_contract={"task_repositories": ["backend", "mobile"]})


def test_todo_cli_flags_for_role_fields(tmp_path: Path, capsys) -> None:
    registry = _registry(tmp_path)
    code = main([
        "--registry", str(registry), "--runtime-root", str(tmp_path / "runtime"),
        "--format", "json", "todo", "add",
        "--goal-id", GOAL_ID, "--role", "agent", "--text", "implement the orders UI",
        "--required-role", "developer", "--acceptor-agent", ACC,
        "--task-repo", "Frontend", "--task-repo", "backend",
        "--requires-acceptance", "true",
    ])
    added = json.loads(capsys.readouterr().out)
    assert code == 0, added.get("error")
    assert added["task_repositories"] == ["Frontend", "backend"]
    code = main([
        "--registry", str(registry), "--runtime-root", str(tmp_path / "runtime"),
        "--format", "json", "todo", "update",
        "--goal-id", GOAL_ID, "--todo-id", added["todo_id"],
        "--reject-count", "1", "--clear-acceptor-agent", "--clear-task-repos",
        "--agent-id", ORCH,
    ])
    updated = json.loads(capsys.readouterr().out)
    assert code == 0, updated.get("error")
    assert updated["reject_count"] == 1
    assert "task_repositories" not in updated
    assert "acceptor_agent" not in updated
    code = main([
        "--registry", str(registry), "--runtime-root", str(tmp_path / "runtime"),
        "--format", "json", "todo", "list",
        "--goal-id", GOAL_ID, "--required-role", "developer",
    ])
    rejected = json.loads(capsys.readouterr().out)
    assert code != 0
    assert "--required-role" in rejected["error"]


def test_effective_role_and_acceptance_defaults() -> None:
    assert todo_effective_required_role({"task_class": "advancement_task"}) == "developer"
    assert todo_effective_required_role({"task_class": "user_gate", "role": "user"}) == "orchestrator"
    assert todo_effective_required_role({"task_class": "blocker"}) == "orchestrator"
    assert todo_effective_required_role(
        {"replan_obligation_id": "replan-0123456789abcdef"}
    ) == "orchestrator"
    assert todo_effective_required_role({"action_kind": "decompose"}) == "orchestrator"
    assert todo_effective_required_role({"required_role": "acceptor"}) == "acceptor"
    assert todo_requires_acceptance({"task_class": "advancement_task"}) is True
    assert todo_requires_acceptance({"task_class": "continuous_monitor"}) is False
    assert todo_requires_acceptance({"task_class": "advancement_task",
                                     "requires_acceptance": False}) is False
    assert todo_requires_acceptance({"required_role": "orchestrator",
                                     "task_class": "advancement_task"}) is False


# --- role-aware selection ----------------------------------------------------


def _summary_items() -> dict:
    items = [
        {"todo_id": "todo_impl", "text": "implement the API", "status": "open",
         "role": "agent", "task_class": "advancement_task"},
        {"todo_id": "todo_review", "text": "implement review of the API",
         "status": "open", "role": "agent", "task_class": "advancement_task",
         "required_role": "acceptor"},
        {"todo_id": "todo_split", "text": "implement the plan split", "status": "open",
         "role": "agent", "task_class": "advancement_task", "action_kind": "decompose"},
        {"todo_id": "todo_planning", "text": "implement a replan successor",
         "status": "open", "role": "agent", "task_class": "advancement_task",
         "replan_obligation_id": "replan-0123456789abcdef"},
    ]
    for index, item in enumerate(items, start=1):
        item["index"] = index
        item["done"] = False
    return {"schema_version": "todo_summary_v0", "items": items,
            "total_count": len(items), "open_count": len(items)}


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        (DEV, ["todo_impl"]),
        (ACC, ["todo_review"]),
        (ORCH, ["todo_split", "todo_planning"]),
    ],
)
def test_quota_selection_filters_by_role(agent: str, expected: list[str]) -> None:
    identity = build_quota_agent_identity(_role_goal(), agent_id=agent)
    summary = select_quota_todo_summary(_summary_items(), None, agent_identity=identity)
    assert [item["todo_id"] for item in summary["first_executable_items"]] == expected
    assert summary["role_scope"]["agent_role"] == identity["role"]


def test_quota_selection_without_role_keeps_peer_behaviour() -> None:
    goal = _role_goal()
    goal["coordination"]["agent_roles"] = {ORCH: "orchestrator"}
    identity = build_quota_agent_identity(goal, agent_id=DEV)
    assert "role" not in identity
    summary = select_quota_todo_summary(_summary_items(), None, agent_identity=identity)
    assert len(summary["executable_backlog_items"]) == 4
    assert "role_scope" not in summary


# --- replan routing ----------------------------------------------------------


def _obligation(agent: str | None = DEV) -> dict:
    obligation = {"schema_version": "autonomous_replan_obligation_v0", "required": True,
                  "triggers": [{"kind": "no_progress_streak", "agent_id": agent}]}
    if agent:
        obligation["agent_id"] = agent
    return obligation


def test_replan_scope_routes_to_the_orchestrator() -> None:
    for obligation in (_obligation(DEV), _obligation(None)):
        routed = {
            agent: autonomous_replan_scope_decision(
                obligation, agent_id=agent, registered_agent_ids=[ORCH, DEV, ACC],
                orchestrator_agent_id=ORCH,
            )
            for agent in (ORCH, DEV, ACC)
        }
        assert routed[ORCH]["applies"] is True
        assert routed[ORCH]["scope"] == "role_v1_orchestrator"
        assert routed[DEV]["applies"] is False
        assert routed[ACC]["applies"] is False


def test_replan_scope_without_orchestrator_keeps_peer_rules() -> None:
    decision = autonomous_replan_scope_decision(
        _obligation(DEV), agent_id=DEV, registered_agent_ids=[ORCH, DEV],
    )
    assert decision["applies"] is True
    assert decision["scope"] == "explicit_agent_owner"


def test_orchestrator_picks_up_another_lane_obligation() -> None:
    item = {"autonomous_replan_obligations_by_agent": {DEV: _obligation(DEV)}}
    assert select_autonomous_replan_obligation(item, agent_id=ORCH) is None
    routed = select_autonomous_replan_obligation(
        item, agent_id=ORCH, orchestrator_agent_id=ORCH,
    )
    assert routed["routed_agent_id"] == ORCH
    assert routed["evidence_agent_id"] == DEV
    # The obligation identity is unchanged, so ids and ACKs stay stable.
    assert routed["agent_id"] == DEV


def test_task_orchestration_coordinator_is_the_orchestrator() -> None:
    def lane(todo_id: str) -> dict:
        return {"todo_id": todo_id, "text": f"implement {todo_id}", "status": "open",
                "role": "agent", "task_class": "advancement_task", "done": False,
                "required_write_scopes": [f"src/{todo_id}"]}

    items = [lane("todo_a"), lane("todo_b")]
    kwargs = dict(
        goal_boundary={}, orchestration={"mode": "multi_subagent", "spawn_allowed": True,
                                         "max_children": 2},
        raw_agent_todo_summary={"items": items}, raw_user_todo_summary={"items": []},
        agent_todo_source_items=items, user_todo_source_items=[],
        available_capabilities=[], parent_goal_id=GOAL_ID, max_children=2,
    )
    identity = {"registered_agents": [ORCH, DEV], "orchestrator_agent_id": ORCH}
    assert build_task_orchestration_admission(
        agent_id=DEV, agent_identity=identity, **kwargs
    ) is None


# --- end to end: canonical authority and quota should-run --------------------


@pytest.mark.parametrize("promoted", [False, True])
def test_role_fields_and_selection_end_to_end(tmp_path: Path, promoted: bool) -> None:
    from canonical_authority_fixture import initialize_canonical_authority
    from loopx.control_plane.coordination.runtime_shadow import (
        build_todo_runtime_shadow_projection,
    )
    from loopx.control_plane.testing.canary_harness import (
        run_json_cli_result,
        write_fixture_registry,
    )
    from loopx.control_plane.todos.active_state_todo_parser import (
        parse_active_state_todos,
    )

    runtime, registry = tmp_path / "runtime", tmp_path / "registry.json"
    state = tmp_path / "state.md"
    state.write_text(
        "---\nstatus: active\n---\n# Goal\n## Objective\nShip the orders feature.\n\n"
        "## Agent Todo\n- [ ] [P1] Implement the orders API endpoint.\n"
        "  <!-- loopx:todo todo_id=todo_orders_api role=agent task_class=advancement_task "
        "status=open -->\n",
        encoding="utf-8",
    )
    write_fixture_registry(
        project=tmp_path, runtime_root=runtime, registry_path=registry,
        goal_id="goal-r", domain="role-v1", adapter_kind="generic_project_goal_v0",
        state_file=str(state), registered_agents=[ORCH, DEV, ACC], quota_allowed_slots=None,
    )
    payload = json.loads(registry.read_text())
    coordination = payload["goals"][0].setdefault("coordination", {})
    coordination["agent_model"] = "role_v1"
    coordination["agent_roles"] = {ORCH: "orchestrator", DEV: "developer", ACC: "acceptor"}
    payload["goals"][0]["repos"] = [
        {"name": name, "path": str(tmp_path / name)} for name in ("backend", "frontend")
    ]
    registry.write_text(json.dumps(payload), encoding="utf-8")
    if promoted:
        goal = json.loads(registry.read_text())["goals"][0]
        fields = parse_active_state_todos(state.read_text(), goal=goal, item_limit=None)
        projection = build_todo_runtime_shadow_projection(
            goal_id="goal-r", todos=fields["agent_todos"]["items"], handoff_mode="soft_claim",
        )
        initialize_canonical_authority(runtime, "goal-r", projection, state_path=state)
        state.unlink()

    code, added = run_json_cli_result(
        "todo", "add", "--goal-id", "goal-r", "--role", "agent",
        "--text", "Review the orders API against the acceptance criteria.",
        "--task-class", "advancement_task", "--required-role", "acceptor",
        "--acceptor-agent", ACC, "--task-repo", "backend",
        registry_path=registry, runtime_root=runtime,
    )
    assert code == 0, added
    code, updated = run_json_cli_result(
        "todo", "update", "--goal-id", "goal-r", "--todo-id", "todo_orders_api",
        "--agent-id", ORCH, "--task-repo", "backend", "--task-repo", "frontend",
        "--reject-count", "1",
        registry_path=registry, runtime_root=runtime,
    )
    assert code == 0, updated
    code, listed = run_json_cli_result(
        "todo", "list", "--goal-id", "goal-r", "--role", "agent",
        registry_path=registry, runtime_root=runtime,
    )
    assert code == 0, listed
    by_id = {item["todo_id"]: item for item in listed["agent_todos"]["items"]}
    assert by_id["todo_orders_api"]["task_repositories"] == ["backend", "frontend"]
    assert by_id["todo_orders_api"]["reject_count"] == 1
    review = next(item for item in by_id.values() if item.get("required_role") == "acceptor")
    assert review["acceptor_agent"] == ACC

    def executable(agent: str) -> list[str]:
        code, packet = run_json_cli_result(
            "quota", "should-run", "--goal-id", "goal-r", "--agent-id", agent,
            "--scan-path", str(tmp_path), registry_path=registry, runtime_root=runtime,
        )
        assert code == 0, packet
        summary = packet["agent_todo_summary"]
        assert summary["role_scope"]["agent_role"] == agent_roles[agent]
        return [item["todo_id"] for item in summary.get("first_executable_items") or []]

    agent_roles = {ORCH: "orchestrator", DEV: "developer", ACC: "acceptor"}
    assert executable(DEV) == ["todo_orders_api"]
    assert executable(ACC) == [review["todo_id"]]
    assert executable(ORCH) == []
    # Completion works under role_v1 (the completion policy accepts the model).
    code, completed = run_json_cli_result(
        "todo", "complete", "--goal-id", "goal-r", "--todo-id", "todo_orders_api",
        "--claimed-by", DEV, "--agent-id", DEV, "--evidence", "endpoint merged",
        "--no-follow-up",
        registry_path=registry, runtime_root=runtime,
    )
    assert code == 0, completed.get("error")
