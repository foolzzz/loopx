"""Small no-tools decision probes; oracles are not exposed to the model.

These test review reasoning on supplied evidence, not repository investigation.
Live execution is opt-in and uses the existing bounded provider transport.
"""

from __future__ import annotations

import json
from pathlib import Path


from loopx.capabilities.pr_review_queue.review_contract import (
    build_agent_response_contract,
)

# Public historical code/evidence inputs are separate from reviews and oracles;
# the model must reason from the former, not imitate the published conclusion.
HISTORY = json.loads((Path(__file__).parents[2] /
    "examples/fixtures/pr-review-history/cases.json").read_text())
HISTORICAL_CASES = [(case["scenario"], case["expected_verdict"], case["case_family"])
                    for case in HISTORY]

# Positive twins prevent an always-reject policy from passing this corpus.
CASES = [
    (
        {
            "request": "Review a staged storage migration after its decoder fix.",
            "problem": "Documents must remain writable before promotion; new SQL storage needs atomic archiving.",
            "repository_rule": "Retention policy has one owner; storage adapters may coexist during migration.",
            "code": "legacy.archive: parse_document(); done = filter_done(); keep = standing_decisions(done); move = oldest(done - keep, limit); write_document(move)\n"
                    "native.archive: read_head(); done = filter_done(); keep = standing_decisions(done); move = oldest(done - keep, limit); cas_commit(move)",
            "evidence": "Both routes have active callers. Native File and SQL tests pass; sampled old/new outputs match. Independent selectors remain in unchanged legacy and new native code. Python grows 600 lines for private effects and compatibility. Author says legacy storage explains all retained rules. Input validation and CI pass.",
        },
        "REQUEST_CHANGES",
        "architecture",
    ),
    (
        {
            "request": "Review a staged storage migration.",
            "problem": "Documents must remain writable before promotion; new SQL storage needs atomic archiving.",
            "repository_rule": "Retention policy has one owner; storage adapters may coexist during migration.",
            "code": "legacy.archive: facts = parse_document(); ids = shared_selector(facts, limit); write_document(ids)\n"
                    "native.archive: facts = read_head(); ids = shared_selector(facts, limit); cas_commit(ids)",
            "evidence": "Replaced legacy rules are deleted. Extra Python is private validation execution and required document I/O with real callers and an exit condition; product still grows 600 lines. Real CLI pre/post promotion preserves retention and order; a selector mutation fails the independent oracle. Real File/SQL integration, recovery and installed routes pass; round trips are bounded. Other applicable evidence is verified.",
        },
        "APPROVE",
        "none",
    ),
    (
        {
            "request": "Re-review the whole PR after its latest test fix.",
            "problem": "Sequential worker segments share one workspace. No old segment may write after the next starts.",
            "code": "on_timeout: killpg(TERM); wait(parent, 1s); if parent_wait_timed_out: killpg(KILL); return timed_out\ncaller: on timed_out: probe_progress(); start_next_segment()",
            "evidence": "A child in the same group ignores TERM. Parent exits on TERM. Existing tests mock parent.wait to return success and assert TERM was sent. CI is green. Previous reviewer approved the design; last commit only fixes a timing-sensitive test.",
        },
        "REQUEST_CHANGES",
        "lifecycle",
    ),
    (
        {
            "request": "Review a bounded sequential worker adapter.",
            "problem": "Segments share a workspace. Existing runner has a verified per-segment containment handle.",
            "code": "stop(segment_handle); if not wait_empty(segment_handle, deadline): return cleanup_failed\nprobe_progress(); start_next_segment()",
            "evidence": "Real containment tests cover TERM-resistant descendants, parent exit with a surviving child, normal completion, and drain timeout; failed drain invokes no probe or successor. The adapter reuses the existing lifecycle owner. Current callers and platform paths pass. No new registry or scheduler; unchanged entrypoints pass paired baseline tests. Scope and exact head are verified.",
        },
        "APPROVE",
        "none",
    ),
    (
        {
            "request": "Review an optional export integrity change.",
            "problem": "An optional export command sometimes reads the wrong run's evidence. Existing records already have stable run ids and a canonical serializer.",
            "repository_rule": "Extend the nearest owner; optional workflows must not change baseline paths.",
            "proposal": "Add binding check plus a new global journal, scheduler and custom dual-file transaction used by all exports. New CLI calls each new module. No independently demonstrated consumer needs the journal/scheduler. Non-POSIX exports now reject even with the feature disabled.",
            "evidence": "Feature-on tests pass. Latest comment about a Windows lock test is fixed and CI is green. Previous approval says all production mechanisms are coherent.",
        },
        "REQUEST_CHANGES",
        "architecture",
    ),
    (
        {
            "request": "Review an optional export integrity change.",
            "problem": "An optional export command sometimes reads the wrong run's evidence. Existing records have stable run ids and a canonical serializer.",
            "repository_rule": "Extend the nearest owner; optional workflows must not change baseline paths.",
            "proposal": "Add a typed exact run-id comparison at the existing export owner. On mismatch return a diagnostic before writing. Reuse canonical serialization; no new state, scheduler or providers.",
            "evidence": "Real CLI rejects wrong-run evidence without effects, accepts matching evidence and separately reads back the output. Paired feature-off baseline/head tests pass on supported platforms. Current caller inventory and exact head checked. A P2 suggestion to rename a local variable is the only remaining finding.",
        },
        "APPROVE",
        "none",
    ),
    (
        {
            "request": "Review integration readiness of two related open PRs.",
            "code": "PR A changes parse_request from returning 5 values to 6, including for legacy input. PR B calls the same function and destructures exactly 5 values in its public CLI.",
            "evidence": "Both standalone suites pass. Their changed source lines do not overlap. With A's real parser and B's public entrypoint, the same valid legacy input changes from successful preview to invalid_input. Last comments on documentation were resolved.",
        },
        "REQUEST_CHANGES",
        "integration",
    ),
    (
        {
            "request": "Review integration readiness of two related open PRs.",
            "code": "Both PRs use a shared named ParsedRequest with an optional binding digest. Legacy remains accepted; unsupported newer protocol is rejected explicitly, not silently downgraded.",
            "evidence": "An integrated exact head exercises both real CLI consumers with legacy, bound, and invalid inputs. Receipts are independently read back, binding is retained where supported, feature-off matches baseline, and invalid input has no effects. Repository ownership is unchanged and both shipped callers require this small seam. Other applicable evidence is verified. No unresolved findings.",
        },
        "APPROVE",
        "none",
    ),
    (
        {
            "request": "Review a team-work delivery slice against its accepted outcome.",
            "problem": "The owner needs worker B to consume worker A's accepted artifact after restart.",
            "proposal": "Add a handoff status field, serializer and test. The producer and consumer are left to later unspecified PRs; title says peer handoff delivered.",
            "evidence": "Serialization tests pass. No runtime path consumes the field; B still cannot see A's result. The same owner could complete the existing bounded exchange path in this slice without new authority.",
        },
        "REQUEST_CHANGES",
        "architecture",
    ),
    (
        {
            "request": "Review a prerequisite for durable peer handoff, not the whole team feature.",
            "problem": "Receiver B loses A's accepted artifact reference on restart.",
            "proposal": "Repair the existing persisted reference and independent readback. Automatic wake remains in existing scheduler task #43; the owning scheduler team consumes this contract next.",
            "evidence": "Real A→store→B restart and stale-reference negative tests pass; ownership and default behavior are preserved. Separate wake integration has a different retry owner and rollback boundary. Remaining gap is explicitly disclosed; all applicable review evidence verified.",
        },
        "APPROVE",
        "none",
    ),
    (
        {
            "request": "Review a maintenance change with no product-roadmap id.",
            "problem": "A supported release's documented install command is broken.",
            "proposal": "Correct the existing command and delete the stale alternative. No new capability or runtime behavior.",
            "evidence": "The exact command succeeds from the released package in a clean environment. Documentation links and public-boundary checks pass. The requested repair is complete; no further task is needed.",
        },
        "APPROVE",
        "none",
    ),
    (
        {
            "request": "Review a correct patch for the current user request.",
            "problem": "The user changed priority to restoring lost result delivery, and withdrew the earlier dashboard redesign request.",
            "proposal": "Deliver the old dashboard redesign with passing rendering tests and a polished completion report. No result-delivery path is changed.",
            "evidence": "The current request and owner correction are available. The author cites only the superseded task. The redesign has no demonstrated prerequisite relationship to restoring delivery.",
        },
        "REQUEST_CHANGES",
        "architecture",
    ),
]


