import { GitBranch } from "lucide-react";

import type { GatePlanCard, GateThreadView } from "../../data/gate-thread";
import { useWorkspaceI18n, type WorkspaceMessageKey } from "./i18n";
import { ROLE_BOARD_GATE_KINDS, ROLE_BOARD_ROLES, formatUsd, type RoleBoardGateKind, type RoleBoardRole } from "./role-board-model";

const COMPLETION_PUSH_KEYS: Record<string, WorkspaceMessageKey> = {
  declined: "gateThread.completePush.declined",
  local_only: "gateThread.completePush.local_only",
  no_merges: "gateThread.completePush.no_merges",
  nothing_to_push: "gateThread.completePush.nothing_to_push",
  pushed: "gateThread.completePush.pushed",
  up_to_date: "gateThread.completePush.up_to_date",
};

function isGateKind(kind: string): kind is RoleBoardGateKind {
  return (ROLE_BOARD_GATE_KINDS as readonly string[]).includes(kind);
}

function useRoleLabel() {
  const { t } = useWorkspaceI18n();
  return (role: string | null | undefined) => role && (ROLE_BOARD_ROLES as readonly string[]).includes(role)
    ? t(`roles.role.${role as RoleBoardRole}`) : role ?? "";
}

function PlanCard({ plan }: { plan: GatePlanCard }) {
  const { t } = useWorkspaceI18n();
  const roleLabel = useRoleLabel();
  return (
    <div className="personal-gate-plan" data-testid="gate-plan-card">
      <p className="personal-gate-detail-lead">
        <strong>{plan.title ?? plan.plan_id}</strong>
        <small>{t("roles.planSummary", { count: plan.todos.length, revision: plan.revision ?? 1 })}</small>
      </p>
      {plan.summary ? <p className="personal-gate-plan-summary">{plan.summary}</p> : null}
      {plan.todos.length ? (
        <ol className="personal-gate-plan-todos">
          {plan.todos.map((todo) => (
            <li data-plan-key={todo.key} key={todo.key}>
              <p><code>{todo.key}</code><span>{todo.text}</span></p>
              <small>
                {todo.required_role ? <span className="personal-role-badge">{roleLabel(todo.required_role)}</span> : null}
                {todo.task_repositories.length ? <span><GitBranch aria-hidden size={11} />{todo.task_repositories.join(", ")}</span> : null}
                {todo.depends_on.length ? <span>{t("gateThread.planDependsOn", { keys: todo.depends_on.join(", ") })}</span> : null}
              </small>
              {todo.acceptance ? <p className="personal-gate-plan-field"><span>{t("gateThread.planAcceptance")}</span>{todo.acceptance}</p> : null}
              {todo.validation_command ? <p className="personal-gate-plan-field"><span>{t("gateThread.planValidation")}</span><code>{todo.validation_command}</code></p> : null}
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}

function CriteriaChanges({ changes }: { changes: NonNullable<GateThreadView["criteria_changes"]> }) {
  const { t } = useWorkspaceI18n();
  return (
    <div className="personal-gate-thread-criteria" data-testid="gate-criteria-changes">
      <p className="personal-gate-thread-plan">{t("gateThread.criteriaChanges")}</p>
      <table>
        <thead>
          <tr>
            <th scope="col">{t("gateThread.criteriaTodo")}</th>
            <th scope="col">{t("gateThread.criteriaOld")}</th>
            <th scope="col">{t("gateThread.criteriaNew")}</th>
            <th scope="col">{t("gateThread.criteriaReason")}</th>
          </tr>
        </thead>
        <tbody>
          {changes.map((change) => (
            <tr data-result={change.result ?? undefined} key={change.todo_id}>
              <td><code>{change.todo_id}</code></td>
              <td data-side="old">{change.old || t("gateThread.criteriaNone")}</td>
              <td data-side="new">{change.new || t("gateThread.criteriaNone")}</td>
              <td>{change.reason ?? ""}{change.result ? ` (${change.result})` : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PushRepos({ view }: { view: GateThreadView }) {
  const { t } = useWorkspaceI18n();
  return (
    <div data-testid="gate-push-repos">
      <p className="personal-gate-thread-plan">{t(view.push_reason === "push_failed" ? "gateThread.pushRetryLead" : "gateThread.pushLead")}</p>
      <ul className="personal-gate-facts-list">
        {(view.push_repos ?? []).map((repo) => (
          <li data-push-repo={repo.name} key={repo.name}>
            <p><strong>{repo.name}</strong>{repo.branch ? <code>{repo.remote ? `${repo.branch} → ${repo.remote}` : repo.branch}</code> : null}</p>
            <small>{repo.status === "ready"
              ? t("gateThread.pushCommits", { count: repo.unpushed_commits ?? repo.log.length, range: repo.commit_range ?? "" })
              : t("gateThread.pushSkipped", { reason: repo.note ?? repo.status })}</small>
            {repo.log.length ? <ol className="personal-gate-commit-log">{repo.log.map((line) => <li key={line}><code>{line}</code></li>)}</ol> : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

function BudgetFacts({ view }: { view: GateThreadView }) {
  const { t } = useWorkspaceI18n();
  const roleLabel = useRoleLabel();
  if (view.budget_usd == null || view.spent_usd == null) return null;
  const ratio = view.spent_ratio ?? (view.budget_usd > 0 ? view.spent_usd / view.budget_usd : null);
  return (
    <dl className="personal-gate-facts" data-testid="gate-budget">
      <div>
        <dt>{t("gateThread.budgetSpent")}</dt>
        <dd>{t("gateThread.budgetSpentValue", { budget: formatUsd(view.budget_usd), percent: ratio == null ? "-" : Math.round(ratio * 100), spent: formatUsd(view.spent_usd) })}</dd>
      </div>
      {view.estimated_usd ? <div><dt>{t("gateThread.budgetEstimated")}</dt><dd>{formatUsd(view.estimated_usd)}</dd></div> : null}
      {view.by_role?.length ? (
        <div>
          <dt>{t("gateThread.budgetByRole")}</dt>
          <dd>{view.by_role.map((row) => t("gateThread.budgetRoleValue", { cost: formatUsd(row.cost_usd), role: roleLabel(row.role), turns: row.turns ?? 0 })).join(" / ")}</dd>
        </div>
      ) : null}
      {view.default_raise_usd ? (
        <div>
          <dt>{t("gateThread.budgetRaise")}</dt>
          <dd>
            {formatUsd(view.default_raise_usd)}
            {/* LoopX refuses a raise that does not cover the spend; the note carries the amount. */}
            {view.default_raise_usd <= view.spent_usd ? <small className="personal-gate-facts-warning" data-budget-raise-short>{t("gateThread.budgetRaiseShort")}</small> : null}
          </dd>
        </div>
      ) : null}
    </dl>
  );
}

function CompletionFacts({ view }: { view: GateThreadView }) {
  const { t } = useWorkspaceI18n();
  const usage = view.completion_usage;
  const todos = view.completion_todos;
  if (!usage && !todos && !view.completion_repos?.length) return null;
  return (
    <dl className="personal-gate-facts" data-testid="gate-goal-complete">
      {todos ? <div><dt>{t("gateThread.completeTodos")}</dt><dd>{t("gateThread.completeTodosValue", todos)}</dd></div> : null}
      {usage ? (
        <div>
          <dt>{t("gateThread.completeUsage")}</dt>
          <dd>{t("gateThread.completeUsageValue", { cost: formatUsd(usage.cost_usd ?? 0), hours: (usage.agent_hours ?? 0).toFixed(1), turns: usage.turns ?? 0 })}</dd>
        </div>
      ) : null}
      {view.completion_repos?.length ? (
        <div>
          <dt>{t("gateThread.completeRepos")}</dt>
          <dd>
            {view.completion_repos.map((repo) => (
              <span data-completion-repo={repo.name} key={repo.name}>{t("gateThread.completeRepoValue", {
                branch: repo.branch ?? "-",
                merges: repo.merged_todo_commits.length,
                name: repo.name,
                push: repo.push && COMPLETION_PUSH_KEYS[repo.push] ? t(COMPLETION_PUSH_KEYS[repo.push]) : repo.push ?? "-",
              })}</span>
            ))}
          </dd>
        </div>
      ) : null}
      {view.follow_ups?.length ? (
        <div><dt>{t("gateThread.completeFollowUps")}</dt><dd>{view.follow_ups.map((item) => <span key={item.todo_id ?? item.text}>{item.text}</span>)}</dd></div>
      ) : null}
    </dl>
  );
}

/**
 * What a gate asks the owner to decide, by kind: the plan card and its criteria
 * changes, the repos a push sends, the budget spend, or the finished goal's
 * summary. A plain decision has nothing beyond its text and thread.
 */
export function GateKindDetails({ view }: { view: GateThreadView | null }) {
  const { t } = useWorkspaceI18n();
  if (!view || view.kind === "decision") return null;
  let body = null;
  if (view.kind === "plan_approval") {
    body = (
      <>
        {view.plan_id ? <p className="personal-gate-thread-plan">{t("gateThread.planCard", { planId: view.plan_id })}</p> : null}
        {view.plan ? <PlanCard plan={view.plan} /> : null}
        {view.criteria_changes?.length ? <CriteriaChanges changes={view.criteria_changes} /> : null}
      </>
    );
  } else if (view.kind === "acceptor_blocked") {
    body = <p className="personal-gate-thread-plan">{t("gateThread.acceptorBlocked", { todoId: view.review_todo_id ?? "" })}</p>;
  } else if (view.kind === "push_request" && view.push_repos?.length) {
    body = <PushRepos view={view} />;
  } else if (view.kind === "budget_exhausted" && view.budget_usd != null && view.spent_usd != null) {
    body = <BudgetFacts view={view} />;
  } else if (view.kind === "goal_complete" && (view.completion_todos || view.completion_usage || view.completion_repos?.length)) {
    body = <CompletionFacts view={view} />;
  }
  if (!body) return null;
  return (
    <section aria-label={isGateKind(view.kind) ? t(`roles.gateKind.${view.kind}`) : view.kind} className="personal-detail-card personal-gate-kind" data-gate-kind={view.kind}>
      <header><strong>{isGateKind(view.kind) ? t(`roles.gateKind.${view.kind}`) : view.kind}</strong></header>
      {body}
    </section>
  );
}
