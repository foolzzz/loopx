// Role board model (fork slice S8, design decisions 7 and 25).
//
// The status projection (`run_history.goals[].role_board`) carries raw facts;
// this module derives the board: columns are stages, rows are role swimlanes.
// It has no runtime imports so contract smokes can compile it standalone.

export const ROLE_BOARD_ROLES = ["orchestrator", "developer", "acceptor"] as const;
export const ROLE_BOARD_STAGES = ["planned", "assigned", "running", "in_review", "rework", "done"] as const;
// `loopx.gate_threads.GATE_KINDS`; the status schema maps an unknown kind to "decision".
export const ROLE_BOARD_GATE_KINDS = [
  "decision", "plan_approval", "acceptor_blocked", "push_request", "budget_exhausted", "goal_complete",
] as const;

export type RoleBoardRole = (typeof ROLE_BOARD_ROLES)[number];
export type RoleBoardStage = (typeof ROLE_BOARD_STAGES)[number];
export type RoleBoardGateKind = (typeof ROLE_BOARD_GATE_KINDS)[number];
export type RoleBoardActivity = "running" | "idle" | "cooldown" | "unavailable" | "unknown";

export type WorkspaceRoleBoardAgent = {
  activity: RoleBoardActivity;
  agentId: string;
  provider?: string | null;
  reason?: string | null;
  role: RoleBoardRole | null;
  runningGoalIds?: string[];
  runningTodoIds: string[];
  until?: number | null;
};

export type WorkspaceRoleBoardCard = {
  acceptanceCriteria?: string | null;
  acceptorAgent?: string | null;
  claimedBy?: string | null;
  dependencyWait?: string | null;
  criteriaChangeGateTodoId?: string | null;
  criteriaChangePlanId?: string | null;
  planGateTodoId?: string | null;
  planId?: string | null;
  priority?: string | null;
  rejectCount: number;
  repositories: string[];
  requiredRole?: RoleBoardRole | null;
  requiresAcceptance: boolean;
  role: RoleBoardRole;
  running: boolean;
  runningAgentId?: string | null;
  status: string;
  text: string;
  todoId: string;
};

export type WorkspaceRoleBoardGate = {
  awaiting: "awaiting_user" | "awaiting_orchestrator";
  blocksAgent?: string | null;
  kind: RoleBoardGateKind;
  messageCount: number;
  planId?: string | null;
  planRevision?: number | null;
  planStatus?: string | null;
  planTitle?: string | null;
  planTodoCount?: number | null;
  text: string;
  todoId: string;
  updatedAt?: string | null;
};

export type WorkspaceRoleBoard = {
  agents: WorkspaceRoleBoardAgent[];
  cards: WorkspaceRoleBoardCard[];
  dispatcherAvailable: boolean;
  dispatcherServing: boolean;
  gates: WorkspaceRoleBoardGate[];
  omittedCardCount: number;
};

/** Structural view of the parsed `role_board` projection (snake_case wire shape). */
export type RoleBoardProjectionInput = {
  agents: Array<{
    activity: RoleBoardActivity;
    agent_id: string;
    provider?: string | null;
    reason?: string | null;
    role?: RoleBoardRole | null;
    running_goal_ids?: string[];
    running_todo_ids: string[];
    until?: number | null;
  }>;
  dispatcher: { available: boolean; serving: boolean };
  gates: Array<{
    awaiting: "awaiting_user" | "awaiting_orchestrator";
    blocks_agent?: string | null;
    kind: RoleBoardGateKind;
    message_count: number;
    plan_id?: string | null;
    plan_revision?: number | null;
    plan_status?: string | null;
    plan_title?: string | null;
    plan_todo_count?: number | null;
    text: string;
    todo_id: string;
    updated_at?: string | null;
  }>;
  omitted?: Record<string, number>;
  todos: Array<{
    acceptance_criteria?: string | null;
    acceptor_agent?: string | null;
    claimed_by?: string | null;
    dependency_wait?: string | null;
    criteria_change_gate_todo_id?: string | null;
    criteria_change_plan_id?: string | null;
    effective_role: RoleBoardRole;
    plan_gate_todo_id?: string | null;
    plan_id?: string | null;
    priority?: string | null;
    reject_count: number;
    required_role?: RoleBoardRole | null;
    requires_acceptance: boolean;
    running: boolean;
    running_agent_id?: string | null;
    status: string;
    task_repositories: string[];
    text: string;
    todo_id: string;
  }>;
};

