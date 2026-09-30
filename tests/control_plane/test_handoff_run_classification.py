"""Post-handoff run classification through the status wrappers.

`loopx.status` binds the handoff read model to the runtime classification
sets. These cases pin the three decisions a status reader relies on:

- handoff-ready: an explicit ready classification, or an approved operator
  gate that carries an agent command;
- external-evidence watch: only structured wait state or an explicit legacy
  external-evidence classification prefix. A classification that merely
  contains a monitor-like word is not an external wait;
- custom post-handoff work: any classified run that is not status-neutral,
  handoff-ready, agent-ready, user/controller, blocking, or an external wait.
"""

from __future__ import annotations

import pytest

from loopx import status as status_module

APPROVED_GATE = {"decision": "approve", "agent_command": "continue"}

# (id, run, handoff_ready, external_evidence_watch, custom_post_handoff_work)
CASES = (
    ("ready-classification", {"classification": "controller_opted_in_waiting_for_run"}, True, False, False),
    ("approved-gate-classification", {"classification": "operator_gate_approved"}, True, False, False),
    ("approved-gate-with-command", {"classification": "custom_delivery", "operator_gate": APPROVED_GATE},
     True, False, False),
    ("approved-gate-without-command", {"classification": "custom_delivery",
                                       "operator_gate": {"decision": "approve"}}, False, False, True),
    ("deferred-gate-with-command", {"classification": "custom_delivery",
                                    "operator_gate": {"decision": "defer", "agent_command": "continue"}},
     False, False, True),
    ("monitor-word-is-not-external", {"classification": "feature_monitoring_not_external"}, False, False, True),
    ("monitor-prefix-is-not-external", {"classification": "monitor_feature_rollout"}, False, False, True),
    ("plain-custom-work", {"classification": "research_progress_written"}, False, False, True),
    ("agent-ready", {"classification": "inspect_result"}, False, False, False),
    ("user-or-controller", {"classification": "needs_user_relay"}, False, False, False),
    ("blocking", {"classification": "blocked_by_safety"}, False, False, False),
    ("waiting-on-external", {"classification": "custom_delivery", "waiting_on": "external_evidence"},
     False, True, False),
    ("execution-waiting-on-external", {"classification": "custom_delivery",
                                       "execution_waiting_on": "external_evidence"}, False, True, False),
    ("external-observation", {"classification": "custom_delivery", "external_evidence_observation": {}},
     False, True, False),
    ("monitor-event-external-mode", {"classification": "custom_delivery",
                                     "monitor_event": {"monitor_mode": "external_signal_watch"}},
     False, True, False),
    ("monitor-event-external-kind", {"classification": "custom_delivery",
                                     "monitor_event": {"monitor_kind": "external_evidence"}}, False, True, False),
    ("monitor-event-waiting-on", {"classification": "custom_delivery",
                                  "monitor_event": {"waiting_on": "external_evidence"}}, False, True, False),
    ("monitor-event-internal-mode", {"classification": "custom_delivery",
                                     "monitor_event": {"monitor_mode": "internal_poll"}}, False, False, True),
    ("legacy-await-prefix", {"classification": "await_fixture"}, False, True, False),
    ("legacy-observation-prefix", {"classification": "external_evidence_observation_fixture"},
     False, True, False),
    ("status-neutral-classification", {"classification": "quota_monitor_poll"}, False, False, False),
    ("status-neutral-agent-lane", {"classification": "custom_delivery", "progress_scope": "agent_lane"},
     False, False, False),
    ("unclassified", {}, False, False, False),
)


@pytest.mark.parametrize(
    ("run", "ready", "external", "custom"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_handoff_run_classification(run: dict[str, object], ready: bool, external: bool, custom: bool) -> None:
    assert status_module.is_handoff_ready_run(run) is ready
    assert status_module.run_has_external_evidence_watch_signal(run) is external
    assert status_module.is_custom_post_handoff_work_run(run) is custom
