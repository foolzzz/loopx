import assert from "node:assert/strict";
import test from "node:test";
import {runtimeShadowHeadDigest, runtimeShadowPartitionDigest,
  readRuntimeShadowCandidate} from "../../loopx/control_plane/coordination/runtime_shadow_candidate.ts";

test("runtime shadow parity ignores only the resume evaluation observation clock", () => {
  const head = {
    handoff_mode: "hard_lease",
    todos: [{
      todo_id: "todo-a",
      resume_ready: false,
      resume_condition: {
        evaluated_at: "2026-09-20T00:00:00Z",
        satisfied: false,
        availability_reason: "resume_condition_pending",
      },
    }],
    leases: [],
  };
  const laterObservation = structuredClone(head);
  laterObservation.todos[0]!.resume_condition.evaluated_at = "2026-09-21T00:00:00Z";

  assert.equal(
    runtimeShadowHeadDigest(head),
    runtimeShadowHeadDigest(laterObservation),
  );
  assert.equal(
    runtimeShadowPartitionDigest("todos", {
      handoff_mode: head.handoff_mode,
      todos: head.todos,
    }),
    runtimeShadowPartitionDigest("todos", {
      handoff_mode: laterObservation.handoff_mode,
      todos: laterObservation.todos,
    }),
  );

  const changedDecision = structuredClone(laterObservation);
  changedDecision.todos[0]!.resume_condition.satisfied = true;
  assert.notEqual(
    runtimeShadowHeadDigest(head),
    runtimeShadowHeadDigest(changedDecision),
  );
  assert.notEqual(
    runtimeShadowPartitionDigest("todos", {
      handoff_mode: head.handoff_mode,
      todos: head.todos,
    }),
    runtimeShadowPartitionDigest("todos", {
      handoff_mode: changedDecision.handoff_mode,
      todos: changedDecision.todos,
    }),
  );
});


test("runtime candidate refuses the removed observation store", async () => {
  await assert.rejects(readRuntimeShadowCandidate({
    schema_version: "loopx_coordination_runtime_shadow_outbox_read_v0",
    runtime_root: "/unused", goal_id: "goal-a", store_kind: "legacy_observation",
  }), /store_kind must be runtime_shadow/);
});
