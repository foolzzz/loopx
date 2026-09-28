// Gate drawer contract smoke: the `GET /api/chat/gate-thread` schema keeps the
// kind-specific fields of typed gates (plan card, push repos, budget, goal
// completion) and degrades a malformed field instead of failing the thread,
// and each typed gate's primary approve names the option it selects.
import { gateThreadSignature, gateThreadViewSchema } from "../src/data/gate-thread";
import { gateOptionChoices, primaryGateOption } from "../src/features/personal-workspace/gate-decisions";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function equal(actual: unknown, expected: unknown, message: string) {
  const left = JSON.stringify(actual);
  const right = JSON.stringify(expected);
  assert(left === right, `${message}: expected ${right}, received ${left}`);
}

const base = { ok: true, goal_id: "g", status: "open", awaiting: "awaiting_user", messages: [] };

// Decision 12: the plan card read model (`plan_card_view`).
const plan = gateThreadViewSchema.parse({
  ...base, todo_id: "todo_plan", kind: "plan_approval", plan_id: "plan_0123456789ab",
  plan: {
    plan_id: "plan_0123456789ab", status: "pending", revision: 2, title: "Todo app v1", summary: "Contract first.",
    todos: [
      { key: "contract", text: "Write the API contract", required_role: "developer", depends_on: [],
        task_repositories: ["api"], acceptance: "openapi covers CRUD", validation_command: "test -f openapi.yaml" },
      { key: "web", text: "Implement the web UI", required_role: "developer", depends_on: ["contract"],
        task_repositories: ["web"], acceptance: null, validation_command: null },
    ],
  },
});
equal(plan.plan?.todos.map((todo) => [todo.key, todo.depends_on, todo.task_repositories]),
  [["contract", [], ["api"]], ["web", ["contract"], ["web"]]], "plan todos keep keys, dependencies and repos");
equal([plan.plan?.title, plan.plan?.revision, plan.plan?.todos[0].validation_command],
  ["Todo app v1", 2, "test -f openapi.yaml"], "plan card keeps title, revision and validation command");

// Decision 41: budget_exhausted (values from a live gate).
const budget = gateThreadViewSchema.parse({
  ...base, todo_id: "todo_budget", kind: "budget_exhausted", options: ["raise_budget", "continue_without_limit", "stop_goal"],
  budget_usd: 0.6, spent_usd: 1, spent_ratio: 1.6667, estimated_usd: 0.25, default_raise_usd: 0.9,
  by_role: [{ role: "orchestrator", cost_usd: 0.75, turns: 3 }, { role: "developer", cost_usd: 0.25, turns: 1 }],
});
equal([budget.budget_usd, budget.spent_usd, budget.estimated_usd, budget.default_raise_usd, budget.by_role?.map((row) => row.role)],
  [0.6, 1, 0.25, 0.9, ["orchestrator", "developer"]], "budget fields survive parsing");

// G8: push_request.
const push = gateThreadViewSchema.parse({
  ...base, todo_id: "todo_push", kind: "push_request", push_reason: "all_merged",
  push_repos: [
    { name: "api", status: "ready", branch: "main", remote: "origin", commit_range: "0123456789ab..ba9876543210",
      unpushed_commits: 2, log: ["ba98765 Merge todo contract", "0f0f0f0 Add openapi"], merge_target: "main" },
    { name: "web", status: "no_remote", branch: "main", remote: null, note: "no remote configured (local-only repo); not pushed" },
  ],
});
equal(push.push_repos?.map((repo) => [repo.name, repo.status, repo.commit_range ?? null, repo.log.length]),
  [["api", "ready", "0123456789ab..ba9876543210", 2], ["web", "no_remote", null, 0]], "push repos keep status, range and log");

// Decision 42: goal_complete.
const complete = gateThreadViewSchema.parse({
  ...base, todo_id: "todo_done", kind: "goal_complete",
  completion_repos: [{ name: "api", branch: "main", remote: "origin", head: "abc", push: "pushed", merged_todo_commits: [{ sha: "a" }] }],
  completion_todos: { accepted: 3, rejects: 1, superseded: 0, done_without_review: 1, orchestrator_todos: 2 },
  completion_usage: { cost_usd: 4.2, cost_estimated_usd: 0.5, turns: 12, agent_hours: 1.5, by_role: [] },
  follow_ups: [{ todo_id: "todo_alert", task_class: null, text: "Budget at 80%" }],
});
equal([complete.completion_todos?.accepted, complete.completion_usage?.turns, complete.completion_repos?.[0].push,
  complete.completion_repos?.[0].merged_todo_commits.length, complete.follow_ups?.[0].text],
[3, 12, "pushed", 1, "Budget at 80%"], "goal completion fields survive parsing");

// Drift: a malformed typed field hides its section; the thread still parses.
const drifted = gateThreadViewSchema.parse({
  ...base, todo_id: "todo_x", kind: "budget_exhausted", messages: [
    { seq: 1, message_id: "m1", author: "user", text: "why?", at: "2026-09-28T00:00:00Z" },
  ],
  plan: { plan_id: "plan_0123456789ab", todos: "bad" }, budget_usd: "0.6", by_role: "orchestrator",
  push_repos: {}, completion_todos: "3", follow_ups: 7,
});
equal([drifted.messages.length, drifted.plan?.todos, drifted.budget_usd, drifted.by_role, drifted.push_repos,
  drifted.completion_todos, drifted.follow_ups], [1, [], undefined, undefined, undefined, undefined, undefined],
"malformed typed fields degrade to undefined");

// The drawer refreshes the role board when a read moves the thread.
const signature = gateThreadSignature(drifted);
assert(signature !== gateThreadSignature({ ...drifted, messages: [...drifted.messages, drifted.messages[0]] }), "a new message changes the signature");
assert(signature !== gateThreadSignature({ ...drifted, awaiting: "awaiting_orchestrator" }), "an awaiting change changes the signature");
assert(signature !== gateThreadSignature({ ...drifted, awaiting: "closed", status: "done" }), "closing changes the signature");
equal(signature, gateThreadSignature({ ...drifted, text: "same" } as typeof drifted), "an unchanged thread keeps its signature");

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
