"""Typed Chat action for resolving an owner decision gate (``gate.resolve``).

The local dashboard is owner-local and loopback-only, so an applied
``gate.resolve`` proposal is the owner's decision. It is written through the
same canonical owner as ``loopx todo complete --decision-outcome``:
:func:`loopx.todos.complete_goal_todo`. The coordination kernel has no owner
actor identity, so the actor of record is the Agent the gate blocks (exactly
what the CLI requires via ``--agent-id``); the receipt and ``authority_reason``
carry the owner/dashboard provenance.
"""

from __future__ import annotations

from typing import Any

from .control_plane.coordination.local_authority import (
    LocalCoordinationAuthorityRejection,
    LocalCoordinationAuthorityUnavailable,
)
from .todos import complete_goal_todo, list_goal_todos

GATE_DURABLE_DECISIONS = frozenset({"approve", "reject", "cancel"})
GATE_AUTHORITY_REASON = "owner decision via local dashboard (loopback)"
_TERMINAL_BASIS_SCHEMA = "loopx_chat_canonical_terminal_basis_v0"
_STALE_AUTHORITY_CODES = frozenset(
    {"provider_revision_mismatch", "authority_source_changed", "provider_revision_conflict"}
)


class ChatGateActionMixin:
    """Keep gate.resolve preview/apply parity with the CLI decision path."""

    # -- reads -----------------------------------------------------------------

    def _gate_rows(self, goal_id: str) -> list[dict[str, Any]]:
        rows = list_goal_todos(registry_path=self.registry_path, goal_id=goal_id)
        return [dict(row) for row in rows.get("todos") or [] if isinstance(row, dict)]

    def _gate_readback(self, goal_id: str, todo_id: str) -> dict[str, Any]:
        rows = self._gate_rows(goal_id)
        gate = next((row for row in rows if row.get("todo_id") == todo_id), None)
        if gate is None:
            raise ValueError(f"gate todo {todo_id!r} was not found in Goal {goal_id!r}")
        readback: dict[str, Any] = {
            "todo_id": todo_id,
            "status": gate.get("status"),
            "decision_outcome": gate.get("decision_outcome"),
            "note": gate.get("note"),
        }
        target_id = gate.get("unblocks_todo_id")
        if target_id:
            target = next((row for row in rows if row.get("todo_id") == target_id), None)
            readback["target"] = {
                "todo_id": target_id,
                "status": target.get("status") if target else None,
            }
        return readback

    def _open_gate(self, goal_id: str, todo_id: str) -> dict[str, Any]:
        gate = next(
            (row for row in self._gate_rows(goal_id) if row.get("todo_id") == todo_id),
            None,
        )
        if gate is None:
            raise ValueError(f"gate todo {todo_id!r} was not found in Goal {goal_id!r}")
        if gate.get("role") != "user" or gate.get("task_class") != "user_gate":
            raise ValueError(f"todo {todo_id!r} is not an owner decision gate (user_gate)")
        if gate.get("status") != "open":
            raise ValueError(
                f"gate {todo_id!r} is already {gate.get('status')}; refresh and review its current state"
            )
        return gate

    @staticmethod
    def _gate_actor(gate: dict[str, Any], parameters: dict[str, Any]) -> str | None:
        requested = parameters.get("agent_id")
        bound = gate.get("bound_agent") or gate.get("blocks_agent")
        if requested and bound and requested != bound:
            raise ValueError(
                f"gate {gate.get('todo_id')!r} belongs to agent {bound!r}, not {requested!r}"
            )
        return str(requested or bound) if (requested or bound) else None

    def _gate_terminal_basis(self, goal_id: str) -> dict[str, Any] | None:
        basis = self._canonical_update_basis(goal_id)
        if basis is None:
            return None
        return {**basis, "schema_version": _TERMINAL_BASIS_SCHEMA}

    def _run_gate_complete(
        self,
        parameters: dict[str, Any],
        *,
        actor: str | None,
        basis: dict[str, Any] | None,
        operation_id: str | None,
        dry_run: bool,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if basis is not None:
            options = {
                "completion_turn_key": operation_id,
                "terminal_review_basis": {
                    "provider_revision": basis["provider_revision"],
                    "registry_sha256": basis["registry_sha256"],
                },
            }
        return complete_goal_todo(
            registry_path=self.registry_path,
            goal_id=str(parameters["goal_id"]),
            todo_id=str(parameters["todo_id"]),
            role="user",
            decision_outcome=str(parameters["decision"]),
            note=parameters.get("note"),
            no_followup=True,
            agent_id=actor,
            authority_reason=GATE_AUTHORITY_REASON,
            dry_run=dry_run,
            **options,
        )

    # -- preview ---------------------------------------------------------------

    def _gate_resolve_preview(
        self, normalized: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None, list[str]]:
        """Return (fingerprint, canonical basis, evidence) for a gate decision."""

        from .chat_actions import _digest

        goal_id = str(normalized["goal_id"])
        gate = self._open_gate(goal_id, str(normalized["todo_id"]))
        if normalized["decision"] not in GATE_DURABLE_DECISIONS:
            # Deferral is deliberately not a durable write: a deferred Todo is
            # terminal, which would close the gate without a decision.
            return (
                self._goal_state_fingerprint(goal_id),
                None,
                ["The gate is open; deferring leaves it open and writes nothing."],
            )
        actor = self._gate_actor(gate, normalized)
        basis = self._gate_terminal_basis(goal_id)
        dry = self._run_gate_complete(
            normalized, actor=actor, basis=basis, operation_id=None, dry_run=True
        )
        if dry.get("ok") is not True:
            raise ValueError(
                str(dry.get("error") or "gate decision failed canonical dry-run validation")
            )
        fingerprint = (
            _digest({"goal_id": goal_id, "canonical_update_basis": basis})
            if basis is not None
            else self._goal_state_fingerprint(goal_id)
        )
        return fingerprint, basis, [
            "The open owner decision gate was found and the canonical completion dry-run passed.",
            "Applying records the decision through the same path as `loopx todo complete --decision-outcome`.",
        ]

    # -- apply -----------------------------------------------------------------

    def _gate_receipt(
        self, proposal_id: str, parameters: dict[str, Any], *, outcome: str,
        operation_id: str, readback: dict[str, Any],
    ) -> dict[str, Any]:
        from .chat_actions import _digest

        goal_id = str(parameters["goal_id"])
        todo_id = str(parameters["todo_id"])
        return {
            "receipt_id": _digest({"proposal_id": proposal_id, "todo_id": todo_id})[:32],
            "outcome": outcome,
            "projection_verified": True,
            "operation_id": operation_id,
            "decision_outcome": parameters["decision"],
            "decided_by": "owner",
            "surface": "local_dashboard",
            "gate_readback": readback,
            "resource_ids": {"goal_id": goal_id, "todo_id": todo_id},
        }

    def _apply_gate_resolve(
        self, proposal_id: str, proposal: dict[str, Any], parameters: dict[str, Any]
    ) -> dict[str, Any]:
        from .chat_actions import ProtectedActionGate, _digest

        decision = str(parameters["decision"])
        if decision not in GATE_DURABLE_DECISIONS:
            raise ProtectedActionGate(
                "gate.resolve",
                gate={
                    "kind": "gate_defer_preview_only",
                    "summary": "Deferring a gate writes nothing; the gate stays open and the blocked work stays blocked.",
                    "next_action": "Come back and approve, reject, or cancel the gate when you are ready.",
                },
            )
        goal_id = str(parameters["goal_id"])
        todo_id = str(parameters["todo_id"])
        operation_id = f"chat-gate:{proposal_id}"
        basis = proposal.get("canonical_update_basis")
        current = self._gate_readback(goal_id, todo_id)
        if current.get("status") == "done":
            # Recovery after a committed write whose receipt was lost: the
            # canonical state already carries this exact decision, so record it
            # instead of writing again. A different recorded decision is stale.
            if current.get("decision_outcome") == decision:
                stored = self.store.apply(
                    proposal_id,
                    current_state_fingerprint=str(proposal["expected_state_fingerprint"]),
                    receipt=self._gate_receipt(
                        proposal_id, parameters, outcome="gate_resolved",
                        operation_id=operation_id, readback=current,
                    ),
                )
                return {"proposal": stored, "turn": None}
            stale = self.store.apply(
                proposal_id,
                current_state_fingerprint=_digest({"gate": current}),
                receipt={},
            )
            return {"proposal": stale, "turn": None}
        if basis is None:
            current_fingerprint = self._goal_state_fingerprint(goal_id)
            if current_fingerprint != proposal.get("expected_state_fingerprint"):
                stale = self.store.apply(
                    proposal_id, current_state_fingerprint=current_fingerprint, receipt={}
                )
                return {"proposal": stale, "turn": None}
        gate = self._open_gate(goal_id, todo_id)
        actor = self._gate_actor(gate, parameters)
        try:
            result = self._run_gate_complete(
                parameters, actor=actor, basis=basis,
                operation_id=operation_id, dry_run=False,
            )
        except LocalCoordinationAuthorityRejection:
            raise
        except LocalCoordinationAuthorityUnavailable as error:
            if error.code in _STALE_AUTHORITY_CODES:
                stale = self.store.apply(proposal_id, current_state_fingerprint=_digest({
                    "current": self._goal_state_fingerprint(goal_id), "conflict": error.code,
                }), receipt={})
                return {"proposal": stale, "turn": None}
            failed = self.store.mark_failed(
                proposal_id, error_code="canonical_gate_retry_required",
                message="The gate decision is not yet verified. Retry this proposal to recover it.",
                details={"operation_id": operation_id, "reason_code": error.code},
            )
            return {"proposal": failed, "turn": None}
        if result.get("ok") is not True:
            failed = self.store.mark_failed(
                proposal_id, error_code="canonical_gate_validation_failed",
                message=str(result.get("error") or "Gate completion validation did not pass; the gate is unchanged."),
                details={"operation_id": operation_id, "reason_code": result.get("reason_code")},
            )
            return {"proposal": failed, "turn": None}
        readback = self._gate_readback(goal_id, todo_id)
        if readback.get("status") != "done" or readback.get("decision_outcome") != decision:
            failed = self.store.mark_failed(
                proposal_id, error_code="canonical_gate_readback_mismatch",
                message="The gate decision was submitted but the readback does not show it yet; retry to verify.",
                details={"operation_id": operation_id},
            )
            return {"proposal": failed, "turn": None}
        stored = self.store.apply(
            proposal_id,
            current_state_fingerprint=str(proposal["expected_state_fingerprint"]),
            receipt=self._gate_receipt(
                proposal_id, parameters, outcome="gate_resolved",
                operation_id=operation_id, readback=readback,
            ),
        )
        return {"proposal": stored, "turn": None}
