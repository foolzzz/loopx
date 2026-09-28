"""Decision 12: a plan card is applied only after the owner approves its plan_approval gate.

``loopx plan apply`` recovers an approved plan whose apply was interrupted; it
never stands in for the owner's approve. The rule reads the gate todo from the
goal's todo authority (Markdown or promoted canonical), not a caller flag.
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
from loopx.todos import complete_goal_todo
from tests.control_plane.test_gates_plans_intake import GOAL, ORCH, PLAN, fixture, rows

PLAN_KEYS = [todo["key"] for todo in PLAN["todos"]]


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
                              decision_outcome=decision, note="owner decision", no_followup=True, agent_id=ORCH)
    assert done["ok"] is True, done
    return done


def _plan_todo_ids(registry: Path, plan_id: str) -> list[str]:
    return sorted(todo_id for todo_id, row in rows(registry).items()
                  if f"Plan {plan_id} item " in str(row.get("note") or ""))


def _decided_events(runtime: Path) -> list[dict]:
    return [event for event in load_rollout_events(rollout_event_log_path(runtime, GOAL))
            if event["event_kind"] == "plan_decided"]


@pytest.mark.parametrize("provider", [None, "file", "sqlite"])
def test_plan_apply_refuses_a_pending_plan_until_the_owner_approves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], provider: str | None,
) -> None:
    registry, runtime, plan = _setup(tmp_path, monkeypatch, provider)
    plan_id, gate_id = plan["plan_id"], plan["gate_todo_id"]
    before = rows(registry)

    code, payload = _cli_apply(registry, runtime, plan_id, capsys)
    assert code == 1, payload
    assert payload["error_code"] == "plan_not_approved"
    assert gate_id in payload["error"] and "gate resolve" in payload["error"]
    with pytest.raises(PlanCardError) as raised:
        apply_plan(registry_path=registry, runtime_root=runtime, goal_id=GOAL, plan_id=plan_id)
    assert raised.value.code == "plan_not_approved"
    # Nothing moved: no todos, the card is still pending, the gate still open, no decision event.
    assert rows(registry) == before
    assert rows(registry)[gate_id]["status"] == "open"
    assert read_plan(runtime, GOAL, plan_id)["status"] == "pending"
    assert _decided_events(runtime) == []

    # The owner's approve still applies the plan, exactly once.
    done = _decide(registry, gate_id, "approve")
    assert done["plan_card"]["status"] == "applied", done["plan_card"]
    ids = done["plan_card"]["todo_id_map"]
    assert list(ids) == PLAN_KEYS
    assert set(rows(registry)) == set(before) | set(ids.values())
    code, payload = _cli_apply(registry, runtime, plan_id, capsys)
    assert code == 0 and payload["already_applied"] is True
    assert _plan_todo_ids(registry, plan_id) == sorted(ids.values())


@pytest.mark.parametrize("provider", [None, "file"])
@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_plan_apply_reads_the_gate_decision_after_an_interrupted_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    provider: str | None, decision: str,
) -> None:
    registry, runtime, plan = _setup(tmp_path, monkeypatch, provider)
    plan_id, gate_id = plan["plan_id"], plan["gate_todo_id"]
    # The gate closes, then the process dies before the plan card is settled.
    real_settle = plan_cards_module.settle_gate_decision
    monkeypatch.setattr(plan_cards_module, "settle_gate_decision", lambda **_kwargs: None)
    _decide(registry, gate_id, decision)
    monkeypatch.setattr(plan_cards_module, "settle_gate_decision", real_settle)
    gate = rows(registry)[gate_id]
    assert (gate["status"], gate["decision_outcome"]) == ("done", decision)
    assert read_plan(runtime, GOAL, plan_id)["status"] == "pending"
    before = rows(registry)

    code, payload = _cli_apply(registry, runtime, plan_id, capsys)
    if decision == "approve":  # recovery: the owner did approve
        assert code == 0, payload
        assert payload["plan"]["status"] == "applied"
        assert list(payload["plan"]["todo_id_map"]) == PLAN_KEYS
        assert _plan_todo_ids(registry, plan_id) == sorted(payload["plan"]["todo_id_map"].values())
    else:  # the owner rejected: plan apply must not turn that into an apply
        assert code == 1, payload
        assert payload["error_code"] == "plan_not_approved"
        assert "reject" in payload["error"]
        assert rows(registry) == before
        assert read_plan(runtime, GOAL, plan_id)["status"] == "pending"


@pytest.mark.parametrize("provider", [None, "file"])
def test_plan_apply_recovers_an_interrupted_apply_without_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], provider: str | None,
) -> None:
    registry, runtime, plan = _setup(tmp_path, monkeypatch, provider)
    plan_id, gate_id = plan["plan_id"], plan["gate_todo_id"]
    before = set(rows(registry))
    # Canonical goals create plan todos one by one; Markdown goals write one batch.
    # The crash is armed only inside the apply, not during the approve preflight's dry run.
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
    assert read_plan(runtime, GOAL, plan_id)["status"] == "applying"
    partial = set(rows(registry)) - before
    assert len(partial) == (2 if provider else 0)  # Markdown is all or nothing

    code, payload = _cli_apply(registry, runtime, plan_id, capsys)
    assert code == 0, payload
    record = payload["plan"]
    assert record["status"] == "applied"
    assert record["apply_mode"] == ("canonical_sequence" if provider else "legacy_batch")
    ids = record["todo_id_map"]
    assert list(ids) == PLAN_KEYS
    assert partial <= set(ids.values())  # the todos created before the crash are reused
    assert set(rows(registry)) == before | set(ids.values())
    assert _plan_todo_ids(registry, plan_id) == sorted(ids.values())
    code, payload = _cli_apply(registry, runtime, plan_id, capsys)
    assert code == 0 and payload["already_applied"] is True
