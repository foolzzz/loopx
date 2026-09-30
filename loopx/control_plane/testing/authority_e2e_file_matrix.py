"""Twelve shared lifecycle scenarios over the file coordination provider.

The Stage 0 ladder row runs these invariant scenarios against the production
coordination executor and ``FileCoordinationProvider``: a same-todo race, an
independent claim, exact replay, identity reuse, stale revisions, lost
responses, and the recoverable-execution renew/reclaim/complete lifecycle.
Each scenario reports one boolean row.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from loopx.control_plane.coordination.executor import (
    CoordinationAuthorityExecutor,
    sample_claim_envelope,
    sample_work_envelope,
)
from loopx.control_plane.coordination.file_provider import FileCoordinationProvider
from loopx.control_plane.coordination.head import bootstrap_head

MATRIX_CLOCK_START = 1_800_000_000.0
PRECONDITIONS: dict[str, Any] = {
    "authorization_projection_revision": 3,
    "authorization_projection_digest": "sha256:bootstrap-auth",
    "dependency_revision": 12,
    "gate_revision": 5,
}


class CoordinationProvider(Protocol):
    def load(self) -> tuple[dict[str, Any] | None, int]: ...

    def store_identity(self) -> str: ...

    def compare_and_put(
        self, expected_provider_generation: int, head: dict[str, Any]
    ) -> dict[str, Any]: ...


class MatrixClock:
    """Deterministic, advanceable executor clock.

    Expiry adjudication is the authority's own decision, so the matrix drives it
    explicitly while the provider underneath stays real.
    """

    def __init__(self, value: float = MATRIX_CLOCK_START) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _LossyOnce:
    """Report the first applied write as ambiguous, as a lost response would."""

    def __init__(self, inner: CoordinationProvider) -> None:
        self.inner = inner
        self.armed = True

    def load(self) -> tuple[dict[str, Any] | None, int]:
        return self.inner.load()

    def store_identity(self) -> str:
        return self.inner.store_identity()

    def compare_and_put(
        self, expected_provider_generation: int, head: dict[str, Any]
    ) -> dict[str, Any]:
        outcome = self.inner.compare_and_put(expected_provider_generation, head)
        if self.armed and outcome.get("result") == "applied":
            self.armed = False
            return {"result": "ambiguous"}
        return outcome


def _todo() -> dict[str, Any]:
    return {
        "todo_revision": 7,
        "status": "open",
        "claimed_by": None,
        "eligibility": {
            "authorization_projection_revision": 3,
            "authorization_projection_digest": "sha256:bootstrap-auth",
            "allowed_agent_ids": ["agent-a", "agent-b"],
            "dependencies_satisfied": True,
            "dependency_revision": 12,
            "gates_open": True,
            "gate_revision": 5,
        },
        "repository": "git:example/repo",
        "code_revision": "0123456789abcdef",
        "last_lease_epoch": 6,
    }


def _work(
    executor: CoordinationAuthorityExecutor,
    agent: str,
    operation_id: str,
    command: dict[str, Any],
) -> dict[str, Any]:
    return executor.apply(
        sample_work_envelope(
            goal_id=executor.goal_id,
            operation_id=operation_id,
            agent_id=agent,
            device_id=f"dev-{agent}",
            command=command,
        )
    )


def _claim(
    executor: CoordinationAuthorityExecutor,
    agent: str,
    todo_id: str,
    operation_id: str,
    *,
    ttl: int = 600,
    revision: int = 7,
) -> dict[str, Any]:
    return executor.apply(
        sample_claim_envelope(
            goal_id=executor.goal_id,
            operation_id=operation_id,
            agent_id=agent,
            device_id=f"dev-{agent}",
            todo_id=todo_id,
            expected_todo_revision=revision,
            expected_preconditions=dict(PRECONDITIONS),
            lease_ttl_seconds=ttl,
        )
    )


def _executor(
    provider: CoordinationProvider,
    goal_id: str,
    now: Callable[[], float] = lambda: MATRIX_CLOCK_START,
) -> CoordinationAuthorityExecutor:
    return CoordinationAuthorityExecutor(provider, goal_id=goal_id, now=now)


def _bootstrap(provider: CoordinationProvider, goal_id: str, todo_ids: tuple[str, ...]) -> None:
    head = bootstrap_head(
        goal_id,
        {todo_id: _todo() for todo_id in todo_ids},
        store_binding=provider.store_identity(),
    )
    provider.compare_and_put(0, head)


def scenario_matrix(make_provider: Callable[[str], CoordinationProvider]) -> dict[str, bool]:
    """Run the shared invariant script and return ``row -> passed``."""

    rows: dict[str, bool] = {}
    goal_id = "g" + uuid.uuid4().hex[:8]
    provider_a = make_provider(goal_id)
    provider_b = make_provider(goal_id)
    assert provider_a.load() == (None, 0)
    head = bootstrap_head(
        goal_id,
        {"todo-1": _todo(), "todo-2": _todo()},
        store_binding=provider_a.store_identity(),
    )
    assert provider_a.compare_and_put(0, head)["result"] == "applied"
    executor_a = _executor(provider_a, goal_id)
    executor_b = _executor(provider_b, goal_id)

    # Same-todo race with two independent handles and a real thread barrier.
    barrier = threading.Barrier(2)
    outcomes: dict[str, dict[str, Any]] = {}

    def race(name: str, executor: CoordinationAuthorityExecutor, agent: str) -> None:
        barrier.wait()
        outcomes[name] = _claim(executor, agent, "todo-1", f"race-{agent}")

    threads = [
        threading.Thread(target=race, args=("a", executor_a, "agent-a")),
        threading.Thread(target=race, args=("b", executor_b, "agent-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    kinds = sorted(outcome["result"] for outcome in outcomes.values())
    rows["same_todo_one_winner"] = kinds == ["applied", "conflict"]

    winner = next(o for o in outcomes.values() if o["result"] == "applied")
    winner_agent = winner["original_receipt"]["actor"]["agent_id"]
    other_agent = "agent-b" if winner_agent == "agent-a" else "agent-a"

    # An independent todo applies for the other endpoint after an internal rebase.
    second = _claim(executor_b, other_agent, "todo-2", "independent-1")
    rows["independent_todo_applies"] = second["result"] == "applied"

    # Exact replay across a reconstructed executor.
    replay_executor = _executor(make_provider(goal_id), goal_id)
    replayed = _claim(replay_executor, winner_agent, "todo-1", f"race-{winner_agent}")
    rows["replay_returns_original_receipt"] = (
        replayed["result"] == "already_applied"
        and replayed["original_receipt"] == winner["original_receipt"]
    )

    # Identity reuse with different semantics.
    mutated = _claim(replay_executor, winner_agent, "todo-1", f"race-{winner_agent}", ttl=601)
    rows["identity_mismatch_rejected"] = (
        mutated["result"] == "rejected" and mutated["reason"] == "operation_identity_mismatch"
    )

    # A stale caller revision conflicts without a state change.
    head_now, generation_now = provider_a.load()
    stale = _claim(executor_a, winner_agent, "todo-2", "stale-1")
    rows["stale_revision_conflicts"] = (
        stale["result"] == "conflict" and provider_a.load() == (head_now, generation_now)
    )

    # A lost response after commit recovers through the receipt index.
    goal2 = "g" + uuid.uuid4().hex[:8]
    provider2 = make_provider(goal2)
    _bootstrap(provider2, goal2, ("todo-1",))
    lost = _claim(_executor(_LossyOnce(provider2), goal2), "agent-a", "todo-1", "lost-1")
    rows["lost_response_recovers_receipt"] = lost["result"] == "already_applied"

    final_head, _ = provider_a.load()
    assert final_head is not None
    rows["receipts_retained"] = len(final_head["receipt_index"]) == 2
    rows["authority_revision_advanced_twice"] = final_head["authority_revision"] == 2

    rows.update(_recoverable_execution_rows(make_provider))
    return rows


def _recoverable_execution_rows(
    make_provider: Callable[[str], CoordinationProvider],
) -> dict[str, bool]:
    """Renew, reclaim, fenced writeback, and atomic successor completion."""

    rows: dict[str, bool] = {}
    goal_id = "g" + uuid.uuid4().hex[:8]
    provider = make_provider(goal_id)
    clock = MatrixClock()
    _bootstrap(provider, goal_id, ("todo_parent01", "todo_other01"))
    executor = _executor(provider, goal_id, clock)
    first = _claim(executor, "agent-a", "todo_parent01", "r3-claim")
    fence = {
        "lease_id": first["original_receipt"]["lease_id"],
        "expected_lease_epoch": first["original_receipt"]["lease_epoch"],
    }
    renewed = _work(executor, "agent-a", "r3-renew", {
        "type": "renew_work", "todo_id": "todo_parent01",
        "expected_todo_revision": 8, **fence, "lease_ttl_seconds": 600,
    })
    rows["renew_extends_the_active_lease"] = (
        renewed["result"] == "applied"
        and renewed["original_receipt"]["lease_epoch"] == first["original_receipt"]["lease_epoch"]
    )

    clock.value += 600 + 31
    reclaimed = _work(executor, "agent-b", "r3-reclaim", {
        "type": "reclaim_work", "todo_id": "todo_parent01",
        "expected_todo_revision": 9,
        "expected_preconditions": dict(PRECONDITIONS),
        "lease_ttl_seconds": 600,
    })
    rows["expired_lease_reclaimed_with_new_epoch"] = (
        reclaimed["result"] == "applied"
        and reclaimed["original_receipt"]["lease_epoch"]
        == first["original_receipt"]["lease_epoch"] + 1
        and reclaimed["original_receipt"]["superseded_owner"] == "agent-a"
    )

    stale = _work(executor, "agent-a", "r3-stale-writeback", {
        "type": "complete_work", "todo_id": "todo_parent01",
        "expected_todo_revision": 10, **fence,
        "no_followup": False, "successor_todo_ids": [], "evidence": None,
    })
    rows["superseded_executor_cannot_write_back"] = (
        stale["result"] == "rejected" and stale["reason"] == "stale_lease_fence"
    )

    new_fence = {
        "lease_id": reclaimed["original_receipt"]["lease_id"],
        "expected_lease_epoch": reclaimed["original_receipt"]["lease_epoch"],
    }
    done = _work(executor, "agent-b", "r3-complete", {
        "type": "complete_work", "todo_id": "todo_parent01",
        "expected_todo_revision": 10, **new_fence,
        "no_followup": False, "successor_todo_ids": ["todo_next01"],
        "evidence": None,
    })
    successor_claim = (
        _claim(executor, "agent-a", "todo_next01", "r3-successor", revision=0)
        if done["result"] == "applied"
        else {"result": "skipped"}
    )
    head, _ = provider.load()
    assert head is not None
    rows["complete_creates_claimable_successor_atomically"] = (
        done["result"] == "applied"
        and done["original_receipt"]["completion_continuation"] == "successor"
        and successor_claim["result"] == "applied"
        and head["coordination"]["todos"]["todo_parent01"]["status"] == "done"
        and "todo_parent01" not in head["coordination"]["leases"]
    )
    return rows


def file_matrix(root: Path) -> dict[str, bool]:
    """Run the scenario matrix over a fresh file coordination store under ``root``."""

    return scenario_matrix(
        lambda goal_id: FileCoordinationProvider(root / "coordination", goal_id)
    )
