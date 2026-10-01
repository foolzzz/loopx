import type { JsonObject } from "../effect_program.ts";
import { EffectRuntimeRequestError } from "../effect_runtime_errors.ts";
import {
  requireJsonObject,
  requireNonEmptyString,
} from "../runtime_decode.ts";
import { parseTodoTimestampMicros } from "../runtime_timestamp.ts";

export const MONITOR_SCHEDULE_REQUEST_SCHEMA =
  "loopx_monitor_schedule_request_v0";
export const MONITOR_SCHEDULE_RESULT_SCHEMA =
  "loopx_monitor_schedule_result_v0";

export interface MonitorScheduleResult extends JsonObject {
  schema_version: typeof MONITOR_SCHEDULE_RESULT_SCHEMA;
  next_due_at: string | null;
  schedule_source: "explicit" | "cadence" | "none";
  cadence_seconds: number | null;
}

const MONITOR_CADENCE_PATTERN =
  /^\s*(?<count>[1-9][0-9]{0,4})\s*(?<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\s*$/i;

function optionalRequestString(value: unknown, label: string): string {
  if (value === undefined || value === null) return "";
  if (typeof value !== "string") {
    throw new EffectRuntimeRequestError(`${label} must be a string when present`);
  }
  return value.trim();
}

function monitorCadenceSeconds(value: unknown): number | null {
  const cadence = optionalRequestString(value, "monitor cadence");
  if (!cadence) return null;
  const match = MONITOR_CADENCE_PATTERN.exec(cadence);
  if (!match?.groups) return null;
  const count = Number.parseInt(match.groups.count, 10);
  const unit = match.groups.unit.toLowerCase();
  const multiplier = unit.startsWith("s")
    ? 1
    : unit.startsWith("m")
    ? 60
    : unit.startsWith("h")
    ? 60 * 60
    : 24 * 60 * 60;
  return count * multiplier;
}

function monitorScheduleTimestamp(milliseconds: number): string {
  return new Date(milliseconds).toISOString().replace(".000Z", "Z");
}

export function projectMonitorSchedule(value: unknown): MonitorScheduleResult {
  const request = requireJsonObject(value, "monitor.schedule params");
  if (request.schema_version !== MONITOR_SCHEDULE_REQUEST_SCHEMA) {
    throw new EffectRuntimeRequestError("Monitor schedule request schema mismatch");
  }
  const explicitNextDueAt = optionalRequestString(
    request.explicit_next_due_at,
    "explicit_next_due_at",
  );
  if (explicitNextDueAt) {
    if (parseTodoTimestampMicros(explicitNextDueAt) === null) {
      throw new EffectRuntimeRequestError(
        "explicit_next_due_at must be an ISO timestamp",
      );
    }
    return {
      schema_version: MONITOR_SCHEDULE_RESULT_SCHEMA,
      next_due_at: explicitNextDueAt,
      schedule_source: "explicit",
      cadence_seconds: monitorCadenceSeconds(request.cadence),
    };
  }

  const cadenceSeconds = monitorCadenceSeconds(request.cadence);
  if (cadenceSeconds === null) {
    return {
      schema_version: MONITOR_SCHEDULE_RESULT_SCHEMA,
      next_due_at: null,
      schedule_source: "none",
      cadence_seconds: null,
    };
  }
  const generatedAt = requireNonEmptyString(request.generated_at, "generated_at");
  const generatedAtMicros = parseTodoTimestampMicros(generatedAt);
  if (generatedAtMicros === null) {
    throw new EffectRuntimeRequestError("generated_at must be an ISO timestamp");
  }
  const dueMicros = generatedAtMicros + BigInt(cadenceSeconds) * 1_000_000n;
  if (dueMicros < -62135596800000000n || dueMicros > 253402300799999999n) {
    throw new EffectRuntimeRequestError("cadence produces a due time outside the ISO timestamp range");
  }
  // Drop sub-millisecond digits within the second, including before the epoch.
  const dueMilliseconds = (dueMicros - (dueMicros < 0n ? 999n : 0n)) / 1_000n;
  return {
    schema_version: MONITOR_SCHEDULE_RESULT_SCHEMA,
    next_due_at: monitorScheduleTimestamp(
      Number(dueMilliseconds),
    ),
    schedule_source: "cadence",
    cadence_seconds: cadenceSeconds,
  };
}