export function roleBoardFromProjection(projection: RoleBoardProjectionInput): WorkspaceRoleBoard {
  return {
    agents: projection.agents.map((agent) => ({
      activity: agent.activity,
      agentId: agent.agent_id,
      provider: agent.provider ?? null,
      reason: agent.reason ?? null,
      role: agent.role ?? null,
      runningGoalIds: agent.running_goal_ids,
      runningTodoIds: agent.running_todo_ids,
      until: agent.until ?? null,
    })),
    cards: projection.todos.map((todo) => ({
      acceptanceCriteria: todo.acceptance_criteria ?? null,
      acceptorAgent: todo.acceptor_agent ?? null,
      claimedBy: todo.claimed_by ?? null,
      dependencyWait: todo.dependency_wait ?? null,
      criteriaChangeGateTodoId: todo.criteria_change_gate_todo_id ?? null,
      criteriaChangePlanId: todo.criteria_change_plan_id ?? null,
      planGateTodoId: todo.plan_gate_todo_id ?? null,
      planId: todo.plan_id ?? null,
      priority: todo.priority ?? null,
      rejectCount: todo.reject_count,
      repositories: todo.task_repositories,
      requiredRole: todo.required_role ?? null,
      requiresAcceptance: todo.requires_acceptance,
      role: todo.effective_role,
      running: todo.running,
      runningAgentId: todo.running_agent_id ?? null,
      status: todo.status,
      text: todo.text,
      todoId: todo.todo_id,
    })),
    dispatcherAvailable: projection.dispatcher.available,
    dispatcherServing: projection.dispatcher.serving,
    gates: projection.gates.map((gate) => ({
      awaiting: gate.awaiting,
      blocksAgent: gate.blocks_agent ?? null,
      kind: gate.kind,
      messageCount: gate.message_count,
      planId: gate.plan_id ?? null,
      planRevision: gate.plan_revision ?? null,
      planStatus: gate.plan_status ?? null,
      planTitle: gate.plan_title ?? null,
      planTodoCount: gate.plan_todo_count ?? null,
      text: gate.text,
      todoId: gate.todo_id,
      updatedAt: gate.updated_at ?? null,
    })),
    omittedCardCount: (projection.omitted?.open_todos ?? 0) + (projection.omitted?.done_todos ?? 0),
  };
}

const DONE_STATUSES = new Set(["done", "completed"]);

/**
 * Derived stage (decision 7): in_review is a real status; running, rework and
 * assigned are derived from an active Turn/lease, the reject count and a bound agent.
 */
export function roleBoardStage(card: Pick<WorkspaceRoleBoardCard, "claimedBy" | "rejectCount" | "running" | "status">): RoleBoardStage {
  const status = card.status.trim().toLowerCase();
  if (DONE_STATUSES.has(status)) return "done";
  if (status === "in_review") return "in_review";
  if (card.running) return "running";
  if (card.rejectCount > 0) return "rework";
  if (card.claimedBy) return "assigned";
  return "planned";
}

/** Swimlane: work under review sits with the acceptor, everything else with its effective role. */
export function roleBoardLane(card: Pick<WorkspaceRoleBoardCard, "role" | "status">): RoleBoardRole {
  return card.status.trim().toLowerCase() === "in_review" ? "acceptor" : card.role;
}

export type RoleBoardSwimlane = {
  agents: WorkspaceRoleBoardAgent[];
  cells: Record<RoleBoardStage, WorkspaceRoleBoardCard[]>;
  role: RoleBoardRole;
};