# Same symptom, different compatibility obligations. Do not reward blanket
# version deletion, blanket retention, or approval after only the last bug fix.
# Breaking a durable reader/writer contract is integration; redundant live
# dispatch paths without independent consumers are an architecture cost.
COMPATIBILITY_CASES = [
    (
        {
            "request": "Re-review recurring-job completion after the reported replay bug was fixed.",
            "problem": "A reopened job must complete its new cycle without replaying old success.",
            "proposal": "Keep an old request version for named operations and add a new version where null id means current cycle. Both enter the same transaction. Author says old receipts require the old request version.",
            "evidence": "Complete caller inventory finds only one adapter and runtime shipped in the same package, selected by source fingerprint. Requests are transient; persisted receipts store ids and intent digests, not request versions. A single current request with named explicit/current-cycle variants preserves both meanings and hashes. Real old-receipt readback and cycle/race tests pass for that smaller design. No external old clients or delayed request queues exist. Prior review fixed legacy fallback and all correctness tests pass.",
            "repository_rule": "Remove avoidable permanent protocol branches when one equally validated contract serves every supported caller.",
        },
        "REQUEST_CHANGES",
        "architecture",
    ),
    (
        {
            "request": "Review recurring-job completion with a new current-cycle mode.",
            "problem": "New cycles must complete independently while supported offline clients can retry named operations.",
            "proposal": "Keep a thin old-request decoder, normalize both formats to a typed explicit/current-cycle intent, and share one transaction and receipt owner.",
            "evidence": "Named deployed mobile releases ship independently and must remain supported for 90 days. Their old request bytes still arrive. Caller inventory, release policy, mixed-client integration, historical receipt replay and cycle/race tests pass. Decoder retirement is tied to expiry of that supported window. No duplicated state rule or implicit null mode remains; all other evidence is verified.",
        },
        "APPROVE",
        "none",
    ),
    (
        {
            "request": "Review a protocol cleanup for a co-packaged adapter and runtime.",
            "problem": "Reduce duplicate request decoders without losing pending durable work after restart.",
            "proposal": "Delete the old request decoder because all live callers upgrade together.",
            "evidence": "The durable retry queue stores complete old-version request bodies. After upgrade, its real restart test rejects those pending jobs before dispatch. No migration or draining step exists. New live requests work and receipt readback passes. The author argues source fingerprinting makes all old formats unnecessary.",
        },
        "REQUEST_CHANGES",
        "integration",
    ),
    (
        {
            "request": "Review a protocol cleanup for a co-packaged adapter and runtime.",
            "problem": "Reduce duplicate live request paths while preserving durable pending work after restart.",
            "proposal": "Use one typed current request for all live calls; retain the old durable-queue decoder only at the replay boundary and normalize to the same owner.",
            "evidence": "Caller inventory proves coordinated upgrades. Persisted pending requests require the historical reader until migration drains them; result receipts separately keep their original ids. Real pre-upgrade queue restart, live call, cycle/race, negative identity and immutable receipt tests pass. No duplicate business rule remains and other applicable evidence is verified. A local naming suggestion is P2 and optional.",
        },
        "APPROVE",
        "none",
    ),
]
CASES.extend(COMPATIBILITY_CASES)

