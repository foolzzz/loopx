"""Decision 12: a plan card is applied only once its plan_approval gate is recorded done with decision approve.

``loopx plan apply`` recovers such a plan when its settlement or apply was
interrupted; it never stands in for the gate decision. The rule reads the gate
todo from the goal's todo authority (Markdown or promoted canonical, archived
rows included), not a caller flag. It checks the recorded decision, not who
recorded it: an orchestrator closing its own plan gate is a separate authority
boundary that these tests do not cover.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import loopx.plan_cards as plan_cards_module
import loopx.todos as todos_module
from loopx.cli import main
from loopx.plan_cards import PlanCardError, apply_plan, propose_plan, read_plan
from loopx.rollout_event_log import load_rollout_events, rollout_event_log_path
from loopx.todos import archive_completed_todos, complete_goal_todo, list_goal_todos
from tests.control_plane.test_gates_plans_intake import GOAL, ORCH, PLAN, fixture, rows

PLAN_KEYS = [todo["key"] for todo in PLAN["todos"]]
PROVIDERS = [None, "file", "sqlite"]


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str | None) -> tuple[Path, Path, dict]:
    if provider == "sqlite":
        from canonical_authority_fixture import isolate_sqlite_runtime

        isolate_sqlite_runtime(tmp_path, monkeypatch)
    registry, runtime = fixture(tmp_path, provider)
    plan = propose_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, agent_id=ORCH, plan=PLAN)["plan"]
    assert plan["status"] == "pending"
    return registry, runtime, plan


def _cli_apply(registry: Path, runtime: Path, plan_id: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    code = main(["--registry", str(registry), "--runtime-root", str(runtime), "--format", "json",
                 "plan", "apply", "--goal-id", GOAL, "--plan-id", plan_id])
    return code, json.loads(capsys.readouterr().out)


def _decide(registry: Path, gate_id: str, decision: str) -> dict:
    done = complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=gate_id, role="user",
                              decision_outcome=decision, note="gate decision", no_followup=True, agent_id=ORCH)
    assert done["ok"] is True, done
    return done


def _close_gate_without_settlement(monkeypatch: pytest.MonkeyPatch, registry: Path, gate_id: str, decision: str) -> None:
    """The gate closes, then the process dies before the plan card is settled."""

    real_settle = plan_cards_module.settle_gate_decision
    monkeypatch.setattr(plan_cards_module, "settle_gate_decision", lambda **_kwargs: None)
    _decide(registry, gate_id, decision)
    monkeypatch.setattr(plan_cards_module, "settle_gate_decision", real_settle)


def _approve_and_crash_at_the_third_plan_todo(
    monkeypatch: pytest.MonkeyPatch, registry: Path, plan_id: str, gate_id: str, provider: str | None,
) -> dict:
    """Approve the gate; the apply dies while it writes the plan's third todo.

    Canonical goals create plan todos one by one; Markdown goals write one batch.
    The crash is armed only inside the apply, not during the approve preflight's dry run.
    """

    target = "add_goal_todo" if provider else "add_todo_to_lines"
    real_add, real_apply = getattr(todos_module, target), plan_cards_module.apply_plan
    applying: list[bool] = []
    plan_writes: list[str] = []

    def armed_apply(**kwargs):
        applying.append(True)
        try:
            return real_apply(**kwargs)
        finally:
            applying.clear()

    def crash_on_the_third_plan_todo(*args, **kwargs):
        if applying and f"Plan {plan_id} item " in str(kwargs.get("note") or ""):
            plan_writes.append(str(kwargs["note"]))
            if len(plan_writes) == 3:
                raise RuntimeError("synthetic crash mid-apply")
        return real_add(*args, **kwargs)

    monkeypatch.setattr(plan_cards_module, "apply_plan", armed_apply)
    monkeypatch.setattr(todos_module, target, crash_on_the_third_plan_todo)
    done = _decide(registry, gate_id, "approve")
    monkeypatch.setattr(todos_module, target, real_add)
    monkeypatch.setattr(plan_cards_module, "apply_plan", real_apply)
    assert len(plan_writes) == 3
    assert done["plan_card"]["ok"] is False and "loopx plan apply" in done["plan_card"]["recovery"]
    return done


def _plan_todo_ids(registry: Path, plan_id: str) -> list[str]:
    return sorted(todo_id for todo_id, row in rows(registry).items()
                  if f"Plan {plan_id} item " in str(row.get("note") or ""))


def _decided_events(runtime: Path) -> list[dict]:
    return [event for event in load_rollout_events(rollout_event_log_path(runtime, GOAL))
            if event["event_kind"] == "plan_decided"]


def _assert_applied_once(registry: Path, record: dict, before: set[str], partial: set[str] = frozenset()) -> None:
    assert record["status"] == "applied"
    ids = record["todo_id_map"]
    assert list(ids) == PLAN_KEYS
    assert partial <= set(ids.values())  # todos created before a crash are reused
    assert set(rows(registry)) == before | set(ids.values())
    assert _plan_todo_ids(registry, record["plan_id"]) == sorted(ids.values())


@pytest.mark.parametrize("provider", PROVIDERS)
def test_plan_apply_refuses_a_pending_plan_until_its_gate_is_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], provider: str | None,
) -> None:
    registry, runtime, plan = _setup(tmp_path, monkeypatch, provider)
    plan_id, gate_id = plan["plan_id"], plan["gate_todo_id"]
    before = rows(registry)

    code, payload = _cli_apply(registry, runtime, plan_id, capsys)
    assert code == 1, payload
    assert payload["error_code"] == "plan_not_approved"
    assert gate_id in payload["error"] and "still open" in payload["error"]
    with pytest.raises(PlanCardError) as raised:
        apply_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, plan_id=plan_id)
    assert raised.value.code == "plan_not_approved"
    # Nothing moved: no todos, the card is still pending, the gate still open, no decision event.
    assert rows(registry) == before
    assert rows(registry)[gate_id]["status"] == "open"
    assert read_plan(runtime, GOAL, plan_id)["status"] == "pending"
    assert _decided_events(runtime) == []

    # Approving the gate still applies the plan, exactly once.
    done = _decide(registry, gate_id, "approve")
    assert done["plan_card"]["status"] == "applied", done["plan_card"]
    _assert_applied_once(registry, read_plan(runtime, GOAL, plan_id), set(before))
    code, payload = _cli_apply(registry, runtime, plan_id, capsys)
    assert code == 0 and payload["already_applied"] is True


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_plan_apply_reads_the_gate_decision_after_an_interrupted_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    provider: str | None, decision: str,
) -> None:
    registry, runtime, plan = _setup(tmp_path, monkeypatch, provider)
    plan_id, gate_id = plan["plan_id"], plan["gate_todo_id"]
    _close_gate_without_settlement(monkeypatch, registry, gate_id, decision)
    gate = rows(registry)[gate_id]
    assert (gate["status"], gate["decision_outcome"]) == ("done", decision)
    assert read_plan(runtime, GOAL, plan_id)["status"] == "pending"
    before = rows(registry)

    code, payload = _cli_apply(registry, runtime, plan_id, capsys)
    if decision == "approve":  # recovery: the gate is recorded approved
        assert code == 0, payload
        _assert_applied_once(registry, payload["plan"], set(before))
    else:  # a recorded reject must not turn into an apply
        assert code == 1, payload
        assert payload["error_code"] == "plan_not_approved"
        assert "closed with reject" in payload["error"]
        assert rows(registry) == before
        assert read_plan(runtime, GOAL, plan_id)["status"] == "pending"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_plan_apply_finds_the_archived_gate_of_an_approved_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], provider: str | None,
) -> None:
    registry, runtime, plan = _setup(tmp_path, monkeypatch, provider)
    plan_id, gate_id = plan["plan_id"], plan["gate_todo_id"]
    _close_gate_without_settlement(monkeypatch, registry, gate_id, "approve")
    archived = archive_completed_todos(registry_path=registry, goal_id=GOAL, role="user", max_active_done=0,
                                       dry_run=False)
    assert archived["ok"] is True and archived["moved_count"] == 1, archived
    before = set(rows(registry))
    assert gate_id not in before  # the active listing no longer shows the gate
    [gate] = list_goal_todos(registry_path=registry, goal_id=GOAL, todo_id=gate_id)["todos"]
    assert (gate["status"], gate["decision_outcome"], gate["archive_state"]) == ("done", "approve", "archive")

    code, payload = _cli_apply(registry, runtime, plan_id, capsys)
    assert code == 0, payload
    _assert_applied_once(registry, payload["plan"], before)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_plan_apply_recovers_an_interrupted_apply_without_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], provider: str | None,
) -> None:
    registry, runtime, plan = _setup(tmp_path, monkeypatch, provider)
    plan_id, gate_id = plan["plan_id"], plan["gate_todo_id"]
    before = set(rows(registry))
    _approve_and_crash_at_the_third_plan_todo(monkeypatch, registry, plan_id, gate_id, provider)
    assert read_plan(runtime, GOAL, plan_id)["status"] == "applying"
    partial = set(rows(registry)) - before
    assert len(partial) == (2 if provider else 0)  # Markdown is all or nothing

    code, payload = _cli_apply(registry, runtime, plan_id, capsys)
    assert code == 0, payload
    assert payload["plan"]["apply_mode"] == ("canonical_sequence" if provider else "legacy_batch")
    _assert_applied_once(registry, payload["plan"], before, partial)
    code, payload = _cli_apply(registry, runtime, plan_id, capsys)
    assert code == 0 and payload["already_applied"] is True


@pytest.mark.parametrize("provider", PROVIDERS)
def test_replaying_the_approve_through_settlement_recovers_an_interrupted_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str | None,
) -> None:
    registry, runtime, plan = _setup(tmp_path, monkeypatch, provider)
    plan_id, gate_id = plan["plan_id"], plan["gate_todo_id"]
    before = set(rows(registry))
    _approve_and_crash_at_the_third_plan_todo(monkeypatch, registry, plan_id, gate_id, provider)
    partial = set(rows(registry)) - before

    # The same gate decision again: the closed gate is unchanged and settlement resumes the apply.
    replay = _decide(registry, gate_id, "approve")
    assert replay["plan_card"]["ok"] is True and replay["plan_card"]["status"] == "applied", replay["plan_card"]
    assert rows(registry)[gate_id]["decision_outcome"] == "approve"
    _assert_applied_once(registry, read_plan(runtime, GOAL, plan_id), before, partial)
    assert len(_decided_events(runtime)) == 1


@pytest.mark.parametrize("provider", PROVIDERS)
def test_replaying_an_approve_through_settlement_does_not_apply_a_recorded_reject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str | None,
) -> None:
    registry, runtime, plan = _setup(tmp_path, monkeypatch, provider)
    plan_id, gate_id = plan["plan_id"], plan["gate_todo_id"]
    _close_gate_without_settlement(monkeypatch, registry, gate_id, "reject")
    before = rows(registry)

    try:
        replay = complete_goal_todo(registry_path=registry, goal_id=GOAL, todo_id=gate_id, role="user",
                                    decision_outcome="approve", note="changed my mind", no_followup=True,
                                    agent_id=ORCH)
    except ValueError:  # a canonical provider refuses a conflicting replay of the closed gate
        replay = None
    if replay is not None:  # the gate keeps its reject; settlement gets the caller's approve
        assert replay["plan_card"]["ok"] is False, replay["plan_card"]
        assert "not approved" in replay["plan_card"]["error"]
    assert rows(registry) == before
    assert rows(registry)[gate_id]["decision_outcome"] == "reject"
    assert read_plan(runtime, GOAL, plan_id)["status"] == "pending"
    assert _decided_events(runtime) == []
