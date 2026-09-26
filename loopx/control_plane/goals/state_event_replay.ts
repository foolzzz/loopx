/** Replay admission and lifecycle projection for the retained event source.
 * Content stays in the host codec: ordinals address that exact input batch.
 * This pure plan grants no append, lease, capture or promotion authority. */
import type {JsonObject} from "../effect_program.ts";
import {EffectRuntimeRequestError} from "../effect_runtime_errors.ts";
import {assertNever, requireJsonObject, requireNonEmptyString, requireStringLiteral, requireStringArray} from "../runtime_decode.ts";
import {authorityUnicodeCompare} from "../coordination/authority_store_codec.ts";
import {normalizeTodoPriority} from "../todos/priority.ts";

const KINDS = ["todo_added", "todo_claimed", "todo_updated", "todo_blocked", "todo_deferred",
  "todo_completed", "todo_in_review", "todo_reopened", "refresh_recorded", "run_recorded", "quota_spent", "evidence_attached",
  "supervisor_proposed", "supervisor_receipt_recorded"] as const;
type Kind = typeof KINDS[number];
type Status = "open" | "blocked" | "deferred" | "done" | "in_review";
const TODO_LIFECYCLE_KINDS = ["todo_added", "todo_claimed", "todo_updated", "todo_blocked", "todo_deferred",
  "todo_completed", "todo_in_review", "todo_reopened"];
interface Event {
  ordinal: number; id: string; goal: string; kind: Kind; sequence: number | null;
  time: string; todo: string | null; role: "user" | "agent" | null;
  priority: string | null; plannerOrder: number | null; contentChanged: boolean;
  fields: string[]; binding: string | null; continuation: string | null;
  removedPolicy: string | null; exclusions: boolean; goalBound: boolean | null;
}
interface Todo {
  todo_id: string; field_sources: Record<string, number>; status: Status; done: boolean;
  role: "user" | "agent"; priority: string; planner_order: number | null;
  source_section: string; render_priority: boolean; append_sequence: number | null;
  binding: string | null; removedPolicy: string | null;
}
function nullableInteger(value: unknown, name: string, positive = false): number | null {
  if (value === null) return null;
  if (typeof value !== "number" || !Number.isSafeInteger(value) || (positive && value < 1)) {
    throw new EffectRuntimeRequestError(`${name} must be ${positive ? "a positive" : "a"} safe integer or null`);
  }
  return value;
}
function nullableText(value: unknown, name: string): string | null {
  return value === null ? null : requireNonEmptyString(value, name);
}
function decode(raw: unknown, index: number, offset: number): Event {
  const ordinal = index + offset;
  const r = requireJsonObject(raw, "event replay facts");
  const kind = requireStringLiteral(r.event_type, KINDS, "event_type");
  const todo = r.todo_id === null ? null : requireNonEmptyString(r.todo_id, "todo_id");
  if (TODO_LIFECYCLE_KINDS.includes(kind) && todo === null) throw new EffectRuntimeRequestError(`${kind} requires refs.todo_id`);
  if (typeof r.content_changed !== "boolean") throw new EffectRuntimeRequestError("content_changed must be boolean");
  if (typeof r.has_exclusions !== "boolean" || (r.goal_bound !== null && typeof r.goal_bound !== "boolean")) {
    throw new EffectRuntimeRequestError("event ownership facts require explicit booleans");
  }
  return {ordinal, id: requireNonEmptyString(r.event_id, "event_id"), goal: requireNonEmptyString(r.goal_id, "goal_id"),
    kind, todo, sequence: nullableInteger(r.append_sequence, "append_sequence", true),
    time: requireNonEmptyString(r.recorded_at, "recorded_at"),
    role: r.role === null ? null : requireStringLiteral(r.role, ["user", "agent"] as const, "role"),
    priority: r.priority === null ? null : normalizeTodoPriority(r.priority),
    plannerOrder: nullableInteger(r.planner_order, "planner_order"), contentChanged: r.content_changed,
    fields: requireStringArray(r.fields, "event content fields"),
    binding: nullableText(r.capability_binding_ref, "capability_binding_ref"),
    continuation: nullableText(r.continuation_policy, "continuation_policy"),
    removedPolicy: nullableText(r.removed_continuation_policy, "removed_continuation_policy"),
    exclusions: r.has_exclusions, goalBound: r.goal_bound};
}

