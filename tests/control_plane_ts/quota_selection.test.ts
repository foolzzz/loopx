import assert from "node:assert/strict";
import test from "node:test";
import type { JsonObject } from "../../loopx/control_plane/effect_program.ts";
import { projectQuotaSelection } from "../../loopx/control_plane/todos/quota_selection.ts";
import { productionScaleCoordinationFixture } from "./production_scale_coordination_fixture.ts";

function row(id: string, fields: JsonObject = {}): JsonObject {
  return {payload: {todo_id: id}, claim: null, bound: null, blocks: null, excluded: [],
    global: false, gate: false, removed: false, actionable: true, due: false,
    watch_only: false,
    task_class: "advancement_task", priority: 1, index: 1, profile_rank: 1,
    missing: [], raw_claimed: false, ...fields};
}
function request(items: JsonObject[], fields: JsonObject = {}): JsonObject {
  return {items, active_items: items, active_executable_items: [], agent_id: "agent-a",
    user_gate_scope: false, monitor_supported: true, diagnostic_limit: 3,
    backlog_limit: 8, visibility_limit: 16, profile: null, source_open_count: items.length, ...fields};
}
const ids = (value: unknown) => (value as JsonObject[]).map(item => item.todo_id);

test("gate applicability overrides execution claims, but not another lane's explicit scope", () => {
  const rows = [row("global", {gate: true, global: true, claim: "agent-b", excluded: ["agent-a"]}),
    row("targeted", {gate: true, blocks: "agent-a", claim: "agent-b"}),
    row("other", {gate: true, blocks: "agent-b", claim: "agent-a"}),
    row("legacy", {gate: true, claim: "agent-b"}),
    row("action", {bound: "agent-a", claim: "agent-b"}),
    row("other-action", {bound: "agent-b", claim: "agent-a"})];
  const input = request(rows, {user_gate_scope: true});
  const before = structuredClone(input);
  const lanes = projectQuotaSelection(input).lanes as JsonObject;
  assert.deepEqual(ids(lanes.open_items), ["global", "targeted"]);
  assert.deepEqual(ids(lanes.user_action_open_items), ["action"]);
  assert.deepEqual(ids(lanes.other_agent_scoped_items), ["other", "legacy"]);
  assert.deepEqual(ids(lanes.active_next_action_items), ["global", "targeted", "action"]);
  assert.equal(lanes.claim_scope, null);
  assert.deepEqual(input, before);
});

test("execution scope is shared with active-next-action, including removed-policy rejection", () => {
  const items = [row("excluded", {excluded: ["agent-a"]}), row("removed", {removed: true}),
    row("peer", {claim: "agent-b"}), row("unclaimed", {priority: 0, profile_rank: 0}),
    row("mine", {claim: "agent-a", priority: 4, profile_rank: 2})];
  const result = projectQuotaSelection(request(items));
  const lanes = result.lanes as JsonObject;
  assert.deepEqual(ids(lanes.open_items), ["mine", "unclaimed"]);
  assert.deepEqual(ids(lanes.active_next_action_items), ["unclaimed", "mine"]);
  assert.equal((lanes.claim_scope as JsonObject).executor_excluded_self_count, 1);
  assert.equal((lanes.claim_scope as JsonObject).removed_continuation_blocked_count, 1);
  assert.deepEqual(ids((result.claim_visibility as JsonObject).claimed_by_others_items), ["peer"]);
});

test("monitor eligibility preserves provider writeback and capability fences", () => {
  const items = [row("due", {task_class: "continuous_monitor", due: true}),
    row("watch", {task_class: "continuous_monitor", due: true, watch_only: true}),
    row("missing", {task_class: "continuous_monitor", due: true, missing: ["network"]}),
    row("future", {task_class: "continuous_monitor"})];
  const lanes = projectQuotaSelection(request(items)).lanes as JsonObject;
  assert.deepEqual(ids(lanes.monitor_due_items), ["due", "watch"]);
  assert.deepEqual(ids(lanes.watch_only_monitor_items), ["watch"]);
  assert.deepEqual(ids(lanes.watch_only_monitor_due_items), ["watch"]);
  assert.deepEqual(ids(lanes.non_watch_only_monitor_due_items), ["due"]);
  assert.deepEqual(ids(lanes.monitor_capability_blocked_due_items), ["missing"]);
  assert.deepEqual(ids(lanes.executable_items), []);
  const unsupported = projectQuotaSelection(request(items, {monitor_supported: false})).lanes as JsonObject;
  assert.deepEqual(unsupported.monitor_due_items, []);
  assert.deepEqual(unsupported.monitor_capability_blocked_due_items, []);
});

