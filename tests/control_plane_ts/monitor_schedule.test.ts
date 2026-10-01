import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

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

test("monitor schedule uses the shared timestamp grammar for explicit and computed due times", () => {
  const vectors: {value: string; epoch_micros: string | null}[] = JSON.parse(readFileSync(
    new URL("../fixtures/control_plane/timestamp_codec_v0.json", import.meta.url), "utf8"));
  for (const {value, epoch_micros} of vectors) {
    const explicit = {schema_version: MONITOR_SCHEDULE_REQUEST_SCHEMA, explicit_next_due_at: value};
    const computed = {schema_version: MONITOR_SCHEDULE_REQUEST_SCHEMA, generated_at: value, cadence: "1m"};
    if (epoch_micros === null) {
      assert.throws(() => projectMonitorSchedule(explicit), /timestamp/, value);
      assert.throws(() => projectMonitorSchedule(computed), /timestamp/, value);
    } else {
      assert.equal(projectMonitorSchedule(explicit).next_due_at, value);
      // Ordinary instants exercise schedule arithmetic without overflowing the calendar.
      if (Math.abs(Number(epoch_micros)) < 1e16) {
        const expected = new Date(Number(epoch_micros) / 1000 + 60_000).toISOString().replace(".000Z", "Z");
        assert.equal(projectMonitorSchedule(computed).next_due_at, expected, value);
      }
    }
  }
});

test("cadence arithmetic retains the UTC calendar boundary and truncates only the final instant", () => {
  assert.throws(() => projectMonitorSchedule({
    schema_version: MONITOR_SCHEDULE_REQUEST_SCHEMA,
    generated_at: "9999-12-31T23:59:59.999999Z", cadence: "1s",
  }), /due time outside the ISO timestamp range/);
  assert.equal(projectMonitorSchedule({
    schema_version: MONITOR_SCHEDULE_REQUEST_SCHEMA,
    generated_at: "0001-01-01T00:00:00.000001Z", cadence: "1m",
  }).next_due_at, "0001-01-01T00:01:00Z");
  assert.equal(projectMonitorSchedule({
    schema_version: MONITOR_SCHEDULE_REQUEST_SCHEMA,
    generated_at: "9999-12-31T23:59:58.999999Z", cadence: "1s",
  }).next_due_at, "9999-12-31T23:59:59.999Z");
  assert.equal(projectMonitorSchedule({
    schema_version: MONITOR_SCHEDULE_REQUEST_SCHEMA,
    generated_at: "1969-12-31T23:58:59.999999Z", cadence: "1m",
  }).next_due_at, "1969-12-31T23:59:59.999Z");
});