/** Continuation state is ephemeral replay data, never a durable authority token. */
function decodeTodo(value: unknown): Todo {
  const r = requireJsonObject(value, "replay continuation");
  const sources = requireJsonObject(r.field_sources, "content sources");
  const field_sources: Record<string, number> = {};
  for (const [key, index] of Object.entries(sources)) {
    const n = nullableInteger(index, "content source ordinal");
    if (n === null || n < 0) throw new EffectRuntimeRequestError("invalid content source ordinal");
    Object.defineProperty(field_sources, key, {value: n, enumerable: true, writable: true, configurable: true});
  }
  const role = requireStringLiteral(r.role, ["user", "agent"] as const, "role");
  const status = requireStringLiteral(r.status, ["open", "done", "blocked", "deferred", "in_review"] as const, "status");
  if (typeof r.render_priority !== "boolean") throw new EffectRuntimeRequestError("render_priority must be boolean");
  const priority = normalizeTodoPriority(r.priority);
  if (priority === null) throw new EffectRuntimeRequestError("continuation priority is required");
  return {todo_id: requireNonEmptyString(r.todo_id, "todo_id"), field_sources,
    status, done: status === "done", role, priority,
    source_section: role === "user" ? "User Todo / Owner Review Reading Queue" : "Agent Todo",
    render_priority: r.render_priority, planner_order: nullableInteger(r.planner_order, "planner_order"),
    append_sequence: nullableInteger(r.append_sequence, "append_sequence", true),
    binding: nullableText(r.binding, "capability binding"), removedPolicy: nullableText(r.removedPolicy, "removed policy")};
}

