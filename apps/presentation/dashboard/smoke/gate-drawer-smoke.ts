// Gate drawer contract smoke: the `GET /api/chat/gate-thread` parser keeps
// each kind-specific section of a typed gate (plan card, criteria changes,
// push repos, budget, goal completion) and reports a malformed or missing
// section unavailable instead of failing the thread or showing empty facts;
// decisions are offered only on a successful read of the current open gate;
// and each typed gate's primary approve names the option it selects.
import { gateThreadSignature, parseGateThreadView, type GateSection } from "../src/data/gate-thread";
import {
  gateDecisionAccess,
  gateOptionChoices,
  primaryGateOption,
  type GateThreadState,
} from "../src/features/personal-workspace/gate-decisions";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function equal(actual: unknown, expected: unknown, message: string) {
  const left = JSON.stringify(actual);
  const right = JSON.stringify(expected);
  assert(left === right, `${message}: expected ${right}, received ${left}`);
}

function ready<T>(value: GateSection<T> | undefined, message: string): T {
  assert(value?.state === "ready", `${message}: expected a ready section, received ${JSON.stringify(value)}`);
  return value.value;
}

const state = (value: GateSection<unknown> | undefined) => value?.state ?? "absent";
const base = { ok: true, goal_id: "g", status: "open", awaiting: "awaiting_user", messages: [] };

// Decision 12: the plan card read model (`plan_card_view`).
const card = {
  plan_id: "plan_0123456789ab", status: "pending", revision: 2, title: "Todo app v1", summary: "Contract first.",
  todos: [
    { key: "contract", text: "Write the API contract", required_role: "developer", depends_on: [],
      task_repositories: ["api"], acceptance: "openapi covers CRUD", validation_command: "test -f openapi.yaml" },
    { key: "web", text: "Implement the web UI", required_role: "developer", depends_on: ["contract"],
      task_repositories: ["web"], acceptance: null, validation_command: null },
  ],
};
const planGate = { ...base, todo_id: "todo_plan", kind: "plan_approval", plan_id: card.plan_id };
const plan = ready(parseGateThreadView({ ...planGate, plan: card }).sections.plan, "plan card");
equal(plan.todos.map((todo) => [todo.key, todo.depends_on, todo.task_repositories]),
  [["contract", [], ["api"]], ["web", ["contract"], ["web"]]], "plan todos keep keys, dependencies and repos");
equal([plan.title, plan.revision, plan.todos[0].validation_command], ["Todo app v1", 2, "test -f openapi.yaml"],
  "plan card keeps title, revision and validation command");
// A malformed, missing or backend-flagged card is unavailable, never an empty plan.
for (const [name, payload] of [
  ["todos not a list", { ...planGate, plan: { ...card, todos: 5 } }],
  ["todo not an object", { ...planGate, plan: { ...card, todos: ["contract"] } }],
  ["depends_on not a list", { ...planGate, plan: { ...card, todos: [{ ...card.todos[1], depends_on: "contract" }] } }],
  ["backend plan_error", { ...planGate, plan_error: "plan_malformed" }],
  ["no card at all", planGate],
] as const) {
  const view = parseGateThreadView(payload);
  equal(state(view.sections.plan), "unavailable", `plan card with ${name}`);
}

// Decision 40: malformed criteria changes (or options) do not reject the thread; they are unavailable, not "none".
const criteria = [{ todo_id: "todo_a", old: "CRUD", new: "CRUD paged by 50", reason: "paging" }];
equal(ready(parseGateThreadView({ ...planGate, plan: card, criteria_changes: criteria }).sections.criteriaChanges,
  "criteria changes")[0].new, "CRUD paged by 50", "criteria changes survive parsing");
for (const [name, payload] of [
  ["changes not a list", { ...planGate, plan: card, criteria_changes: 7 }],
  ["change without todo id", { ...planGate, plan: card, criteria_changes: [{ old: "a", new: "b" }] }],
  ["backend criteria error", { ...planGate, plan: card, criteria_changes_error: "criteria_changes_malformed" }],
] as const) {
  const view = parseGateThreadView(payload);
  equal([state(view.sections.criteriaChanges), state(view.sections.plan)], ["unavailable", "ready"],
    `criteria changes with ${name} are unavailable while the plan stays ready`);
}
const badOptions = parseGateThreadView({ ...base, todo_id: "todo_x", kind: "budget_exhausted", options: "raise_budget",
  budget_usd: 0.6, spent_usd: 1 });