export type RoleBoardGrid = {
  lanes: RoleBoardSwimlane[];
  stageCounts: Record<RoleBoardStage, number>;
  unassignedAgents: WorkspaceRoleBoardAgent[];
  userGates: WorkspaceRoleBoardGate[];
};

function emptyCells(): Record<RoleBoardStage, WorkspaceRoleBoardCard[]> {
  return { planned: [], assigned: [], running: [], in_review: [], rework: [], done: [] };
}

export function buildRoleBoardGrid(board: WorkspaceRoleBoard): RoleBoardGrid {
  const lanes = ROLE_BOARD_ROLES.map((role): RoleBoardSwimlane => ({
    agents: board.agents.filter((agent) => agent.role === role),
    cells: emptyCells(),
    role,
  }));
  const stageCounts = Object.fromEntries(ROLE_BOARD_STAGES.map((stage) => [stage, 0])) as Record<RoleBoardStage, number>;
  for (const card of board.cards) {
    const stage = roleBoardStage(card);
    const lane = lanes.find((candidate) => candidate.role === roleBoardLane(card)) ?? lanes[1];
    lane.cells[stage].push(card);
    stageCounts[stage] += 1;
  }
  return {
    lanes,
    stageCounts,
    unassignedAgents: board.agents.filter((agent) => !agent.role),
    // Gates waiting on the user first; plan approvals before plain decisions.
    userGates: [...board.gates].sort((left, right) =>
      Number(left.awaiting !== "awaiting_user") - Number(right.awaiting !== "awaiting_user")
      || Number(left.kind !== "plan_approval") - Number(right.kind !== "plan_approval")),
  };
}

/** Fork G9: the goal's usage summary shown in the role board header. */
export type WorkspaceTurnUsage = {
  acceptedTodos: number;
  agentHours: number;
  budgetRatio: number | null;
  budgetUsd: number | null;
  byRole: Array<{ agentHours: number; costUsd: number; role: string; turns: number }>;
  costEstimatedUsd: number;
  costPerAcceptedTodoUsd: number | null;
  costPerAcceptedTodoWithoutOrchestratorUsd: number | null;
  costUsd: number;
  orchestratorCostUsd: number | null;
  turns: number;
  unpricedTurns: number;
};

/** Structural view of the parsed `turn_usage_summary` projection. */
export type TurnUsageProjectionInput = {
  accepted_todos: number;
  agent_hours: number;
  budget?: { budget_usd: number; spent_ratio: number } | null;
  by_role: Array<{ agent_hours: number; cost_usd: number; role: string; turns: number }>;
  cost_estimated_usd: number;
  cost_per_accepted_todo_usd?: number | null;
  cost_per_accepted_todo_excl_orchestrator_usd?: number | null;
  cost_usd: number;
  orchestrator_cost_usd?: number;
  turns: number;
  unpriced_turns: number;
};

export function turnUsageFromProjection(projection: TurnUsageProjectionInput): WorkspaceTurnUsage {
  return {
    acceptedTodos: projection.accepted_todos,
    agentHours: projection.agent_hours,
    budgetRatio: projection.budget?.spent_ratio ?? null,
    budgetUsd: projection.budget?.budget_usd ?? null,
    byRole: projection.by_role.map((row) => ({
      agentHours: row.agent_hours,
      costUsd: row.cost_usd,
      role: row.role,
      turns: row.turns,
    })),
    costEstimatedUsd: projection.cost_estimated_usd,
    costPerAcceptedTodoUsd: projection.cost_per_accepted_todo_usd ?? null,
    costPerAcceptedTodoWithoutOrchestratorUsd: projection.cost_per_accepted_todo_excl_orchestrator_usd ?? null,
    costUsd: projection.cost_usd,
    orchestratorCostUsd: projection.orchestrator_cost_usd ?? null,
    turns: projection.turns,
    unpricedTurns: projection.unpriced_turns,
  };
}

export function formatUsd(value: number): string {
  return `$${value.toFixed(value >= 100 ? 0 : 2)}`;
}