# Scope approval and subject readiness are distinct; refusal is not recovery.
SCOPE_CASES = [
    (
        {"request": "Review an owner-configured acceptance gate after bypass fixes.",
         "problem": "The owner enabled checks for two validation jobs in a project containing other independent work.",
         "proposal": "On project activation, every existing or future advancement job must have an owner binding. Changing a selected job's role no longer bypasses the gate.",
         "evidence": "Selected-job, selected-job recovery and feature-off tests pass. A newly created unrelated job is rejected as unbound even though its ordinary validator passes. No owner instruction authorizes a project-wide contract. Prior review approved the bypass repair."},
        "REQUEST_CHANGES", "architecture",
    ),
    (
        {"request": "Review an owner-configured acceptance gate after bypass fixes.",
         "problem": "The owner enabled checks for two validation jobs in a project containing other independent work.",
         "proposal": "The shared gate distinguishes explicit selected-job coverage from binding readiness. Covered unbound jobs stay held even after editable role changes. Unselected jobs retain ordinary admission.",
         "evidence": "Real CLI tests cover selected missing binding, existing independent work, a new independent job after activation, role-change escape, and owner repair followed by resumed selected work. Feature-off and ordinary validation remain unchanged; other required evidence is verified."},
        "APPROVE", "none",
    ),
    (
        {"request": "Review recovery of jobs stranded behind a policy gate.",
         "problem": "The accepted outcome is to restore useful work after an overly broad gate captured independent jobs.",
         "proposal": "When admission fails, persist a blocker receipt and mark replan complete. Require the same per-job owner binding on every retry.",
         "evidence": "The receipt write, replan completion and retry recommendation tests pass. The real job remains rejected after following those steps. No runnable owner route, scope correction or accepted prerequisite boundary is delivered. Author calls this automatic recovery."},
        "REQUEST_CHANGES", "lifecycle",
    ),
    (
        {"request": "Review a deliberately project-wide owner policy gate and its recovery.",
         "problem": "The owner explicitly requires every current and future job in the project to satisfy an approval contract.",
         "proposal": "The gate applies to all jobs, including newly created jobs. Unbound jobs fail closed with a repair command owned by the authorized operator.",
         "evidence": "Owner intent explicitly covers future work. Real CLI tests prove unbound new work is held, owner correction restores execution, workers cannot change scope, and disabling the optional feature preserves baseline behavior. All other required evidence is verified. Global coverage is deliberate, not inferred from activation."},
        "APPROVE", "none",
    ),
]
CASES.extend(SCOPE_CASES)