equal([state(badOptions.sections.options), state(badOptions.sections.budget)], ["unavailable", "ready"],
  "malformed options are contained to their own section");

// Decision 41: budget_exhausted (values from a live gate).
const budgetGate = { ...base, todo_id: "todo_budget", kind: "budget_exhausted" };
const budget = ready(parseGateThreadView({
  ...budgetGate, options: ["raise_budget", "continue_without_limit", "stop_goal"],
  budget_usd: 0.6, spent_usd: 1, spent_ratio: 1.6667, estimated_usd: 0.25, default_raise_usd: 0.9,
  by_role: [{ role: "orchestrator", cost_usd: 0.75, turns: 3 }, { role: "developer", cost_usd: 0.25, turns: 1 }],
}).sections.budget, "budget");
equal([budget.budget_usd, budget.spent_usd, budget.estimated_usd, budget.default_raise_usd, budget.by_role?.map((row) => row.role)],
  [0.6, 1, 0.25, 0.9, ["orchestrator", "developer"]], "budget fields survive parsing");
for (const [name, payload] of [
  ["budget as text", { ...budgetGate, budget_usd: "0.6", spent_usd: 1 }],
  ["role cost as text", { ...budgetGate, budget_usd: 0.6, spent_usd: 1, by_role: [{ role: "developer", cost_usd: "0.25" }] }],
  ["no budget facts", budgetGate],
] as const) {
  equal(state(parseGateThreadView(payload).sections.budget), "unavailable", `budget with ${name} is unavailable, not zero`);
}

// G8: push_request.
const pushGate = { ...base, todo_id: "todo_push", kind: "push_request" };
const push = ready(parseGateThreadView({
  ...pushGate, push_reason: "all_merged",
  push_repos: [
    { name: "api", status: "ready", branch: "main", remote: "origin", commit_range: "0123456789ab..ba9876543210",
      unpushed_commits: 2, log: ["ba98765 Merge todo contract", "0f0f0f0 Add openapi"], merge_target: "main" },
    { name: "web", status: "no_remote", branch: "main", remote: null, note: "no remote configured (local-only repo); not pushed" },
  ],
}).sections.push, "push");
equal(push.push_repos.map((repo) => [repo.name, repo.status, repo.commit_range ?? null, repo.log?.length ?? 0]),
  [["api", "ready", "0123456789ab..ba9876543210", 2], ["web", "no_remote", null, 0]], "push repos keep status, range and log");
equal(state(parseGateThreadView({ ...pushGate, push_repos: {} }).sections.push), "unavailable", "malformed push repos");
equal(state(parseGateThreadView(pushGate).sections.push), "unavailable", "a push gate without its repos");

// Decision 42: goal_complete.
const completeGate = { ...base, todo_id: "todo_done", kind: "goal_complete" };
const completion = {
  completion_repos: [{ name: "api", branch: "main", remote: "origin", head: "abc", push: "pushed", merged_todo_commits: [{ sha: "a" }] }],
  completion_todos: { accepted: 3, rejects: 1, superseded: 0, done_without_review: 1, orchestrator_todos: 2 },
  completion_usage: { cost_usd: 4.2, cost_estimated_usd: 0.5, turns: 12, agent_hours: 1.5, by_role: [] },
  follow_ups: [{ todo_id: "todo_alert", task_class: null, text: "Budget at 80%" }],
};
const complete = ready(parseGateThreadView({ ...completeGate, ...completion }).sections.completion, "completion");
equal([complete.completion_todos.accepted, complete.completion_usage.turns, complete.completion_repos?.[0].push,
  complete.completion_repos?.[0].merged_todo_commits.length, complete.follow_ups?.[0].text],
[3, 12, "pushed", 1, "Budget at 80%"], "goal completion fields survive parsing");
for (const [name, payload] of [
  ["todo counts as text", { ...completeGate, ...completion, completion_todos: "3" }],
  ["merged commits not a list", { ...completeGate, ...completion, completion_repos: [{ name: "api", merged_todo_commits: 4 }] }],
  ["no snapshot", completeGate],
] as const) {
  equal(state(parseGateThreadView(payload).sections.completion), "unavailable", `completion with ${name}`);
}

