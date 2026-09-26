import assert from "node:assert/strict";
import {test} from "node:test";
import {planStateEventReplay} from "../../loopx/control_plane/goals/state_event_replay.ts";
import type {JsonObject} from "../../loopx/control_plane/effect_program.ts";

function event(kind: string, n: number, fields: JsonObject = {}): JsonObject {
  return {event_id: `event-${n}`, goal_id: "sample", event_type: kind, append_sequence: n,
    recorded_at: "2026-09-24T00:00:00Z", todo_id: "todo_alpha", role: null, priority: null,
    planner_order: null, content_changed: false, fields: ["title"], capability_binding_ref: null,
    continuation_policy: null, removed_continuation_policy: null, has_exclusions: false,
    goal_bound: null, ...fields};
}
function plan(events: JsonObject[], goal: string | null = null): JsonObject {
  return planStateEventReplay({schema_version: "state_event_replay_request_v0", goal_id: goal, events});
}
function rows(result: JsonObject): JsonObject[] {return result.todos as JsonObject[];}

test("replay orders facts without changing caller arrays; ordinals still address original content", () => {
  const input = [event("todo_completed", 3), event("todo_added", 1), event("todo_claimed", 2)];
  const before = structuredClone(input);
  const result = plan(input);
  assert.deepEqual(input, before);
  assert.deepEqual(result.event_indices, [1, 2, 0]);
  assert.deepEqual(rows(result)[0].field_sources, {title: 0});
  assert.equal(rows(result)[0].status, "done");
  assert.equal(rows(result)[0].done, true);
});

test("different event IDs cannot overwrite an existing Todo even after completion", () => {
  assert.throws(() => plan([event("todo_added", 1), event("todo_completed", 2), event("todo_added", 3)]), /already exists/);
});

test("mixed Goal, orphan, duplicate identity and unsupported event kind reject", () => {
  assert.throws(() => plan([event("todo_added", 1)], "another"), /share one goal/);
  assert.throws(() => plan([event("todo_completed", 1)]), /unknown todo_id/);
  assert.throws(() => plan([event("todo_added", 1), event("todo_added", 1)]), /deduplicated/);
  assert.throws(() => plan([event("todo_resurrected", 1)]), /unsupported/);
  assert.throws(() => plan([event("todo_added", 1, {todo_id: null})]), /requires refs.todo_id/);
});

test("summary order preserves zero, user lanes and the actual source section", () => {
  const result = plan([event("todo_added", 1, {planner_order: 0}),
    event("todo_added", 2, {todo_id: "todo_beta", planner_order: 1}),
    event("todo_updated", 3, {todo_id: "todo_beta", role: "user", priority: "P0"})]);
  const [user, agent] = rows(result);
  assert.equal(user.todo_id, "todo_beta");
  assert.equal(user.source_section, "User Todo / Owner Review Reading Queue");
  assert.equal(user.render_priority, true);
  assert.equal(agent.planner_order, 0);
  assert.equal(agent.render_priority, false);
});

test("legacy lexical tie ordering follows Unicode scalar order, not UTF-16 order", () => {
  const input = [event("refresh_recorded", 1, {append_sequence: null, event_id: "\u{10000}"}),
    event("run_recorded", 2, {append_sequence: null, event_id: "\ue000"})];
  assert.deepEqual(plan(input).event_indices, [1, 0]);
});

test("malformed facts and unsafe integers cannot silently change replay ordering", () => {
  for (const field of ["append_sequence", "planner_order"]) {
    for (const value of [true, 1.5, Number.MAX_SAFE_INTEGER + 1]) {
      assert.throws(() => plan([event("todo_added", 1, {[field]: value})]), /safe integer/);
    }
  }
  assert.throws(() => plan([event("todo_added", 1, {role: "superuser"})]), /unsupported/);
  assert.throws(() => plan([event("todo_updated", 1, {priority: "P99"})]), /priority/);
});

test("binding cannot change; owner addressing is exclusive and actor attribution can clear", () => {
  const added = event("todo_added", 1, {capability_binding_ref: "domain:alpha",
    fields: ["capability_binding_ref", "goal_bound", "last_actor_agent_id"]});
  assert.throws(() => plan([added, event("todo_updated", 2, {capability_binding_ref: "domain:beta"})]), /immutable/);
  const result = rows(plan([added, event("todo_updated", 2, {fields: ["bound_agent"]})]))[0];
  assert.deepEqual(result.field_sources, {capability_binding_ref: 0, bound_agent: 1});
  const rebound = rows(plan([added, event("todo_updated", 2, {fields: ["bound_agent"]}),
    event("todo_updated", 3, {fields: ["goal_bound"], goal_bound: true})]))[0];
  assert.deepEqual(rebound.field_sources, {capability_binding_ref: 0, goal_bound: 2});
  for (const flag of [false, true]) {
    const both = rows(plan([added, event("todo_updated", 2, {fields: ["bound_agent", "goal_bound"], goal_bound: flag})]))[0];
    assert.deepEqual(both.field_sources, flag
      ? {capability_binding_ref: 0, goal_bound: 1}
      : {capability_binding_ref: 0, bound_agent: 1, goal_bound: 1});
  }
});

test("removed continuation remains blocked until an explicit independent handoff repair", () => {
  const added = event("todo_added", 1, {removed_continuation_policy: "author_handoff",
    fields: ["removed_continuation_policy"]});
  const denied = event("todo_updated", 2, {continuation_policy: "independent_handoff", fields: ["continuation_policy"]});
  assert.deepEqual(rows(plan([added, denied]))[0].field_sources, {removed_continuation_policy: 0});
  const repaired = {...denied, has_exclusions: true, fields: ["continuation_policy", "excluded_agents"]};
  assert.deepEqual(rows(plan([added, repaired]))[0].field_sources, {continuation_policy: 1, excluded_agents: 1});
});

test("all historical event kinds have an explicit projection disposition", () => {
  const input = [event("todo_added", 1), event("todo_claimed", 2), event("todo_updated", 3),
    event("todo_blocked", 4), event("todo_deferred", 5), event("todo_completed", 6),
    event("refresh_recorded", 7), event("run_recorded", 8), event("quota_spent", 9),
    event("evidence_attached", 10), event("supervisor_proposed", 11), event("supervisor_receipt_recorded", 12)];
  const result = plan(input);
  assert.deepEqual(result.timeline_indices, [6, 7, 8, 9]);
  assert.equal(rows(result)[0].status, "done");
  assert.equal((result.event_indices as number[]).length, 12);
});

test("continuation is equivalent to one fold and cannot point into the new batch", () => {
  const first = [event("todo_added", 1, {fields: ["title", "claimed_by"]})];
  const next = [event("todo_updated", 2, {priority: "P0", fields: ["title"]}), event("todo_completed", 3, {fields: ["evidence"]})];
  const seed = rows(plan(first));
  const resumed = planStateEventReplay({schema_version: "state_event_replay_request_v0", goal_id: "sample",
    offset: 1, initial_todos: seed, events: next});
  assert.deepEqual(rows(resumed), rows(plan([...first, ...next])));
  assert.throws(() => planStateEventReplay({schema_version: "state_event_replay_request_v0", goal_id: "sample",
    offset: 0, initial_todos: seed, events: next}), /does not precede/);
  assert.deepEqual(seed, rows(plan(first)), "continuation input must remain unchanged");
});
