"""role_v1 goals owe no cadence replan from run history (fork decision 39).

The run-history replan (``replan_history.ts``) raises two kinds of trigger:

* stall triggers (``typed_progress_repeat``, ``dead_monitor_repeat``,
  ``blocked_successor_no_progress_repeat``, ``monitor_no_change_streak``):
  a lane made no progress. Under role_v1 they are routed to the orchestrator
  (S1) and the dispatcher opens an orchestrator action todo for them;
* the cadence trigger ``periodic_review_due``: a lane recorded a number of
  durable runs since the last replan ACK, whether it progressed or not.

Under role_v1 every orchestrator Turn reviews the goal state (decision 31) and
the orchestrator is event-triggered, so a cadence review is no stuck case: it
only woke an idle orchestrator once developer and acceptor Turns crossed the
threshold. Obligations are derived on read, so this module drops obligations
whose every trigger is a cadence trigger before the goal frontier selects one.
Stall obligations and all peer_v1 obligations are left unchanged.
"""

from __future__ import annotations

from typing import Any

from ...agents.runtime_model import AgentRuntimeModel, orchestrator_owns_planning_review

ROLE_V1_UNDERIVED_REPLAN_CADENCE_TRIGGERS = frozenset({"periodic_review", "periodic_review_due"})


def replan_obligation_is_cadence_only(obligation: Any) -> bool:
    if not isinstance(obligation, dict):
        return False
    triggers = obligation.get("triggers")
    kinds = {
        str(trigger.get("kind") or "")
        for trigger in (triggers if isinstance(triggers, list) else [])
        if isinstance(trigger, dict)
    }
    return bool(kinds) and kinds <= ROLE_V1_UNDERIVED_REPLAN_CADENCE_TRIGGERS


def _without_cadence_obligations(source: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(source, dict):
        return source
    revised = dict(source)
    if replan_obligation_is_cadence_only(revised.get("autonomous_replan_obligation")):
        revised.pop("autonomous_replan_obligation")
    by_agent = revised.get("autonomous_replan_obligations_by_agent")
    if isinstance(by_agent, dict):
        kept = {
            agent: obligation
            for agent, obligation in by_agent.items()
            if not replan_obligation_is_cadence_only(obligation)
        }
        if kept:
            revised["autonomous_replan_obligations_by_agent"] = kept
        else:
            revised.pop("autonomous_replan_obligations_by_agent")
    return revised


def replan_obligation_sources(
    item: dict[str, Any],
    project_asset: dict[str, Any] | None,
    agent_runtime_model: AgentRuntimeModel | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The item and project asset the goal frontier selects a replan from.

    peer_v1 (and an unknown model) selects from the sources unchanged; role_v1
    selects from copies without cadence-only obligations.
    """

    if not orchestrator_owns_planning_review(agent_runtime_model):
        return item, project_asset
    return _without_cadence_obligations(item) or {}, _without_cadence_obligations(project_asset)
