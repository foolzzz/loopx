import { useMemo } from "react";
import { CircleDollarSign, ClipboardCheck, FlagTriangleRight, GitBranch, MessageSquareText, ShieldAlert, ShieldCheck, Upload, type LucideIcon } from "lucide-react";

import type { WorkspaceDrawerSelection, WorkspaceGoal, WorkspaceModel } from "./personal-workspace-model";
import { useWorkspaceI18n } from "./i18n";
import {
  ROLE_BOARD_ROLES,
  ROLE_BOARD_STAGES,
  buildRoleBoardGrid,
  formatUsd,
  roleBoardStage,
  type WorkspaceRoleBoardAgent,
  type WorkspaceRoleBoardCard,
  type WorkspaceRoleBoardGate,
  type RoleBoardGateKind,
  type RoleBoardRole,
  type WorkspaceTurnUsage,
} from "./role-board-model";

const gateKindIcons: Record<RoleBoardGateKind, LucideIcon> = {
  acceptor_blocked: ShieldAlert,
  budget_exhausted: CircleDollarSign,
  decision: MessageSquareText,
  goal_complete: FlagTriangleRight,
  plan_approval: ClipboardCheck,
  push_request: Upload,
};

function AgentChip({ agent }: { agent: WorkspaceRoleBoardAgent }) {
  const { t } = useWorkspaceI18n();
  const activity = t(`roles.activity.${agent.activity}`);
  const detail = [activity, agent.reason, agent.provider].filter(Boolean).join(" · ");
  return (
    <span className={`personal-role-agent-chip is-${agent.activity}`} data-agent-id={agent.agentId} title={detail}>
      <i aria-hidden="true" />
      <strong>{agent.agentId}</strong>
      <small>{activity}</small>
    </span>
  );
}

/** Fork G9: compact spend strip — cost, agent-hours, Turns, per-role split, cost per accepted todo. */
function UsageSummary({ usage }: { usage: WorkspaceTurnUsage }) {
  const { t } = useWorkspaceI18n();
  const cost = formatUsd(usage.costUsd);
  const estimated = usage.costEstimatedUsd > 0 ? t("roles.usage.estimated", { cost: formatUsd(usage.costEstimatedUsd) }) : null;
  const roleLabel = (role: string) => (ROLE_BOARD_ROLES as readonly string[]).includes(role)
    ? t(`roles.role.${role as RoleBoardRole}`) : role;
  // Pilot v1 N5: "accepted" is an accept record; show the figure without orchestrator overhead too.
  const withoutOrchestrator = usage.costPerAcceptedTodoWithoutOrchestratorUsd != null && (usage.orchestratorCostUsd ?? 0) > 0
    ? t("roles.usage.withoutOrchestrator", { cost: formatUsd(usage.costPerAcceptedTodoWithoutOrchestratorUsd) }) : null;
  const roles = usage.byRole.map((row) => `${roleLabel(row.role)} ${formatUsd(row.costUsd)} · ${row.turns}`).join(" / ");
  return (
    <section aria-label={t("roles.usage.title")} className="personal-role-usage" data-role-usage>
      <span data-usage-metric="cost"><strong>{cost}</strong>{estimated ? <small>{estimated}</small> : null}</span>
      <span data-usage-metric="hours"><strong>{usage.agentHours.toFixed(1)}</strong><small>{t("roles.usage.agentHours")}</small></span>
      <span data-usage-metric="turns"><strong>{usage.turns}</strong><small>{t("roles.usage.turns")}</small></span>
      {usage.costPerAcceptedTodoUsd != null ? (
        <span data-usage-metric="per-todo">
          <strong>{formatUsd(usage.costPerAcceptedTodoUsd)}</strong>
          <small>{t("roles.usage.perAcceptedTodo", { count: usage.acceptedTodos })}</small>
          {withoutOrchestrator ? <small data-usage-metric="per-todo-without-orchestrator">{withoutOrchestrator}</small> : null}
        </span>
      ) : null}
      {usage.budgetUsd != null && usage.budgetRatio != null ? (
        <span className={usage.budgetRatio >= 1 ? "is-over" : usage.budgetRatio >= 0.8 ? "is-near" : undefined} data-usage-metric="budget">
          <strong>{Math.round(usage.budgetRatio * 100)}%</strong>
          <small>{t("roles.usage.budget", { budget: formatUsd(usage.budgetUsd) })}</small>
        </span>
      ) : null}
      {roles ? <small className="personal-role-usage-roles" title={roles}>{roles}</small> : null}
    </section>
  );
}