# A successful local feature must not strand later work or burden ordinary use.
CASES.extend([
    (
        {"request": "Review an automatic status refresh feature for a long-running agent.",
         "problem": "Users need accurate status while independent accepted work continues over many turns.",
         "proposal": "Each refresh creates a new highest-priority planning obligation before ordinary work. Every individual refresh and planning call succeeds and persists a receipt.",
         "evidence": "Real sequential CLI calls show that closing one obligation triggers another on the next refresh without new input. Independent work is never selected. The UI reports successful refresh, local feature acceptance and CI pass. No owner policy asks for repeated replanning."},
        "REQUEST_CHANGES", "architecture",
    ),
    (
        {"request": "Review an automatic status refresh feature for a long-running agent.",
         "problem": "Users need accurate status while independent accepted work continues over many turns.",
         "proposal": "Refresh derives obligations from a stable source checkpoint. A satisfied checkpoint survives restart and does not create another obligation without a material change.",
         "evidence": "Real CLI sequences cover refresh, repair, next ordinary task, restart, unchanged refresh and a new material change. Work advances; the new change alone reopens review. The UI readback matches durable progress and preserves cancel/recovery. Other applicable evidence is verified."},
        "APPROVE", "none",
    ),
    (
        {"request": "Review a diagnostic setup wizard added to ordinary task resume.",
         "problem": "Diagnostics are optional; existing users can resume authorized work without setup.",
         "proposal": "Every resume now requires the user to acknowledge five diagnostic screens. All screens work, explain themselves, and their acknowledgements persist; none grants authority or supplies a missing prerequisite.",
         "evidence": "The packaged user journey demonstrates five new interventions on every resume, including after restart. Existing diagnostics-off users cannot skip them. Backend resume and wizard tests pass. No accepted product requirement justifies the repeated interruption."},
        "REQUEST_CHANGES", "architecture",
    ),
    (
        {"request": "Review a confirmation step before a destructive external action.",
         "problem": "The accepted product contract requires one explicit scoped confirmation for this effect; routine work must remain usable.",
         "proposal": "The existing surface explains the effect, offers confirm or cancel, and durably binds one confirmation to that action. Other work and optional diagnostics remain independent.",
         "evidence": "Packaged interaction and CLI readback prove the same pending action, one confirmation, once-only execution, safe cancel, restart recovery and uninterrupted routine resume. Added friction matches the accepted safety contract. Other applicable evidence is verified."},
        "APPROVE", "none",
    ),
])


def test_decision_procedure_is_in_the_real_packet_before_prose():
    response = build_agent_response_contract()
    assert response["review_execution_contract"]["decision_procedure"]["order"] == [
        "establish_goal",
        "challenge_design",
        "falsify_claims",
        "inspect_implementation",
        "reconcile_verdict",
    ]
    assert "decision_procedure" in response["instructions"][1]


def test_corpus_has_positive_controls_and_does_not_send_its_oracle():
    assert {verdict for _, verdict, _ in CASES} == {"APPROVE", "REQUEST_CHANGES"}
    assert sum(verdict == "APPROVE" for _, verdict, _ in CASES) == len(CASES) // 2
    for scenario, _, _ in [*CASES, *HISTORICAL_CASES]:
        assert (
            not {"expected", "expected_verdict", "concern", "case_id", "review_url", "review_file",
                 "decisive_location"} & scenario.keys()
        )
