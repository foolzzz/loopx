/** Fork slice S2: in_review status, review routing and role-aware gates. */
import assert from "node:assert/strict";
import test from "node:test";
import type { JsonObject } from "../../loopx/control_plane/effect_program.ts";
import { projectQuotaSelection } from "../../loopx/control_plane/todos/quota_selection.ts";
import { normalizeTodoRoleContract, planTodoFieldUpdate, TODO_FIELD_UPDATE_REQUEST_SCHEMA } from "../../loopx/control_plane/todos/field_update.ts";
import { planStateEventReplay } from "../../loopx/control_plane/goals/state_event_replay.ts";
import { evaluateTodoCompletionFence, TODO_COMPLETION_FENCE_REQUEST_SCHEMA } from "../../loopx/control_plane/todos/completion_fence.ts";

function row(id: string, fields: JsonObject = {}): JsonObject {
  return {payload: {todo_id: id}, claim: null, bound: null, blocks: null, excluded: [],
    global: false, gate: false, removed: false, actionable: true, due: false, watch_only: false,
    task_class: "advancement_task", priority: 1, index: 1, profile_rank: 1,
    missing: [], raw_claimed: false, ...fields};
}
function request(items: JsonObject[], fields: JsonObject = {}): JsonObject {
  return {items, active_items: items, active_executable_items: [], agent_id: "agent-a",
    user_gate_scope: false, monitor_supported: true, diagnostic_limit: 3,
    backlog_limit: 8, visibility_limit: 16, profile: null, source_open_count: items.length,
    agent_model: "role_v1", ...fields};
}
const ids = (value: unknown) => (value as JsonObject[]).map(item => item.todo_id);
const review = (id: string, reviewer: string | null) => row(id, {actionable: false, claim: "dev-1",
  raw_claimed: true, in_review: true, review_agent: reviewer});

test("in_review todos are executable only by their resolved acceptor", () => {
  const items = [row("impl"), review("delivered", "agent-a"), review("other-acceptor", "agent-b"),
    review("unresolved", null)];
  const lanes = (agent: string, role: string) => projectQuotaSelection(
    request(items, {agent_id: agent, agent_role: role})).lanes as JsonObject;
  const acceptor = lanes("agent-a", "acceptor");
  assert.deepEqual(ids(acceptor.executable_items), ["delivered"]);
  assert.equal((acceptor.role_scope as JsonObject).review_open_count, 1);
  // The developer who delivered never receives the review, even though it holds the claim.
  const developer = lanes("dev-1", "developer");
  assert.deepEqual(ids(developer.executable_items), ["impl"]);
  assert.deepEqual(ids(developer.open_items), ["impl"]);
  // Without a single acceptor the review goes back to the orchestrator.
  assert.deepEqual(ids(lanes("orch", "orchestrator").executable_items), ["unresolved"]);
  // Peer (unroled) lanes see in_review work but cannot execute it.
  const peer = projectQuotaSelection(request(items, {agent_id: "dev-1", agent_model: "peer_v1"})).lanes as JsonObject;
  assert.deepEqual(ids(peer.executable_items), ["impl"]);
});

test("an unscoped user gate blocks developers but not the acceptor; a global gate blocks all", () => {
  const gates = [row("unscoped", {gate: true}), row("dev-scoped", {gate: true, blocks: "dev-1"})];
  const open = (agent: string, role: string, items = gates) => ids((projectQuotaSelection(
    request(items, {agent_id: agent, agent_role: role, user_gate_scope: true})).lanes as JsonObject).open_items);
  assert.deepEqual(open("dev-1", "developer"), ["unscoped", "dev-scoped"]);
  assert.deepEqual(open("acc-1", "acceptor"), []);
  assert.deepEqual(open("acc-1", "acceptor", [row("global", {gate: true, global: true}),
    row("to-acceptor", {gate: true, blocks: "acc-1"})]), ["global", "to-acceptor"]);
  // peer_v1 keeps the previous rule.
  assert.deepEqual(ids((projectQuotaSelection(request(gates, {agent_id: "acc-1", agent_role: "acceptor",
    agent_model: "peer_v1", user_gate_scope: true})).lanes as JsonObject).open_items), ["unscoped"]);
});