/**
 * Goal Role board tab (fork slice S8): columns are derived stages, rows are
 * role swimlanes with agent activity chips, and open gates / plan approvals
 * that wait on the user sit above the grid. Cards reuse the Todo drawer.
 */
export function GoalRoleBoardView({
  goal,
  onSelect,
  selectedTodoId = null,
  userTodos,
}: {
  goal: WorkspaceGoal;
  onSelect: (selection: WorkspaceDrawerSelection) => void;
  selectedTodoId?: string | null;
  userTodos: WorkspaceModel["userTodos"];
}) {
  const { t } = useWorkspaceI18n();
  const board = goal.roleBoard ?? null;
  const grid = useMemo(() => (board ? buildRoleBoardGrid(board) : null), [board]);

  if (!board || !grid) {
    return (
      <section aria-label={t("header.roles")} className="personal-role-board">
        <p className="personal-task-empty">{t("roles.empty")}</p>
      </section>
    );
  }

  const openGate = (gate: Pick<WorkspaceRoleBoardGate, "todoId" | "text">) => {
    const attention = userTodos.find((todo) => todo.goalId === goal.goalId && todo.todoId === gate.todoId);
    onSelect({
      item: attention ?? { blocking: true, goalId: goal.goalId, goalTitle: goal.title, text: gate.text, todoId: gate.todoId },
      kind: "attention",
    });
  };
  const openCard = (card: WorkspaceRoleBoardCard) => {
    const todo = goal.agentTodos.find((candidate) => candidate.todoId === card.todoId);
    onSelect({
      item: {
        ...(todo ?? { done: roleBoardStage(card) === "done", status: card.status, text: card.text, todoId: card.todoId }),
        claimedBy: todo?.claimedBy ?? card.claimedBy ?? null,
        goalId: goal.goalId,
        goalTitle: goal.title,
        ownerLabel: card.claimedBy ?? null,
      },
      kind: "todo",
    });
  };
  const gateById = new Map(board.gates.map((gate) => [gate.todoId, gate]));

  return (
    <section aria-label={t("header.roles")} className="personal-role-board">
      <header className="personal-task-view-toolbar">
        <div><strong>{t("header.roles")}</strong></div>
        {!board.dispatcherAvailable ? <small className="personal-role-board-note">{t("roles.dispatcherMissing")}</small>
          : !board.dispatcherServing ? <small className="personal-role-board-note">{t("roles.dispatcherOffline")}</small> : null}
      </header>
      {goal.turnUsage ? <UsageSummary usage={goal.turnUsage} /> : null}

      <section aria-label={t("roles.gatesTitle")} className="personal-role-gates">
        <header><strong>{t("roles.gatesTitle")}</strong><span>{grid.userGates.length}</span></header>
        {grid.userGates.length ? (
          <ul>
            {grid.userGates.map((gate) => {
              const KindIcon = gateKindIcons[gate.kind] ?? MessageSquareText;
              return (
                <li key={gate.todoId}>
                  <button
                    className={`personal-role-gate is-${gate.awaiting}`}
                    data-gate-kind={gate.kind}
                    data-todo-id={gate.todoId}
                    onClick={() => openGate(gate)}
                    type="button"
                  >
                    <KindIcon aria-hidden size={16} />
                    <span>
                      <strong>{gate.kind === "plan_approval" && gate.planTitle ? gate.planTitle : gate.text}</strong>
                      <small>
                        <span className="personal-role-badge">{t(`roles.gateKind.${gate.kind}`)}</span>
                        <span className={`personal-role-badge is-${gate.awaiting}`}>{t(gate.awaiting === "awaiting_user" ? "roles.gateAwaitingUser" : "roles.gateAwaitingOrchestrator")}</span>
                        {gate.kind === "plan_approval" && gate.planTodoCount != null
                          ? <span>{t("roles.planSummary", { count: gate.planTodoCount, revision: gate.planRevision ?? 1 })}</span>
                          : null}
                        {gate.messageCount ? <span>{t("roles.messages", { count: gate.messageCount })}</span> : null}
                      </small>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        ) : <p className="personal-task-empty">{t("roles.noGates")}</p>}
      </section>

      <div className="personal-role-grid-scroll">
        <div className="personal-role-grid" role="table" aria-label={t("roles.gridLabel")}>
          <div className="personal-role-grid-row is-header" role="row">
            <span role="columnheader">{t("roles.laneHeader")}</span>
            {ROLE_BOARD_STAGES.map((stage) => (
              <span className={`personal-role-stage is-${stage}`} data-stage={stage} key={stage} role="columnheader">
                {t(`roles.stage.${stage}`)}<b>{grid.stageCounts[stage]}</b>
              </span>
            ))}
          </div>
          {grid.lanes.map((lane) => (
            <div className="personal-role-grid-row" data-role-lane={lane.role} key={lane.role} role="row">
              <div className="personal-role-lane-head" role="rowheader">
                <strong>{t(`roles.role.${lane.role}`)}</strong>
                <div className="personal-role-agent-list">
                  {lane.agents.length ? lane.agents.map((agent) => <AgentChip agent={agent} key={agent.agentId} />)
                    : <small>{t("roles.noAgents")}</small>}
                </div>
              </div>
              {ROLE_BOARD_STAGES.map((stage) => (
                <div className="personal-role-cell" data-stage={stage} key={stage} role="cell">
                  {lane.cells[stage].map((card) => {
                    const planGate = card.planGateTodoId ? gateById.get(card.planGateTodoId) : undefined;
                    const criteriaGate = card.criteriaChangeGateTodoId ? gateById.get(card.criteriaChangeGateTodoId) : undefined;
                    return (
                      <article
                        className={`personal-role-card is-${stage}${selectedTodoId === card.todoId ? " is-selected" : ""}`}
                        data-todo-id={card.todoId}
                        key={card.todoId}
                      >
                        <button aria-pressed={selectedTodoId === card.todoId} className="personal-role-card-main" onClick={() => openCard(card)} type="button">
                          <strong>{card.text}</strong>
                          <small>
                            {card.priority ? <span className={`personal-priority-badge is-${card.priority.toLowerCase()}`}>{card.priority}</span> : null}
                            {card.rejectCount > 0 ? <span className="personal-role-badge is-rejected" data-reject-count={card.rejectCount}>{t("roles.rejected", { count: card.rejectCount })}</span> : null}
                            {["blocked", "deferred"].includes(card.status) ? <span className="personal-role-badge">{card.status}</span> : null}
                            <span className="personal-role-card-agent">{card.runningAgentId ?? card.claimedBy ?? t("roles.unassigned")}</span>
                          </small>
                          {card.repositories.length ? (
                            <small className="personal-role-card-repos"><GitBranch aria-hidden size={12} />{card.repositories.join(", ")}</small>
                          ) : null}
                          <small className="personal-role-card-acceptance">
                            <ShieldCheck aria-hidden size={12} />
                            {card.requiresAcceptance
                              ? card.acceptorAgent ? t("roles.acceptor", { agent: card.acceptorAgent }) : t("roles.acceptanceRequired")
                              : t("roles.noAcceptance")}
                          </small>
                          {card.acceptanceCriteria ? (
                            <small className="personal-role-card-criteria" title={card.acceptanceCriteria}>
                              {t("roles.criteria", { criteria: card.acceptanceCriteria })}
                            </small>
                          ) : null}
                          {card.dependencyWait ? (
                            <small className="personal-role-card-criteria" title={card.dependencyWait}>
                              {t("roles.dependencyWait", { reason: card.dependencyWait })}
                            </small>
                          ) : null}
                        </button>
                        {card.planId ? (
                          planGate ? (
                            <button className="personal-role-card-link" onClick={() => openGate(planGate)} type="button">
                              <ClipboardCheck aria-hidden size={12} />{t("roles.plan", { plan: card.planId })}
                            </button>
                          ) : <small className="personal-role-card-link"><ClipboardCheck aria-hidden size={12} />{t("roles.plan", { plan: card.planId })}</small>
                        ) : null}
                        {card.criteriaChangePlanId ? (
                          criteriaGate ? (
                            <button className="personal-role-card-link is-criteria-change" data-criteria-change-plan={card.criteriaChangePlanId} onClick={() => openGate(criteriaGate)} type="button">
                              <ClipboardCheck aria-hidden size={12} />{t("roles.criteriaChangePending", { plan: card.criteriaChangePlanId })}
                            </button>
                          ) : <small className="personal-role-card-link is-criteria-change" data-criteria-change-plan={card.criteriaChangePlanId}><ClipboardCheck aria-hidden size={12} />{t("roles.criteriaChangePending", { plan: card.criteriaChangePlanId })}</small>
                        ) : null}
                      </article>
                    );
                  })}
                </div>
              ))}
            </div>
          ))}
        </div>
      </div>
      {grid.unassignedAgents.length ? (
        <footer className="personal-role-agent-list">
          <small>{t("roles.noRole")}</small>
          {grid.unassignedAgents.map((agent) => <AgentChip agent={agent} key={agent.agentId} />)}
        </footer>
      ) : null}
      {board.omittedCardCount ? <p className="personal-task-empty">{t("roles.omitted", { count: board.omittedCardCount })}</p> : null}
    </section>
  );
}
