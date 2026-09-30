import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import test from "node:test";

import {
  evaluateSchedulerStateOperation,
  normalizeSchedulerHostUpdateFailure,
  normalizeSchedulerHostUpdateFailures,
  retainedSchedulerHostUpdateFailures,
  schedulerRruleIntervalMinutes,
  schedulerStatePath,
  SCHEDULER_STATE_OPERATION_REQUEST_SCHEMA,
} from "../../loopx/control_plane/scheduler/state_store.ts";

const fixture = JSON.parse(
  await readFile(
    new URL(
      "../fixtures/control_plane/scheduler_state_store_characterization_v0.json",
      import.meta.url,
    ),
    "utf8",
  ),
) as {
  schema_version: string;
  source_baseline: string;
  cases: Array<Record<string, unknown>>;
};

const scope = {
  goalId: "goal-a",
  agentId: "agent-a",
  surface: "codex_app",
  stateKey: "scheduler_hint.app_automation.stateful_backoff",
};

// Failure-cache helpers are called directly by the transition kernel; only
// the RRULE operations cross the Python effect-runtime boundary.
const helperOperations: Record<string, (params: Record<string, unknown>) => unknown> = {
  rrule_interval_minutes: (params) => schedulerRruleIntervalMinutes(params.value),
  normalize_failure: (params) => normalizeSchedulerHostUpdateFailure(params.value),
  normalize_failures: (params) =>
    normalizeSchedulerHostUpdateFailures(params.value, params.legacy_failure),
  retain_failures: (params) =>
    retainedSchedulerHostUpdateFailures(
      params.value,
      params.reference_time,
      params.observed_host_rrule,
    ),
};

function characterize(operation: unknown, params: Record<string, unknown>): unknown {
  const helper = helperOperations[String(operation)];
  if (helper) return helper(params);
  return evaluateSchedulerStateOperation({
    schema_version: SCHEDULER_STATE_OPERATION_REQUEST_SCHEMA,
    operation,
    ...params,
  }).value;
}

test("pinned Python scheduler state characterization remains exact", () => {
  assert.equal(
    fixture.schema_version,
    "loopx_scheduler_state_store_characterization_v0",
  );
  assert.equal(fixture.source_baseline, "8b255e1d1");
  assert.equal(fixture.cases.length, 7);
  for (const item of fixture.cases) {
    const result = characterize(
      item.operation,
      item.params as Record<string, unknown>,
    );
    if ("expected" in item) assert.deepEqual(result, item.expected, String(item.name));
    if ("expected_targets" in item) {
      assert.deepEqual(
        (result as Array<Record<string, unknown>>).map((entry) => entry.target_rrule),
        item.expected_targets,
        String(item.name),
      );
    }
    if ("expected_last_count" in item) {
      assert.equal(
        (result as Array<Record<string, unknown>>).at(-1)?.failure_count,
        item.expected_last_count,
        String(item.name),
      );
    }
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

test("failure retention accepts the ISO offsets persisted by Python", () => {
  const result = retainedSchedulerHostUpdateFailures(
    [{
      schema_version: "scheduler_host_update_failure_v0",
      target_rrule: "FREQ=MINUTELY;INTERVAL=3",
      observed_host_rrule: "FREQ=MINUTELY;INTERVAL=30",
      failure_kind: "timeout",
      failure_count: 1,
      failed_at: "2026-01-01T12:00:00+0000",
    }],
    "2026-01-02T12:00:00+00:00",
  );
  assert.equal(result.length, 1);
});

test("pure scheduler state operations do not mutate caller input", () => {
  const item = fixture.cases[5];
  const params = item.params as Record<string, unknown>;
  const before = structuredClone(params);
  characterize(item.operation, params);
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
      "codex_app-b32e6f37f5dad64e",
      "8a41f410c67e7c0e.json",
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
