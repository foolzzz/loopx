/**
 * One owner for the goal rollout-event log location and its receipt reads.
 *
 * The log is a goal-level runtime artifact that more than one transaction
 * reads: the settlement readback, the receipt-bound scheduler follow-up, and
 * prior-host-Turn closeout resolution.  Resolving the path and reading its
 * receipts lives here so a reader cannot invent a second path rule or a
 * different tolerance for malformed lines.
 */
import { relative, resolve, sep } from "node:path";

import type { JsonObject } from "./effect_program.ts";
import { EffectRuntimeRequestError } from "./effect_runtime_errors.ts";
import { requireNonEmptyString } from "./runtime_decode.ts";
import { readReceiptLogSnapshot } from "./runtime/receipt_log_snapshot.ts";

export const ROLLOUT_EVENT_SCHEMA_VERSION = "loopx_rollout_event_v0";
export const HEARTBEAT_RECEIPT_EVENT_KIND = "quota_should_run";

/**
 * One parse of a Goal's rollout-event log.
 *
 * Receipt discovery is intentionally tolerant: an unrelated malformed line
 * cannot erase a valid heartbeat receipt. Settlement readback is intentionally
 * strict: once a receipt creates a closeout obligation, malformed persisted
 * state must fail closed. Keeping the first strict error beside the valid
 * records lets both readers share one physical read without weakening either
 * contract.
 */
export interface GoalRolloutEventSnapshot {
  readonly runtimeRoot: string;
  readonly goalId: string;
  readonly events: readonly JsonObject[];
  readonly firstStrictErrorLine: number | null;
}

/** Reject a goal id that is not one path segment, before any log read. */
export function goalPathSegment(value: unknown): string {
  const label = "goal_id";
  const result = requireNonEmptyString(value, label).trim();
  if (
    result === "." ||
    result === ".." ||
    result.includes("/") ||
    result.includes("\\")
  ) {
    throw new EffectRuntimeRequestError(
      `${label} must be a single path segment`,
      "invalid_goal_id",
    );
  }
  return result;
}

/** Resolve one goal's rollout-event log inside `runtime_root`. */
export function goalRolloutEventLogPath(
  runtimeRoot: string,
  goalId: string,
): string {
  const root = resolve(runtimeRoot);
  const path = resolve(
    root,
    "goals",
    goalPathSegment(goalId),
    "rollout-event-log.jsonl",
  );
  const child = relative(root, path);
  if (child === "" || child === ".." || child.startsWith(`..${sep}`)) {
    throw new EffectRuntimeRequestError(
      "rollout event log path escapes runtime_root",
      "invalid_rollout_event_log_path",
    );
  }
  return path;
}

/** Read and parse the rollout log once, retaining strict-read diagnostics. */
export async function readGoalRolloutEventSnapshot(
  runtimeRoot: string,
  goalId: string,
): Promise<GoalRolloutEventSnapshot | null> {
  const snapshot = await readReceiptLogSnapshot(
    goalRolloutEventLogPath(runtimeRoot, goalId), ROLLOUT_EVENT_SCHEMA_VERSION,
  );
  return snapshot === null ? null : {
    runtimeRoot, goalId, events: snapshot.records,
    firstStrictErrorLine: snapshot.firstErrorLine,
  };
}

/** Return strict settlement input, or fail on the first malformed line. */
export function strictGoalRolloutEvents(
  snapshot: GoalRolloutEventSnapshot | null,
): readonly JsonObject[] {
  if (snapshot !== null && snapshot.firstStrictErrorLine !== null) {
    throw new EffectRuntimeRequestError(
      `settlement readback line ${snapshot.firstStrictErrorLine} is malformed`,
      "malformed_settlement_state",
    );
  }
  return snapshot?.events ?? [];
}

/** Filter heartbeat receipts from an already parsed Goal log snapshot. */
export function goalHeartbeatReceiptsFromSnapshot(
  snapshot: GoalRolloutEventSnapshot | null,
  goalId: string,
  agentId?: string | null,
): JsonObject[] | null {
  if (snapshot === null) return null;
  if (snapshot.goalId !== goalId) {
    throw new EffectRuntimeRequestError(
      "rollout event snapshot does not belong to the requested goal",
      "rollout_event_snapshot_scope_mismatch",
    );
  }
  return snapshot.events.filter((event) =>
    event.event_kind === HEARTBEAT_RECEIPT_EVENT_KIND &&
    event.goal_id === goalId &&
    (agentId === undefined || event.agent_id === agentId)
  );
}
