import assert from "node:assert/strict";
import test from "node:test";

import {
  MONITOR_SCHEDULE_REQUEST_SCHEMA,
  MONITOR_SCHEDULE_RESULT_SCHEMA,
  projectMonitorSchedule,
} from "../../loopx/control_plane/scheduler/monitor_schedule.ts";

test("monitor schedule materializes cadence and preserves explicit due time", () => {
  const cadence = projectMonitorSchedule({
    schema_version: MONITOR_SCHEDULE_REQUEST_SCHEMA,
    generated_at: "2026-08-25T10:00:00+08:00",
    cadence: "30 minutes",
    explicit_next_due_at: null,
  });
  assert.deepEqual(cadence, {
    schema_version: MONITOR_SCHEDULE_RESULT_SCHEMA,
    next_due_at: "2026-08-25T02:30:00Z",
    schedule_source: "cadence",
    cadence_seconds: 1800,
  });

  const explicit = projectMonitorSchedule({
    schema_version: MONITOR_SCHEDULE_REQUEST_SCHEMA,
    generated_at: "ignored-for-explicit-schedule",
    cadence: "1h",
    explicit_next_due_at: "2026-08-26T09:00:00+08:00",
  });
  assert.equal(explicit.next_due_at, "2026-08-26T09:00:00+08:00");
  assert.equal(explicit.schedule_source, "explicit");
  assert.equal(explicit.cadence_seconds, 3600);

  const missing = projectMonitorSchedule({
    schema_version: MONITOR_SCHEDULE_REQUEST_SCHEMA,
    generated_at: "2026-08-25T10:00:00+08:00",
    cadence: "not-a-cadence",
  });
  assert.equal(missing.next_due_at, null);
  assert.equal(missing.schedule_source, "none");
});

test("monitor schedule rejects malformed schema and timestamps", () => {
  assert.throws(
    () => projectMonitorSchedule({
      schema_version: "unsupported",
      generated_at: "2026-08-25T10:00:00Z",
      cadence: "30m",
    }),
    /request schema mismatch/,
  );
  assert.throws(
    () => projectMonitorSchedule({
      schema_version: MONITOR_SCHEDULE_REQUEST_SCHEMA,
      generated_at: "not-a-timestamp",
      cadence: "30m",
    }),
    /generated_at must be an ISO timestamp/,
  );
  assert.throws(
    () => projectMonitorSchedule({
      schema_version: MONITOR_SCHEDULE_REQUEST_SCHEMA,
      generated_at: "2026-08-25T10:00:00Z",
      explicit_next_due_at: "not-a-timestamp",
    }),
    /explicit_next_due_at must be an ISO timestamp/,
  );
});
