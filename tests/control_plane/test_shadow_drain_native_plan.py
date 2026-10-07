"""Native recovery plans execute only against the filesystem observations they proved."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from loopx.control_plane.coordination import runtime_shadow_adapter as adapter
from loopx.control_plane.coordination import runtime_shadow_outbox as outbox
from test_runtime_shadow_drain import _drain, _fixture, _record_todo_write


PLAN = "coordination.runtime_shadow.plan_drain"
COMMIT = "coordination.runtime_shadow.commit_entry"


def inventory(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in directory.iterdir()}


def test_adapter_exchanges_observations_and_effects_without_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, state, runtime = _fixture(tmp_path)
    _record_todo_write(registry, state, runtime, "Retained projection text is not drain transport")
    actual = adapter.effect_runtime_result
    seen: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def rpc(method: str, request: dict, **kwargs: Any) -> dict:
        result = actual(method, request, **kwargs)
        seen.append((method, request, result))
        return result

    monkeypatch.setattr(adapter, "effect_runtime_result", rpc)
    result = _drain(registry, runtime)
    assert result.ok and result.delivered == 1
    plans = [(request, response) for method, request, response in seen if method == PLAN]
    assert len(plans) >= 2  # Preflight and post-commit receipt validation.
    assert sum(method == COMMIT for method, _, _ in seen) == 1
    for request, response in plans:
        assert "proof" not in response and "transactions" not in response
        assert "head" not in response["view"]
        for entry in request["entries"]:
            assert set(entry) == {
                "entry_id", "seq", "prepared", "capture_lineage_id",
                "prepared_sha256", "committed_sha256",
            }
    assert any(request["acknowledgement"] is not None for request, _ in plans)
    assert not any(method.endswith("outbox_read") for method, _, _ in seen)


@pytest.mark.parametrize("mutation", ["cursor", "entry", "new_entry"])
def test_filesystem_change_after_native_proof_prevents_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    registry, state, runtime = _fixture(tmp_path)
    _record_todo_write(registry, state, runtime, "Commit with a local concurrent change")
    actual = adapter.effect_runtime_result
    directory = outbox.partition_directory(runtime, "goal-e2e", "todos")
    after_change: dict[str, bytes] = {}

    def rpc(method: str, request: dict, **kwargs: Any) -> dict:
        result = actual(method, request, **kwargs)
        if method == PLAN and request["acknowledgement"] is not None:
            if mutation == "cursor":
                outbox.write_cursor(directory, partition="todos", **result["cursor_update"])
            elif mutation == "entry":
                path = next(directory.glob("*.prepared.json"))
                path.write_bytes(path.read_bytes() + b"\n")
            else:
                _record_todo_write(registry, state, runtime, "Writer arrived after native proof")
            after_change.update(inventory(directory))
        return result

    monkeypatch.setattr(adapter, "effect_runtime_result", rpc)
    result = _drain(registry, runtime)
    assert result.ok is False
    # Formatting-only mutation is caught by byte revalidation even when dataclass
    # equality sees the same decoded entry. Cursor/inventory changes fail earlier.
    assert result.reason_code == "outbox_file_changed"
    assert result.candidate_readback_verified is True
    assert result.reclaimed_residue == 0
    assert inventory(directory) == after_change


def test_forged_commit_ack_keeps_residue_for_a_later_exact_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, state, runtime = _fixture(tmp_path)
    _record_todo_write(registry, state, runtime, "Recover the committed transaction, not its corrupted ACK")
    actual = adapter.effect_runtime_result

    def rpc(method: str, request: dict, **kwargs: Any) -> dict:
        result = actual(method, request, **kwargs)
        return {**result, "provider_revision": "foreign-revision"} if method == COMMIT else result

    monkeypatch.setattr(adapter, "effect_runtime_result", rpc)
    stopped = _drain(registry, runtime)
    assert not stopped.ok and stopped.reason_code == "shadow_commit_entry_result_invalid"
    assert stopped.reclaimed_residue == 0
    monkeypatch.setattr(adapter, "effect_runtime_result", actual)
    recovered = _drain(registry, runtime)
    assert recovered.ok and recovered.replayed == 1 and recovered.delivered == 0


def test_budget_expiring_during_native_read_cannot_authorize_late_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, state, runtime = _fixture(tmp_path)
    _record_todo_write(registry, state, runtime, "Preserve evidence after time expires")
    actual = adapter.effect_runtime_result
    expired = False
    can_reclaim = adapter._DrainBudget.can_reclaim

    def rpc(method: str, request: dict, **kwargs: Any) -> dict:
        nonlocal expired
        result = actual(method, request, **kwargs)
        if method == PLAN and request["acknowledgement"] is not None:
            expired = True
        return result

    def budget(self: Any, count: int) -> bool:
        return not expired and can_reclaim(self, count)

    monkeypatch.setattr(adapter, "effect_runtime_result", rpc)
    monkeypatch.setattr(adapter._DrainBudget, "can_reclaim", budget)
    result = _drain(registry, runtime)
    directory = outbox.partition_directory(runtime, "goal-e2e", "todos")
    assert result.ok and result.delivered == 1
    assert result.budget_exhausted and result.reclaimed_residue == 0
    assert len(outbox.list_entries(directory)) == 1
    assert outbox.read_cursor(directory) is None
    expired = False
    monkeypatch.setattr(adapter, "effect_runtime_result", actual)
    recovered = _drain(registry, runtime)
    assert recovered.ok and recovered.replayed == 1