test("production-scale corpus counts remain complete while claimant display is bounded", () => {
  const fixture = productionScaleCoordinationFixture("goal-quota");
  const records = fixture.projection.todos as JsonObject[];
  const open = records.filter(item => item.role === "agent" && !item.done);
  const items = open.map((item, index) => row(String(item.todo_id), {
    payload: item, index, claim: item.claimed_by ?? null, raw_claimed: !!item.claimed_by,
    task_class: item.task_class, actionable: item.status === "open",
  }));
  const input = request(items, {visibility_limit: 2});
  const before = structuredClone(input);
  const result = projectQuotaSelection(input);
  const scope = (result.lanes as JsonObject).claim_scope as JsonObject;
  assert.equal(open.length, 80);
  assert.equal(scope.current_agent_claimed_open_count, 40);
  assert.equal(scope.other_agent_claimed_open_count, 40);
  const visible = (result.claim_visibility as JsonObject).claimed_open_items as JsonObject[];
  assert.equal(visible.length, 2);
  assert.deepEqual(new Set(visible.map(item => item.claimed_by)), new Set(["agent-a", "agent-b"]));
  assert.deepEqual(input, before);
});

test("stable ties and zero display never change selection or counts", () => {
  const result = projectQuotaSelection(request([row("zed"), row("alpha")], {
    visibility_limit: 0, backlog_limit: 0, diagnostic_limit: 0,
  }));
  assert.deepEqual(ids((result.lanes as JsonObject).open_items), ["zed", "alpha"]);
  assert.equal((result.lanes as JsonObject).open_count, 2);
  assert.deepEqual((result.claim_visibility as JsonObject).unclaimed_priority_open_items, []);
});

test("malformed facts are rejected, not coerced into scope or execution authority", () => {
  for (const fields of [{gate: "false"}, {global: "true"}, {excluded: "agent-a"}, {priority: null}]) {
    assert.throws(() => projectQuotaSelection(request([row("bad", fields)])));
  }
  assert.throws(() => projectQuotaSelection(request([], {visibility_limit: -1})));
});

test("role_v1 selection routes todos by required role", () => {
  const items = [
    row("impl"),
    row("dev-explicit", {required_role: "developer"}),
    row("review", {required_role: "acceptor"}),
    row("plan-explicit", {required_role: "orchestrator"}),
    row("replan", {planning: true}),
    row("blocker", {task_class: "blocker", actionable: false}),
    row("gate", {gate: true}),
  ];
  const lanesFor = (role: string | null, model = "role_v1") =>
    projectQuotaSelection(request(items, {agent_model: model, agent_role: role})).lanes as JsonObject;
  const developer = lanesFor("developer");
  assert.deepEqual(ids(developer.open_items), ["impl", "dev-explicit"]);
  assert.deepEqual(ids(developer.executable_items), ["impl", "dev-explicit"]);
  const scope = developer.role_scope as JsonObject;
  assert.equal(scope.agent_role, "developer");
  assert.equal(scope.role_filtered_open_count, 5);
  assert.equal((developer.claim_scope as JsonObject).agent_model, "role_v1");
  // The orchestrator never receives developer implementation work.
  assert.deepEqual(ids(lanesFor("orchestrator").open_items),
    ["plan-explicit", "replan", "blocker", "gate"]);
  assert.deepEqual(ids(lanesFor("acceptor").open_items), ["review"]);
  // Unroled agents and peer_v1 goals keep the flat peer behaviour.
  assert.equal(ids(lanesFor(null).open_items).length, items.length);
  assert.equal(ids(lanesFor("developer", "peer_v1").open_items).length, items.length);
  assert.equal(lanesFor(null).role_scope, undefined);
});

test("role_v1 filtering also scopes active next actions and rejects unknown roles", () => {
  const items = [row("impl"), row("review", {required_role: "acceptor"})];
  const lanes = projectQuotaSelection(request(items, {agent_model: "role_v1", agent_role: "acceptor"})).lanes as JsonObject;
  assert.deepEqual(ids(lanes.active_next_action_items), ["review"]);
  assert.throws(() => projectQuotaSelection(request(items, {agent_model: "role_v1", agent_role: "boss"})));
  assert.throws(() => projectQuotaSelection(request([row("bad", {required_role: "boss"})])));
});
