"""Decision 43: orchestrator Turns get a bounded, precomputed goal-state digest.

E2E pilot v1 orchestrator Turns ran 26-43 host steps, mostly CLI reads to
discover state, each re-sending 40-60k tokens. The dispatcher now renders the
state once at launch and puts it in the orchestrator's system prompt.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from loopx.dispatch.orchestrator_digest import (
    DIGEST_MAX_CHARS,
    DIGEST_TRUNCATION_MARKER,
    LIVE_TODO_LIMIT,
    OPEN_GATE_LIMIT,
    bound_digest,
    build_orchestrator_digest,
    orchestrator_digest_or_note,
)
from loopx.dispatch.prompts import ORCHESTRATOR_DIGEST_GUIDANCE
from loopx.gate_threads import reply_to_gate
from loopx.plan_cards import propose_plan
from loopx.todos import add_goal_todo, update_goal_todo
from tests.dispatch.dispatch_fixtures import GOAL_ID, make_repo, read_jsonl, write_fixture
from tests.dispatch.test_loopx_dispatcher import ScriptedShouldRun, _dispatcher

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
AGENTS = {"orch": {"role": "orchestrator"}, "dev": {"role": "developer"},
          "acc": {"role": "acceptor", "runtime": "codex-cli"}}


def _kw(fixture: dict[str, Any]) -> dict[str, Any]:
    return {"registry_path": fixture["registry"], "goal_id": GOAL_ID, "runtime_root_arg": str(fixture["runtime"])}


def _digest(fixture: dict[str, Any], todo_id: str | None = None, **extra: Any) -> str:
    return build_orchestrator_digest(
        registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_id=GOAL_ID, todo_id=todo_id,
        launch_reason="selected_todo", now=NOW, **extra,
    )


def _small_goal(tmp_path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    repos = {"api": make_repo(tmp_path, "api"), "web": make_repo(tmp_path, "web")}
    fixture = write_fixture(tmp_path, agents=AGENTS, repos=repos)
    ids: dict[str, str] = {}
    ids["planning"] = add_goal_todo(
        **_kw(fixture), role="agent", text="Clarify the requirements, then propose a plan", action_kind="plan",
        claimed_by="orch", role_contract={"required_role": "orchestrator", "requires_acceptance": False},
    )["todo_id"]
    ids["api"] = add_goal_todo(
        **_kw(fixture), role="agent", text="Implement the due field in the API", task_class="advancement_task",
        claimed_by="dev",
        role_contract={"required_role": "developer", "requires_acceptance": True, "acceptor_agent": "acc",
                       "task_repositories": ["api"], "acceptance_criteria": "POST /todos accepts an ISO due date"},
    )["todo_id"]
    update_goal_todo(
        **_kw(fixture), todo_id=ids["api"], role="agent", agent_id="dev",
        role_contract={"reject_count": 1, "review_feedback": "rejected by acc (#1): invalid dates return 500"},
    )
    ids["gate"] = add_goal_todo(**_kw(fixture), role="user", task_class="user_gate", agent_id="orch",
                                text="Should overdue be computed in UTC?")["todo_id"]
    reply_to_gate(registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_id=GOAL_ID,
                  todo_id=ids["gate"], text="Is local time an option?", author="orchestrator", agent_id="orch")
    reply_to_gate(registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_id=GOAL_ID,
                  todo_id=ids["gate"], text="Use the user's local date.")
    plan = propose_plan(
        registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_id=GOAL_ID, agent_id="orch",
        plan={"title": "Web follow-up", "todos": [{"key": "web", "text": "Show the due date", "bound_agent": "dev",
                                                   "task_repositories": ["web"], "acceptance": "due is shown"}]},
        runtime_root_arg=str(fixture["runtime"]),
    )["plan"]
    ids["plan"], ids["plan_gate"] = plan["plan_id"], plan["gate_todo_id"]
    return fixture, ids


def test_the_digest_carries_what_the_orchestrator_used_to_discover_with_cli_calls(tmp_path: Path) -> None:
    fixture, ids = _small_goal(tmp_path)
    digest = _digest(fixture, ids["planning"])

    assert digest.startswith("## LoopX goal state digest (as of launch, 2026-09-28T12:00:00+00:00)")
    # Why this Turn runs, and for which todo.
    assert f"This Turn: todo `{ids['planning']}` [open], planning todo" in digest
    assert "dispatcher reason `selected_todo`" in digest
    # Repos and roles.
    assert "Repos: api (merge_target=main, default_branch=main); web (merge_target=main, default_branch=main)" in digest
    assert "Agents: acc=acceptor, dev=developer, orch=orchestrator" in digest
    # Todos: role, bound agent, acceptor, reject count, criteria and the latest feedback.
    assert f"- `{ids['api']}` [open] role=developer agent=dev acceptor=acc rejects=1 repos=api" in digest
    assert "  criteria: POST /todos accepts an ISO due date" in digest
    assert "  feedback: rejected by acc (#1): invalid dates return 500" in digest
    # The open gate awaiting the orchestrator, with its thread tail.
    assert f"gate replies awaiting you: `{ids['gate']}`" in digest
    assert f"- `{ids['gate']}` kind=decision awaiting_orchestrator: Should overdue be computed in UTC?" in digest
    assert "  orchestrator#1: Is local time an option?" in digest
    assert "  user#2: Use the user's local date." in digest
    # The pending plan card and its gate.
    assert f"- pending `{ids['plan']}` rev 1 gate={ids['plan_gate']}: Web follow-up (1 todos)" in digest
    assert f"kind=plan_approval awaiting_user plan={ids['plan']}" in digest
    # Recent orchestrator-relevant events, without the quota noise.
    assert "plan_proposed" in digest and "quota_should_run" not in digest
    # Deterministic: the same state renders the same text.
    assert _digest(fixture, ids["planning"]) == digest
    assert len(digest) <= DIGEST_MAX_CHARS


def test_the_digest_names_an_escalation_and_system_gates(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path, agents=AGENTS)
    criteria, feedback = "k" * 900, "rejected by acc (#2): " + "f" * 500
    blocked = add_goal_todo(
        **_kw(fixture), role="agent", text="Build the badge", task_class="advancement_task", claimed_by="dev",
        role_contract={"required_role": "developer", "requires_acceptance": True, "acceptance_criteria": criteria},
    )["todo_id"]
    update_goal_todo(**_kw(fixture), todo_id=blocked, role="agent", agent_id="dev", status="blocked",
                     reason="escalated", role_contract={"reject_count": 2, "review_feedback": feedback})
    other = add_goal_todo(
        **_kw(fixture), role="agent", text="Other work", task_class="advancement_task", claimed_by="dev",
        role_contract={"required_role": "developer", "acceptance_criteria": "o" * 900},
    )["todo_id"]
    escalation = add_goal_todo(
        **_kw(fixture), role="agent", text=f"Escalation: {blocked} was rejected 2 times by acc. Decide: reassign.",
        task_class="advancement_task", action_kind="replan", claimed_by="orch",
        role_contract={"required_role": "orchestrator", "requires_acceptance": False},
    )["todo_id"]
    gate = add_goal_todo(**_kw(fixture), role="user", task_class="user_gate", agent_id="orch",
                         text="Push request: push it?")["todo_id"]
    from loopx.gate_threads import register_gate_kind

    register_gate_kind(fixture["runtime"], GOAL_ID, gate, kind="push_request")
    digest = _digest(fixture, escalation)
    assert f"escalation of `{blocked}` (rejected twice, now blocked)" in digest
    # The escalated todo is shown whole; other todos are clipped.
    assert f"  criteria: {criteria}\n" in digest and f"  feedback: {feedback}\n" in digest
    assert "o" * 300 not in digest and f"- `{other}` [open]" in digest
    assert f"- `{gate}` kind=push_request awaiting_user: Push request: push it?" in digest


def _large_goal_lines(todos: int) -> list[str]:
    lines = []
    long = "x" * 1200
    statuses = ["open", "in_review", "blocked", "deferred", "done"]
    for index in range(todos):
        status = statuses[index % len(statuses)]
        todo_id = f"todo_{index:012d}"
        meta = [f"todo_id={todo_id}", "role=agent", "task_class=advancement_task", f"status={status}",
                "claimed_by=dev", "required_role=developer", "requires_acceptance=true", "acceptor_agent=acc",
                f"reject_count={index % 3}", "task_repositories=api",
                f"acceptance_criteria={quote('criterion ' + long)}", f"review_feedback={quote('feedback ' + long)}"]
        if status == "done":
            meta.append(f"completed_at=2026-09-{(index % 27) + 1:02d}T00:00:00+00:00")
        box = "x" if status == "done" else " "
        lines += [f"- [{box}] [P1] Todo number {index} {long}", f"  <!-- loopx:todo {' '.join(meta)} -->"]
    return lines


def test_a_very_large_goal_is_truncated_deterministically_within_the_bound(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path, agents=AGENTS, todo_lines=_large_goal_lines(300))
    for index in range(OPEN_GATE_LIMIT + 5):
        gate = add_goal_todo(**_kw(fixture), role="user", task_class="user_gate", agent_id="orch",
                             text=f"Question {index} " + "q" * 400)["todo_id"]
        for turn in range(6):
            reply_to_gate(registry_path=fixture["registry"], runtime_root=fixture["runtime"], goal_id=GOAL_ID,
                          todo_id=gate, text=f"message {turn} " + "m" * 3000)

    digest = _digest(fixture, "todo_000000000000")
    assert len(digest) <= DIGEST_MAX_CHARS
    assert _digest(fixture, "todo_000000000000") == digest
    # Every capped section keeps whole items and says what it left out, and where to read it.
    assert re.search(r"\n- … \d+ more live todos omitted \(loopx todo list\)\n", digest)
    assert re.search(r"\n- … \d+ more closed todos omitted \(loopx todo list --status done\)\n", digest)
    assert re.search(r"\n- … \d+ more open gates omitted \(loopx gate list\)\n", digest)
    assert "earlier messages (loopx gate show --todo-id" in digest
    # No field is copied whole: 1200-character criteria and 3000-character messages are clipped.
    assert "x" * 500 not in digest and "m" * 500 not in digest
    # Live todos come first, by status rank: blocked, in_review, open, deferred.
    shown = [line for line in digest.splitlines() if line.startswith("- `todo_0000")]
    live = [line.split("[", 1)[1].split("]", 1)[0] for line in shown if "[accepted" not in line and "[done" not in line]
    assert live and live == sorted(live, key=["blocked", "in_review", "open", "deferred"].index)
    assert live[0] == "blocked"
    omitted = int(re.search(r"(\d+) more live todos omitted", digest).group(1))
    assert len(live) + omitted == 240 and len(live) <= LIVE_TODO_LIMIT
    # The hard bound still applies on top of the section budgets.
    small = _digest(fixture, "todo_000000000000", max_chars=3_000)
    assert len(small) <= 3_000 and small.endswith(DIGEST_TRUNCATION_MARKER)
    assert small == bound_digest(digest, 3_000)


def test_bound_digest_cuts_on_a_line_with_a_marker() -> None:
    text = "".join(f"line {index}\n" for index in range(100))
    cut = bound_digest(text, 200)
    assert len(cut) <= 200 and cut.endswith(DIGEST_TRUNCATION_MARKER)
    assert all(line.startswith("line ") for line in cut.splitlines()[:-1])
    assert bound_digest("short\n", 200) == "short\n"


def test_an_unreadable_state_degrades_to_a_note() -> None:
    note = orchestrator_digest_or_note(registry_path=Path("/nonexistent/registry.json"),
                                       runtime_root=Path("/nonexistent"), goal_id="missing")
    assert "Unavailable for this Turn" in note and "loopx todo list" in note


def test_orchestrator_launches_carry_the_digest_and_other_roles_do_not(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path, agents={"orch": {"role": "orchestrator"}, "dev": {"role": "developer"}})
    planning = add_goal_todo(
        **_kw(fixture), role="agent", text="Clarify the requirements, then propose a plan", action_kind="plan",
        claimed_by="orch", role_contract={"required_role": "orchestrator", "requires_acceptance": False},
    )["todo_id"]
    work = add_goal_todo(**_kw(fixture), role="agent", text="Build it", task_class="advancement_task",
                         action_kind="fixture", claimed_by="dev")["todo_id"]
    dispatcher = _dispatcher(fixture, should_run=ScriptedShouldRun({"orch": [planning], "dev": [work]}))
    dispatcher.run_once()
    prompts = {row["agent"]: row["system_prompt"] for row in read_jsonl(fixture["turn_log"])}
    orch = prompts["orch"]
    assert ORCHESTRATOR_DIGEST_GUIDANCE in orch
    assert "## LoopX goal state digest (as of launch," in orch
    assert f"This Turn: todo `{planning}` [open], planning todo" in orch
    assert f"- `{work}` [open]" in orch
    # The digest follows the guidance, which follows the orchestrator's CLI rules.
    assert orch.index("loopx plan propose") < orch.index(ORCHESTRATOR_DIGEST_GUIDANCE) < orch.index("## LoopX goal")
    assert "LoopX goal state digest" not in prompts["dev"]


def test_the_digest_of_the_pilot_shaped_goal_stays_small(tmp_path: Path) -> None:
    """A goal the size of the E2E pilot renders far below the bound."""

    fixture, ids = _small_goal(tmp_path)
    for index in range(8):
        add_goal_todo(**_kw(fixture), role="agent", text=f"Work item {index} " + "w" * 120,
                      task_class="advancement_task", claimed_by="dev",
                      role_contract={"required_role": "developer", "requires_acceptance": True,
                                     "task_repositories": ["api"], "acceptance_criteria": "c" * 900})
    digest = _digest(fixture, ids["planning"])
    assert len(digest) < DIGEST_MAX_CHARS // 2, len(digest)
    assert json.dumps(digest)  # plain text, safe to embed
