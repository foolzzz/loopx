// Role board contract smoke (fork slice S8): the zod schema accepts the
// backend `role_board` projection (including the S2 `in_review` status), and
// the board model derives stages and swimlanes from it.
import fixture from "./role-board-fixture.json";

import { roleBoardSchema, runGoalSchema, turnUsageSummarySchema } from "../src/data/status";
import {
  buildRoleBoardGrid,
  roleBoardFromProjection,
  roleBoardLane,
  roleBoardStage,
  turnUsageFromProjection,
} from "../src/features/personal-workspace/role-board-model";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function equal(actual: unknown, expected: unknown, message: string) {
  const left = JSON.stringify(actual);
  const right = JSON.stringify(expected);
  assert(left === right, `${message}: expected ${right}, received ${left}`);
}

const parsed = roleBoardSchema.parse(fixture);
equal(parsed.todos.find((todo) => todo.todo_id === "todo_rb_review")?.status, "in_review", "in_review survives parsing");

// A goal row carries the board and the registry roles; a malformed board degrades to null.
const goal = runGoalSchema.parse({
  id: "goal-rb",
  coordination: { agent_model: "role_v1", registered_agents: ["fable-orch"], agent_roles: { "fable-orch": "orchestrator" } },
  role_board: fixture,
});
equal(goal.coordination?.agent_roles, { "fable-orch": "orchestrator" }, "agent roles are kept on the goal row");
assert(goal.role_board?.agents.length === 4, "role board attaches to the goal row");
equal(runGoalSchema.parse({ id: "goal-rb", role_board: { schema_version: "other" } }).role_board, null,
  "an unknown board version degrades to null instead of failing the payload");

// Forward compatibility: unknown enum values fall back instead of failing the board.
const drifted = roleBoardSchema.parse({
  ...fixture,
  agents: [{ agent_id: "x", role: "reviewer", activity: "sleeping", running_todo_ids: [] }],
  todos: [{ todo_id: "todo_x", text: "x", status: "needs_triage", effective_role: "reviewer", reject_count: -1 }],
  gates: [{ todo_id: "todo_g", text: "g", kind: "vote", awaiting: "someone" }],
});
equal([drifted.agents[0].role, drifted.agents[0].activity], [null, "unknown"], "unknown agent role/activity fall back");
equal([drifted.todos[0].effective_role, drifted.todos[0].reject_count, drifted.todos[0].running], ["developer", 0, false],
  "unknown todo role and invalid reject count fall back");
equal([drifted.gates[0].kind, drifted.gates[0].awaiting], ["decision", "awaiting_user"], "unknown gate kind/awaiting fall back");

const board = roleBoardFromProjection(parsed);
equal(board.cards.find((card) => card.todoId === "todo_rb_review")?.acceptanceCriteria, "GET /todos returns 200; tests pass",
  "G2: the card carries the todo's acceptance criteria");
equal(board.cards.find((card) => card.todoId === "todo_rb_running")?.acceptanceCriteria, null, "cards without criteria stay empty");
equal(board.cards.find((card) => card.todoId === "todo_rb_planned")?.dependencyWait,
  "waiting for dependency todo_rb_review (status in_review)", "G6: the card carries its dependency wait");
equal(board.cards.find((card) => card.todoId === "todo_rb_running")?.dependencyWait, null, "cards without a wait stay empty");
equal(board.omittedCardCount, 3, "omitted card count is surfaced");
const stages = Object.fromEntries(board.cards.map((card) => [card.todoId, roleBoardStage(card)]));
equal(stages, {
  todo_rb_review: "in_review",
  todo_rb_running: "running",
  todo_rb_rework: "rework",
  todo_rb_assigned: "assigned",
  todo_rb_planned: "planned",
  todo_rb_orch: "assigned",
  todo_rb_done: "done",
}, "stages derive from status, active Turn, reject count and binding");

// Precedence: done beats everything; in_review beats running; running beats rework.
const base = { claimedBy: "a", rejectCount: 1, running: true };
equal(roleBoardStage({ ...base, status: "completed" }), "done", "completed maps to done");
equal(roleBoardStage({ ...base, status: "in_review" }), "in_review", "in_review wins over running");
equal(roleBoardStage({ ...base, status: "open" }), "running", "running wins over rework");
equal(roleBoardStage({ ...base, running: false, status: "deferred" }), "rework", "rejected deferred work is rework");
equal(roleBoardLane({ role: "developer", status: "in_review" }), "acceptor", "work under review sits in the acceptor lane");

const grid = buildRoleBoardGrid(board);
equal(grid.lanes.map((lane) => lane.role), ["orchestrator", "developer", "acceptor"], "swimlanes follow the role order");
const lane = (role: string) => grid.lanes.find((candidate) => candidate.role === role)!;
equal(lane("developer").agents.map((agent) => [agent.agentId, agent.activity]),
  [["opus-dev", "running"], ["opus-dev-2", "cooldown"]], "developer lane carries its agents' activity");
equal(lane("acceptor").cells.in_review.map((card) => card.todoId), ["todo_rb_review"], "in_review card sits with the acceptor");
equal(lane("developer").cells.rework.map((card) => [card.todoId, card.rejectCount]), [["todo_rb_rework", 2]], "rejected card is in rework");
equal(lane("orchestrator").cells.assigned.map((card) => card.todoId), ["todo_rb_orch"], "orchestrator work stays in its lane");
equal(grid.stageCounts, { planned: 1, assigned: 2, running: 1, in_review: 1, rework: 1, done: 1 }, "stage counts");
equal(grid.userGates.map((gate) => gate.todoId), ["todo_rb_plan_gate", "todo_rb_decision"],
  "gates waiting on the user come first");

// Fork G9: the usage summary parses, degrades to null when malformed, and maps to the strip model.
const usage = turnUsageSummarySchema.parse({
  schema_version: "loopx_turn_usage_summary_v0", turns: 3, agent_hours: 1.25, cost_usd: 0.14,
  cost_estimated_usd: 0.02, accepted_todos: 1, cost_per_accepted_todo_usd: 0.14,
  by_role: [{ role: "developer", turns: 2, agent_hours: 1, cost_usd: 0.09 }], budget: { budget_usd: 1, spent_ratio: 0.14 },
});
const strip = turnUsageFromProjection(usage);
equal([strip.turns, strip.costUsd, strip.costEstimatedUsd, strip.budgetRatio, strip.byRole[0].role],
  [3, 0.14, 0.02, 0.14, "developer"], "usage summary maps to the strip model");
equal(runGoalSchema.parse({ id: "g", turn_usage_summary: { schema_version: "other" } }).turn_usage_summary, null,
  "an unknown usage summary degrades to null");

console.log("role board smoke: ok");
