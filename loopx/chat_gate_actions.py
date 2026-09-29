"""Typed Chat action for resolving an owner decision gate (``gate.resolve``).

The local dashboard is owner-local and loopback-only, so an applied
``gate.resolve`` proposal is the owner's decision. It is written through the
same canonical owner as ``loopx todo complete --decision-outcome``:
:func:`loopx.todos.complete_goal_todo`. The coordination kernel has no owner
actor identity, so the actor of record is the Agent the gate blocks (exactly
what the CLI requires via ``--agent-id``); the receipt and ``authority_reason``
carry the owner/dashboard provenance.

Every write records the proposal's operation id as the gate's completion
identity (Markdown and canonical alike), so a closed gate is this proposal's
own only when it carries that identity; a gate another surface decided makes
the proposal stale. The settlement that follows the closure (the plan, budget,
review, goal-completion or push effect) must finish before the receipt says
applied: one that did not leaves the proposal failed with a typed code, and a
retry re-runs it (a recorded settlement replays, a failed one is retried).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .control_plane.coordination.local_authority import (
    LocalCoordinationAuthorityRejection,
    LocalCoordinationAuthorityUnavailable,
)
from .gate_threads import GATE_DECISION_ACTOR_OWNER, GATE_DECISION_SURFACE_DASHBOARD
from .todos import complete_goal_todo, list_goal_todos

GATE_DURABLE_DECISIONS = frozenset({"approve", "reject", "cancel"})
GATE_AUTHORITY_REASON = "owner decision via local dashboard (loopback)"
_TERMINAL_BASIS_SCHEMA = "loopx_chat_canonical_terminal_basis_v0"
_STALE_AUTHORITY_CODES = frozenset(
    {"provider_revision_mismatch", "authority_source_changed", "provider_revision_conflict"}
)
# Payload keys under which a closed gate's settlement is reported
# (``complete_goal_todo`` and ``settle_gate_decision`` share them).
_PLAN_SETTLEMENT_KEY = "plan_card"
_PUSH_SETTLEMENT_KEY = "push"
# Typed settlements whose failed effect the next settlement of the gate retries
# (``gate_threads.run_gate_settlement``).
_RETRIED_SETTLEMENT_KEYS = ("budget_gate", "goal_complete", "review_gate")


class ChatGateActionMixin:
    """Keep gate.resolve preview/apply parity with the CLI decision path."""

    # -- reads -----------------------------------------------------------------

    def _gate_rows(self, goal_id: str) -> list[dict[str, Any]]:
        rows = list_goal_todos(registry_path=self.registry_path, goal_id=goal_id)
        return [dict(row) for row in rows.get("todos") or [] if isinstance(row, dict)]

    @staticmethod
    def _gate_row(rows: list[dict[str, Any]], goal_id: str, todo_id: str) -> dict[str, Any]:
        gate = next((row for row in rows if row.get("todo_id") == todo_id), None)
        if gate is None:
            raise ValueError(f"gate todo {todo_id!r} was not found in Goal {goal_id!r}")
        return gate

    def _gate_readback(self, goal_id: str, todo_id: str) -> dict[str, Any]:
        rows = self._gate_rows(goal_id)
        gate = self._gate_row(rows, goal_id, todo_id)
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
        gate = self._gate_row(self._gate_rows(goal_id), goal_id, todo_id)
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

    @staticmethod
    def _gate_decider(parameters: dict[str, Any]) -> str:
        """Who ``closed_by`` records: the agent the owner named, else the owner, never the gate's agent."""

        return str(parameters.get("agent_id") or GATE_DECISION_ACTOR_OWNER)

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
        if operation_id is not None:
            # Recorded on the gate by the Markdown and canonical paths alike: the
            # proof that a closed gate is this proposal's own.
            options["completion_turn_key"] = operation_id
        if basis is not None:
            options["terminal_review_basis"] = {
                "provider_revision": basis["provider_revision"],
                "registry_sha256": basis["registry_sha256"],
            }
        return complete_goal_todo(
            registry_path=self.registry_path,
            goal_id=str(parameters["goal_id"]),
            todo_id=str(parameters["todo_id"]),
            role="user",
            decision_outcome=str(parameters["decision"]),
            gate_option=parameters.get("option"),
            note=parameters.get("note"),
            no_followup=True,
            agent_id=actor,
            authority_reason=GATE_AUTHORITY_REASON,
            gate_decision_surface=GATE_DECISION_SURFACE_DASHBOARD,
            gate_decision_actor=self._gate_decider(parameters),
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
            **({"decision_option": parameters["option"]} if parameters.get("option") else {}),
            "decided_by": "owner",
            "surface": "local_dashboard",
            "gate_readback": readback,
            "resource_ids": {"goal_id": goal_id, "todo_id": todo_id},
        }

    def _gate_completion_key(self, goal_id: str, todo_id: str) -> str | None:
        gate = self._gate_row(self._gate_rows(goal_id), goal_id, todo_id)
        return str(gate["completion_turn_key"]) if gate.get("completion_turn_key") else None

    def _settle_gate(self, parameters: dict[str, Any]) -> dict[str, Any]:
        """Re-run the closed gate's settlement, keyed like ``complete_goal_todo``'s payload.

        Settlement is idempotent: a recorded typed outcome replays without
        mutation (a failed budget, review or goal-completion outcome is retried
        with its recorded option) and an applied plan reports
        ``already_applied``; a missing one runs now.
        """

        from .control_plane.coordination.local_authority_shadow_adapter import effective_runtime_root
        from .plan_cards import settle_gate_decision

        settled = settle_gate_decision(
            registry_path=self.registry_path,
            runtime_root=effective_runtime_root(self.registry_path, None),
            goal_id=str(parameters["goal_id"]),
            todo_id=str(parameters["todo_id"]),
            decision=str(parameters["decision"]),
            # Only this proposal's own closure is settled here, so an audit that an
            # interrupted closure did not record yet is the dashboard's.
            surface=GATE_DECISION_SURFACE_DASHBOARD,
            actor=self._gate_decider(parameters),
            option=parameters.get("option"),
            note=parameters.get("note"),
        )
        if settled is None:
            return {}
        return {settled.pop("payload_key", _PLAN_SETTLEMENT_KEY): settled}

    def _record_gate_resolution(
        self, proposal_id: str, proposal: dict[str, Any], parameters: dict[str, Any], *,
        settled: Mapping[str, Any], operation_id: str,
    ) -> dict[str, Any]:
        """Record the applied receipt once the gate's settlement finished.

        ``settled`` carries each settlement under its payload key. A settlement
        that did not finish leaves the proposal failed with a typed code instead
        (see ``_settlement_failure``); the gate decision itself is recorded.
        """

        failure = self._settlement_failure(
            settled, goal_id=str(parameters["goal_id"]), gate_todo_id=str(parameters["todo_id"]),
            operation_id=operation_id,
        )
        if failure is not None:
            error_code, message, details = failure
            failed = self.store.mark_failed(proposal_id, error_code=error_code, message=message, details=details)
            return {"proposal": failed, "turn": None}
        stored = self.store.apply(
            proposal_id,
            current_state_fingerprint=str(proposal["expected_state_fingerprint"]),
            receipt=self._gate_receipt(
                proposal_id, parameters, outcome="gate_resolved", operation_id=operation_id,
                readback=self._gate_readback(str(parameters["goal_id"]), str(parameters["todo_id"])),
            ),
        )
        return {"proposal": stored, "turn": None}

    @staticmethod
    def _settlement_failure(
        settled: Mapping[str, Any], *, goal_id: str, gate_todo_id: str, operation_id: str,
    ) -> tuple[str, str, dict[str, Any]] | None:
        """The typed failure of a settlement that did not finish, else None.

        - An interrupted plan apply: retrying the proposal re-runs the
          idempotent apply (``plan_apply_recovery_required``).
        - A failed budget, goal-completion or review effect: retrying the
          proposal re-runs the recorded option (``gate_settlement_retry_required``).
        - A failed push is recorded on its gate by design and retried through
          the follow-up push gate it opened (``gate_push_failed``).
        The outcome's own error text stays in the gate index (``loopx gate show``).
        """

        def failed(key: str) -> Mapping[str, Any] | None:
            value = settled.get(key)
            return value if isinstance(value, Mapping) and value.get("ok") is False else None

        base = {"operation_id": operation_id, "gate_todo_id": gate_todo_id}
        plan = failed(_PLAN_SETTLEMENT_KEY)
        if plan is not None:
            return (
                "plan_apply_recovery_required",
                "The gate decision is recorded but its plan is not applied yet. Retry this proposal to finish the plan apply.",
                {**base, "plan_id": plan.get("plan_id"), "plan_status": plan.get("status"),
                 "recovery": plan.get("recovery")},
            )
        push = failed(_PUSH_SETTLEMENT_KEY)
        if push is not None:
            return (
                "gate_push_failed",
                "The push was approved but did not complete. Approve its follow-up push gate to retry it.",
                {**base, "retry_gate_todo_id": push.get("follow_up_gate_todo_id")},
            )
        for key in _RETRIED_SETTLEMENT_KEYS:
            outcome = failed(key)
            if outcome is not None:
                return (
                    "gate_settlement_retry_required",
                    "The gate decision is recorded but its effect did not apply. Retry this proposal to re-run it.",
                    {**base, "settlement": key, "option": outcome.get("option"),
                     "inspect": f"loopx gate show --goal-id {goal_id} --todo-id {gate_todo_id}"},
                )
        return None

    def _resume_closed_gate(
        self, proposal_id: str, proposal: dict[str, Any], parameters: dict[str, Any], *,
        readback: dict[str, Any], operation_id: str,
    ) -> dict[str, Any]:
        """A gate that is already closed: finish this proposal's own closure, else stale.

        Only the recorded completion identity proves the closure is this
        proposal's (a lost receipt, or a settlement interrupted after the
        closure); its settlement is then re-run before the receipt is recorded.
        Any other closure is another surface's decision.
        """

        from .chat_actions import _digest

        goal_id = str(parameters["goal_id"])
        todo_id = str(parameters["todo_id"])
        if self._gate_completion_key(goal_id, todo_id) != operation_id:
            stale = self.store.apply(
                proposal_id, current_state_fingerprint=_digest({"gate": readback}), receipt={},
            )
            return {"proposal": stale, "turn": None}
        try:
            settled = self._settle_gate(parameters)
        except OSError as error:
            return self._settlement_interrupted(proposal_id, todo_id, operation_id=operation_id, error=error)
        return self._record_gate_resolution(
            proposal_id, proposal, parameters, settled=settled, operation_id=operation_id,
        )

    def _settlement_interrupted(
        self, proposal_id: str, todo_id: str, *, operation_id: str, error: OSError,
    ) -> dict[str, Any]:
        """Keep the proposal retryable when the settlement could not run or be recorded.

        Covers an I/O failure while recording the settlement and a settlement
        lock timeout (``LockAcquireTimeoutError`` is an ``OSError``). A retry
        resumes the gate through its recorded identity and re-runs the
        settlement from its recorded intent.
        """

        failed = self.store.mark_failed(
            proposal_id, error_code="gate_settlement_retry_required",
            message="The gate settlement did not complete. Retry this proposal to recover it.",
            details={"operation_id": operation_id, "gate_todo_id": todo_id, "reason": type(error).__name__},
        )
        return {"proposal": failed, "turn": None}

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
            return self._resume_closed_gate(
                proposal_id, proposal, parameters, readback=current, operation_id=operation_id,
            )
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
        except ValueError:
            # The completion fence refuses a gate that closed after the checks
            # above under another completion identity, before any settlement
            # runs; the closed gate decides the outcome.
            closed = self._gate_readback(goal_id, todo_id)
            if closed.get("status") != "done":
                raise
            return self._resume_closed_gate(
                proposal_id, proposal, parameters, readback=closed, operation_id=operation_id,
            )
        except OSError as error:
            return self._settlement_interrupted(proposal_id, todo_id, operation_id=operation_id, error=error)
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
        return self._record_gate_resolution(
            proposal_id, proposal, parameters, settled=result, operation_id=operation_id,
        )