export function planStateEventReplay(value: unknown): JsonObject {
  const r = requireJsonObject(value, "state event replay request");
  if (r.schema_version !== "state_event_replay_request_v0" || !Array.isArray(r.events)) {
    throw new EffectRuntimeRequestError("state event replay requires its schema and complete event facts");
  }
  const offset = nullableInteger(r.offset ?? 0, "batch offset");
  if (offset === null || offset < 0 || offset > Number.MAX_SAFE_INTEGER - r.events.length) {
    throw new EffectRuntimeRequestError("invalid batch offset");
  }
  const events = r.events.map((event, index) => decode(event, index, offset));
  const ids = new Set<string>();
  for (const event of events) {
    if (ids.has(event.id)) throw new EffectRuntimeRequestError("event replay facts must be deduplicated by the source codec");
    ids.add(event.id);
  }
  events.sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0) || authorityUnicodeCompare(a.time, b.time) || authorityUnicodeCompare(a.id, b.id));
  const goal = r.goal_id === null ? events[0]?.goal ?? "" : requireNonEmptyString(r.goal_id, "goal_id");
  const todos = new Map<string, Todo>();
  if (r.initial_todos !== undefined && !Array.isArray(r.initial_todos)) {
    throw new EffectRuntimeRequestError("initial_todos must be an array");
  }
  for (const raw of (r.initial_todos ?? []) as unknown[]) {
    const todo = decodeTodo(raw);
    if (todos.has(todo.todo_id) || Object.values(todo.field_sources).some(index => index >= offset)) {
      throw new EffectRuntimeRequestError("continuation does not precede this replay batch");
    }
    todos.set(todo.todo_id, todo);
  }
  const timeline: number[] = [];
  for (const event of events) {
    if (event.goal !== goal) throw new EffectRuntimeRequestError("all events in a projection must share one goal_id");
    const kind = event.kind;
    switch (kind) {
      case "todo_added": {
        const id = event.todo!; // decode requires identity for every Todo event.
        if (todos.has(id)) throw new EffectRuntimeRequestError(`todo_id already exists: ${id}; use todo_updated`);
        const role = event.role ?? "agent";
        todos.set(id, {todo_id: id, field_sources: Object.fromEntries(event.fields.map(field => [field, event.ordinal])), status: "open", done: false,
          role, priority: event.priority ?? "P2", planner_order: event.plannerOrder,
          source_section: role === "user" ? "User Todo / Owner Review Reading Queue" : "Agent Todo",
          render_priority: event.priority !== null, append_sequence: event.sequence,
          binding: event.binding, removedPolicy: event.removedPolicy});
        break;
      }
      case "todo_claimed": case "todo_updated": case "todo_blocked": case "todo_deferred": case "todo_completed":
      case "todo_in_review": case "todo_reopened": {
        const todo = todos.get(event.todo!);
        if (!todo) throw new EffectRuntimeRequestError(`${kind} references unknown todo_id: ${event.todo}`);
        for (const field of event.fields) Object.defineProperty(todo.field_sources, field,
          {value: event.ordinal, enumerable: true, configurable: true, writable: true});
        if (!event.fields.includes("last_actor_agent_id")) delete todo.field_sources.last_actor_agent_id;
        if (kind === "todo_updated") {
          if (event.binding !== null) {
            if (todo.binding !== null && todo.binding !== event.binding) {
              throw new EffectRuntimeRequestError("capability_binding_ref is immutable once set");
            }
            todo.binding = event.binding;
          }
          if (event.removedPolicy !== null) {
            delete todo.field_sources.continuation_policy;
            todo.removedPolicy = event.removedPolicy;
          } else if (event.continuation !== null && todo.removedPolicy !== null) {
            if (event.continuation === "independent_handoff" && event.exclusions) {
              delete todo.field_sources.removed_continuation_policy;
              todo.removedPolicy = null;
            } else delete todo.field_sources.continuation_policy;
          }
          if (event.fields.includes("bound_agent") && event.goalBound === null) delete todo.field_sources.goal_bound;
          if (event.goalBound === true) delete todo.field_sources.bound_agent;
          if (event.role !== null) todo.role = event.role;
          if (event.priority !== null) todo.priority = event.priority;
          if (event.priority !== null || event.contentChanged) todo.render_priority = true;
          todo.source_section = todo.role === "user" ? "User Todo / Owner Review Reading Queue" : "Agent Todo";
        } else if (kind === "todo_blocked") todo.status = "blocked";
        else if (kind === "todo_deferred") todo.status = "deferred";
        else if (kind === "todo_completed") todo.status = "done";
        // role_v1 acceptance: delivery awaits the acceptor; a rejection reopens.
        else if (kind === "todo_in_review") todo.status = "in_review";
        else if (kind === "todo_reopened") todo.status = "open";
        todo.done = todo.status === "done";
        break;
      }
      case "refresh_recorded": case "run_recorded": case "quota_spent": case "evidence_attached":
        timeline.push(event.ordinal); break;
      case "supervisor_proposed": case "supervisor_receipt_recorded": break;
      default: assertNever(kind, "unhandled event replay kind");
    }
  }
  const items = [...todos.values()].sort((a, b) => Number(a.role !== "user") - Number(b.role !== "user") ||
    authorityUnicodeCompare(a.priority, b.priority) || (a.planner_order ?? 9999) - (b.planner_order ?? 9999) ||
    (a.append_sequence ?? 0) - (b.append_sequence ?? 0));
  return {schema_version: "state_event_replay_plan_v0", goal_id: goal,
    event_indices: events.map(e => e.ordinal), timeline_indices: timeline,
    todos: items.map(item => ({...item, sort_key: [Number(item.role !== "user"), item.priority,
      item.planner_order ?? 9999, item.append_sequence ?? 0]}))};
}