// Only the thread core can make a read fail.
let coreRejected = false;
try {
  parseGateThreadView({ ...base, todo_id: "todo_x", kind: "decision", messages: "none" });
} catch {
  coreRejected = true;
}
assert(coreRejected, "a malformed thread core still rejects the read");

// The drawer refreshes the role board when a read moves the thread.
const thread = parseGateThreadView({ ...budgetGate, budget_usd: 0.6, spent_usd: 1, messages: [
  { seq: 1, message_id: "m1", author: "user", text: "why?", at: "2026-09-28T00:00:00Z" },
] });
const signature = gateThreadSignature(thread);
assert(signature !== gateThreadSignature({ ...thread, messages: [...thread.messages, thread.messages[0]] }), "a new message changes the signature");
assert(signature !== gateThreadSignature({ ...thread, awaiting: "awaiting_orchestrator" }), "an awaiting change changes the signature");
assert(signature !== gateThreadSignature({ ...thread, awaiting: "closed", status: "done" }), "closing changes the signature");

// Readiness is bound to the gate shown now: only its successful open read is actionable.
const current = { goalId: "g", todoId: "todo_budget" };
const readyState: GateThreadState = { goalId: "g", todoId: "todo_budget", status: "ready", view: thread };
const access = (value: GateThreadState | null, gate: { goalId: string; todoId: string } | null = current) => {
  const result = gateDecisionAccess(value, gate);
  return [result.actionable, result.reason, result.view?.todo_id ?? null];
};
equal(access(readyState), [true, "ready", "todo_budget"], "a successful read of this open gate is actionable");
equal(access(null), [false, "pending", null], "no read yet is not actionable");
equal(access(readyState, { goalId: "g", todoId: "todo_plan" }), [false, "pending", null],
  "another gate's read is never reused after a switch");
equal(access({ ...readyState, goalId: "other" }), [false, "pending", null], "the goal is part of the identity");
equal(access({ ...readyState, status: "closed" }), [false, "closed", "todo_budget"], "a closed gate is shown but not actionable");
equal(access({ goalId: "g", todoId: "todo_budget", status: "unavailable" }), [false, "unavailable", null], "a failed read is not actionable");
equal(access({ goalId: "g", todoId: "todo_budget", status: "not_gate" }), [false, "not_gate", null], "a non-gate todo is not actionable");

// Typed gates: the primary approve names the option it selects (the backend's approve default).
equal(["acceptor_blocked", "budget_exhausted", "goal_complete"].map((kind) => primaryGateOption(kind)?.option),
  ["retry_acceptance", "raise_budget", "close_goal"], "primary option per typed gate");
equal(["decision", "plan_approval", "push_request", "vote", null, undefined].map((kind) => primaryGateOption(kind)),
  [null, null, null, null, null, null], "plain, plan and push gates keep the plain approve");
// Option lists and implied decisions mirror REVIEW_GATE_OPTION_DECISIONS, BUDGET_GATE_OPTION_DECISIONS
// and GOAL_COMPLETE_OPTION_DECISIONS in loopx/.
equal(Object.fromEntries(["acceptor_blocked", "budget_exhausted", "goal_complete"].map((kind) => [
  kind, gateOptionChoices(kind)?.map((choice) => [choice.option, choice.resolution]),
])), {
  acceptor_blocked: [["retry_acceptance", "approve"], ["accept_manually", "approve"], ["return_to_developer", "reject"], ["cancel_todo", "cancel"]],
  budget_exhausted: [["raise_budget", "approve"], ["continue_without_limit", "approve"], ["stop_goal", "reject"]],
  goal_complete: [["close_goal", "approve"], ["add_work", "reject"], ["leave_open", "cancel"]],
}, "typed gate options and their decisions");
for (const kind of ["acceptor_blocked", "budget_exhausted", "goal_complete"]) {
  equal(gateOptionChoices(kind)?.filter((choice) => choice.primary).length, 1, `${kind} has exactly one primary option`);
  equal(primaryGateOption(kind)?.resolution, "approve", `${kind} primary option is an approve`);
}

console.log("gate drawer smoke: ok");