test("a gate awaiting the orchestrator's reply does not block the orchestrator lane", () => {
  const gates = [row("clarify", {gate: true, blocks: "orch", claim: null, awaits_orchestrator: true}),
    row("plan-approval", {gate: true, blocks: "orch"}), row("unscoped", {gate: true, awaits_orchestrator: true})];
  const open = (agent: string, role: string, model = "role_v1") => ids((projectQuotaSelection(
    request(gates, {agent_id: agent, agent_role: role, agent_model: model, user_gate_scope: true})).lanes as JsonObject).open_items);
  // The awaited gate stops blocking the orchestrator; the one awaiting the user still blocks it.
  assert.deepEqual(open("orch", "orchestrator"), ["plan-approval"]);
  // Other roles and peer_v1 keep the previous rules.
  assert.deepEqual(open("dev-1", "developer"), ["unscoped"]);
  assert.deepEqual(open("orch", "orchestrator", "peer_v1"), ["clarify", "plan-approval", "unscoped"]);
});

test("field planner accepts in_review and the delivery/verdict fields", () => {
  const plan = planTodoFieldUpdate({schema_version: TODO_FIELD_UPDATE_REQUEST_SCHEMA,
    todo: {todo_id: "todo_review1", status: "open"},
    intent: {status: "in_review", role_contract: {delivered_by: "dev-1", review_feedback: "  needs\n tests "}},
    updated_at: "2026-09-26T00:00:00Z"});
  assert.equal(plan.target_status, "in_review");
  assert.equal(plan.metadata_updates.delivered_by, "dev-1");
  assert.equal(plan.metadata_updates.review_feedback, "needs tests");
  assert.equal(plan.metadata_updates.completed_at, null);
  assert.throws(() => normalizeTodoRoleContract({review_feedback: 3}));
  assert.equal((normalizeTodoRoleContract({review_feedback: "x".repeat(900)}).review_feedback as string).length, 600);
});

test("field planner writes and clears the orchestrator-owned acceptance criteria (G2)", () => {
  const plan = planTodoFieldUpdate({schema_version: TODO_FIELD_UPDATE_REQUEST_SCHEMA,
    todo: {todo_id: "todo_criteria1", status: "open"},
    intent: {role_contract: {acceptance_criteria: "  GET /orders\n returns 200 "}},
    updated_at: "2026-09-26T00:00:00Z"});
  assert.equal(plan.metadata_updates.acceptance_criteria, "GET /orders returns 200");
  assert.equal(normalizeTodoRoleContract({acceptance_criteria: null}).acceptance_criteria, null);
  assert.equal(normalizeTodoRoleContract({acceptance_criteria: "  "}).acceptance_criteria, null);
  assert.throws(() => normalizeTodoRoleContract({acceptance_criteria: ["a"]}), /acceptance_criteria must be a string/);
  const long = normalizeTodoRoleContract({acceptance_criteria: "x".repeat(1500)}).acceptance_criteria as string;
  assert.equal(long.length, 1000);
  assert.equal(long.endsWith("..."), true);
});

test("event replay folds in_review and reopen transitions", () => {
  const event = (kind: string, n: number): JsonObject => ({event_id: `event-${n}`, goal_id: "sample",
    event_type: kind, append_sequence: n, recorded_at: "2026-09-26T00:00:00Z", todo_id: "todo_alpha",
    role: null, priority: null, planner_order: null, content_changed: false, fields: ["title"],
    capability_binding_ref: null, continuation_policy: null, removed_continuation_policy: null,
    has_exclusions: false, goal_bound: null});
  const replay = (kinds: string[]) => (planStateEventReplay({schema_version: "state_event_replay_request_v0",
    goal_id: null, events: kinds.map((kind, index) => event(kind, index + 1))}).todos as JsonObject[])[0];
  assert.equal(replay(["todo_added", "todo_in_review"]).status, "in_review");
  assert.equal(replay(["todo_added", "todo_in_review"]).done, false);
  assert.equal(replay(["todo_added", "todo_in_review", "todo_reopened"]).status, "open");
  assert.equal(replay(["todo_added", "todo_in_review", "todo_completed"]).done, true);
});

test("the completion fence treats in_review as non-terminal", () => {
  const result = evaluateTodoCompletionFence({schema_version: TODO_COMPLETION_FENCE_REQUEST_SCHEMA,
    projection_source: "materialized", todo: {status: "in_review", completion_turn_key: null,
      completion_continuation: null, no_followup: null, successor_todo_ids: []},
    requested_completion_turn_key: null, requested_no_followup: false,
    requested_completion_identity_source: null, goal_id: null, todo_id: null});
  assert.equal(result.outcome, "continue");
  assert.equal(result.terminal_before_request, false);
});
