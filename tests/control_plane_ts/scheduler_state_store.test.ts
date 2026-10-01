import assert from "node:assert/strict";
import { join } from "node:path";
import test from "node:test";

import {
  evaluateSchedulerStateOperation,
  schedulerStatePath,
  SCHEDULER_STATE_OPERATION_REQUEST_SCHEMA,
} from "../../loopx/control_plane/scheduler/state_store.ts";

const scope = {
  goalId: "goal-a",
  agentId: "owner-policy",
  surface: "quota",
  stateKey: "automation-cadence-v1",
};

function characterize(operation: unknown, params: Record<string, unknown>): unknown {
  return evaluateSchedulerStateOperation({
    schema_version: SCHEDULER_STATE_OPERATION_REQUEST_SCHEMA,
    operation,
    ...params,
  }).value;
}

test("current scheduler operations preserve their typed contract", () => {
  const cases = [
    {
      name: "rrule interval clamps to one minute",
      operation: "rrule_for_minutes",
      params: { value: -3 },
      expected: "FREQ=MINUTELY;INTERVAL=1",
    },
    {
      name: "rrule prefix and whitespace normalize",
      operation: "normalize_rrule",
      params: { value: "  RRULE:  FREQ=MINUTELY;  INTERVAL=15  " },
      expected: "FREQ=MINUTELY; INTERVAL=15",
    },
  ];
  for (const item of cases) {
    assert.deepEqual(
      characterize(item.operation, item.params),
      item.expected,
      item.name,
    );
  }
});

test("typed boundary rejects malformed requests", () => {
  assert.throws(
    () => evaluateSchedulerStateOperation({
      schema_version: "unsupported",
      operation: "normalize_rrule",
      value: "RRULE:FREQ=MINUTELY;INTERVAL=3",
    }),
    /request schema mismatch/,
  );
  assert.throws(
    () => evaluateSchedulerStateOperation({
      schema_version: SCHEDULER_STATE_OPERATION_REQUEST_SCHEMA,
      operation: "delete_state",
    }),
    /Scheduler state operation is unsupported/,
  );
});

test("pure scheduler state operations do not mutate caller input", () => {
  const params = { value: "  RRULE:  FREQ=MINUTELY;  INTERVAL=15  " };
  const before = structuredClone(params);
  characterize("normalize_rrule", params);
  assert.deepEqual(params, before);
});

test("state path is scoped, sanitized, and stable", () => {
  const path = schedulerStatePath("/runtime", {
    ...scope,
    goalId: "../goal with spaces",
    agentId: "agent/../../other",
  });
  assert.equal(
    path,
    join(
      "/runtime",
      "goals",
      "goal-with-spaces-116e9296329bcdc8",
      "scheduler-state",
      "agent-..-..-other-55571d8866830ec6",
      "quota-b878a6801d9a9e68",
      "4163ef554dadb952.json",
    ),
  );
});

test("sanitized-equivalent scheduler scopes have distinct bounded paths", () => {
  const goalPaths = ["a b", "a-b", "非 ASCII", "---"].map((goalId) =>
    schedulerStatePath("/runtime", { ...scope, goalId })
  );
  const agentPaths = ["agent/name", "agent-name"].map((agentId) =>
    schedulerStatePath("/runtime", { ...scope, agentId })
  );
  const surfacePaths = ["codex app", "codex-app"].map((surface) =>
    schedulerStatePath("/runtime", { ...scope, surface })
  );
  assert.equal(new Set(goalPaths).size, goalPaths.length);
  assert.equal(new Set(agentPaths).size, agentPaths.length);
  assert.equal(new Set(surfacePaths).size, surfacePaths.length);
  for (const path of [...goalPaths, ...agentPaths, ...surfacePaths]) {
    for (const segment of path.split("/")) assert.ok(segment.length <= 64);
  }
});
