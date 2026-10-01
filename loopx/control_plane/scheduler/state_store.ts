import { createHash } from "node:crypto";
import { homedir } from "node:os";
import { join } from "node:path";

import type { JsonObject } from "../effect_program.ts";
import { EffectRuntimeRequestError } from "../effect_runtime_errors.ts";
import {
  assertNever,
  requireJsonObject as requiredObject,
  requireStringLiteral,
} from "../runtime_decode.ts";

export const SCHEDULER_STATE_OPERATION_REQUEST_SCHEMA =
  "loopx_scheduler_state_operation_request_v0";
export const SCHEDULER_STATE_OPERATION_RESULT_SCHEMA =
  "loopx_scheduler_state_operation_result_v0";

const SCOPE_SEGMENT_LABEL_LIMIT = 47;
const SCOPE_SEGMENT_HASH_LENGTH = 16;

export const SCHEDULER_STATE_OPERATIONS = [
  "rrule_for_minutes",
  "normalize_rrule",
] as const;
export type SchedulerStateOperation =
  (typeof SCHEDULER_STATE_OPERATIONS)[number];

export interface SchedulerScope {
  goalId: string;
  agentId: string;
  surface: string;
  stateKey: string;
}

export interface SchedulerStateOperationResult {
  schema_version: typeof SCHEDULER_STATE_OPERATION_RESULT_SCHEMA;
  operation: SchedulerStateOperation;
  value: unknown;
}

function pythonTruthy(value: unknown): boolean {
  if (value === null || value === undefined || value === false) return false;
  if (typeof value === "number") return value !== 0 && !Number.isNaN(value);
  if (typeof value === "string" || Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}

function pythonString(value: unknown): string {
  if (value === null || value === undefined) return "None";
  if (value === true) return "True";
  if (value === false) return "False";
  if (typeof value === "string") return value;
  if (typeof value === "number") return String(value);
  return String(value);
}

function stringOrEmpty(value: unknown): string {
  return pythonTruthy(value) ? pythonString(value) : "";
}

function trimmed(value: unknown): string {
  return stringOrEmpty(value).trim();
}

function pythonInteger(value: unknown): number | null {
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "number") {
    return Number.isFinite(value) ? Math.trunc(value) : null;
  }
  if (typeof value === "string" && /^[+-]?\d+$/.test(value.trim())) {
    const result = Number(value.trim());
    return Number.isSafeInteger(result) ? result : null;
  }
  return null;
}

export function rruleForMinutes(value: unknown): string {
  const minutes = pythonInteger(value);
  if (minutes === null) throw new EffectRuntimeRequestError("scheduler minutes must be an integer");
  return `FREQ=MINUTELY;INTERVAL=${Math.max(1, minutes)}`;
}

export function normalizeSchedulerRrule(value: unknown): string {
  let text = stringOrEmpty(value).trim().replace(/\s+/g, " ");
  if (text.toUpperCase().startsWith("RRULE:")) text = text.slice(6).trim();
  return text;
}

export function schedulerTimestampMilliseconds(value: unknown): number | null {
  const text = trimmed(value);
  if (!text) return null;
  const timezoneAware = /(?:[zZ]|[+-]\d{2}(?::?\d{2})?)$/.test(text)
    ? text
    : `${text}Z`;
  const parsed = Date.parse(timezoneAware);
  return Number.isNaN(parsed) ? null : parsed;
}

function safeSegment(value: unknown): string {
  const safe = trimmed(value)
    .replace(/[^0-9A-Za-z_.-]+/g, "-")
    .replace(/^[-._]+|[-._]+$/g, "");
  return safe || "default";
}

function stableHash(value: string, length: number): string {
  return createHash("sha256")
    .update(value, "utf8")
    .digest("hex")
    .slice(0, length);
}

function scopedSegment(value: unknown): string {
  const raw = trimmed(value);
  const label = safeSegment(raw).slice(0, SCOPE_SEGMENT_LABEL_LIMIT);
  return `${label}-${stableHash(raw, SCOPE_SEGMENT_HASH_LENGTH)}`;
}

function expandUser(path: string): string {
  if (path === "~") return homedir();
  if (path.startsWith("~/") || path.startsWith("~\\")) {
    return join(homedir(), path.slice(2));
  }
  return path;
}

export function schedulerStatePath(
  runtimeRoot: string,
  scope: SchedulerScope,
): string {
  const stateHash = stableHash(scope.stateKey, 16);
  return join(
    expandUser(runtimeRoot),
    "goals",
    scopedSegment(scope.goalId),
    "scheduler-state",
    scopedSegment(scope.agentId),
    scopedSegment(scope.surface),
    `${stateHash}.json`,
  );
}

function operationRequest(value: unknown): JsonObject {
  const request = requiredObject(value, "scheduler.state params");
  if (request.schema_version !== SCHEDULER_STATE_OPERATION_REQUEST_SCHEMA) {
    throw new EffectRuntimeRequestError("Scheduler state operation request schema mismatch");
  }
  return request;
}

export function evaluateSchedulerStateOperation(
  value: unknown,
): SchedulerStateOperationResult {
  const request = operationRequest(value);
  const operation = requireStringLiteral(
    request.operation,
    SCHEDULER_STATE_OPERATIONS,
    "scheduler.state operation",
    "Scheduler state operation is unsupported",
  );
  let result: unknown;
  switch (operation) {
    case "rrule_for_minutes":
      result = rruleForMinutes(request.value);
      break;
    case "normalize_rrule":
      result = normalizeSchedulerRrule(request.value);
      break;
    default:
      return assertNever(operation, "Scheduler state operation is unsupported");
  }
  return {
    schema_version: SCHEDULER_STATE_OPERATION_RESULT_SCHEMA,
    operation,
    value: result,
  };
}
