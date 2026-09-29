// User gate discussion thread (fork slice S6): the `GET /api/chat/gate-thread`
// wire contract (`loopx gate show`). The thread core is strict: without it
// the gate is unreadable. Every kind-specific section is parsed on its own:
// a malformed section becomes `unavailable` (never an empty or zero fact) and
// the rest of the gate stays readable.
// It has no runtime imports besides zod so contract smokes can compile it standalone.
import { z } from "zod";

const optionalText = z.string().nullable().optional();
const optionalNumber = z.number().nullable().optional();

export const gateThreadMessageSchema = z.object({
  seq: z.number().int().positive(),
  message_id: z.string(),
  author: z.enum(["user", "orchestrator"]),
  agent_id: z.string().nullable().optional(),
  text: z.string(),
  at: z.string(),
});

const gateThreadCoreSchema = z.object({
  ok: z.literal(true),
  goal_id: z.string(),
  todo_id: z.string(),
  status: z.string().nullable().optional(),
  kind: z.string(),
  awaiting: z.enum(["awaiting_user", "awaiting_orchestrator", "closed"]),
  messages: z.array(gateThreadMessageSchema),
});

const roleSpendSchema = z.object({ role: z.string(), cost_usd: z.number(), turns: optionalNumber });

// Decision 12: what approving a plan_approval gate applies (`plan_card_view`).
const planCardSchema = z.object({
  plan_id: z.string(),
  status: optionalText,
  revision: optionalNumber,
  title: z.string(),
  summary: optionalText,
  todos: z.array(z.object({
    key: z.string(),
    text: z.string(),
    required_role: optionalText,
    depends_on: z.array(z.string()),
    task_repositories: z.array(z.string()),
    acceptance: optionalText,
    validation_command: optionalText,
  })),
});

// Decision 40: a plan card's acceptance-criteria changes, old and new side by side.
const criteriaChangesSchema = z.array(z.object({
  todo_id: z.string(),
  old: optionalText,
  new: optionalText,
  reason: optionalText,
  result: optionalText,
}));

// G8: the repos a push_request gate would push (`repo_push_plan`).
const pushSchema = z.object({
  push_reason: optionalText,
  push_repos: z.array(z.object({
    name: z.string(),
    status: z.string(),
    branch: optionalText,
    remote: optionalText,
    commit_range: optionalText,
    unpushed_commits: optionalNumber,
    log: z.array(z.string()).optional(),
    note: optionalText,
  })).min(1),
});

// Decision 41: budget_exhausted. LoopX omits zero-valued facts, so optional numbers may be absent.
const budgetSchema = z.object({
  budget_usd: z.number(),
  spent_usd: z.number(),
  spent_ratio: z.number().optional(),
  estimated_usd: z.number().optional(),
  by_role: z.array(roleSpendSchema).optional(),
  default_raise_usd: z.number().optional(),
});

// Decision 42: goal_complete (`goal_completion_snapshot`).
const completionSchema = z.object({
  completion_todos: z.object({ accepted: z.number(), rejects: z.number(), superseded: z.number() }),
  completion_usage: z.object({
    cost_usd: z.number(),
    turns: z.number(),
    agent_hours: z.number(),
  }),
  // A repo whose merge target branch does not exist yet has no head and no merge list.
  completion_repos: z.array(z.object({
    name: z.string(),
    branch: optionalText,
    head: optionalText,
    push: optionalText,
    merged_todo_commits: z.array(z.unknown()).optional(),
  })).optional(),
  follow_ups: z.array(z.object({ todo_id: optionalText, text: z.string() })).optional(),
});

/** A kind-specific part of a gate: shown when well formed, otherwise reported unavailable. */
export type GateSection<T> = { state: "ready"; value: T } | { state: "unavailable" };

export type GatePlanCard = z.infer<typeof planCardSchema>;
export type GateCriteriaChange = z.infer<typeof criteriaChangesSchema>[number];
export type GatePush = z.infer<typeof pushSchema>;
export type GateBudget = z.infer<typeof budgetSchema>;
export type GateCompletion = z.infer<typeof completionSchema>;
export type GateThreadMessage = z.infer<typeof gateThreadMessageSchema>;

export type GateThreadView = z.infer<typeof gateThreadCoreSchema> & {
  sections: {
    plan?: GateSection<GatePlanCard>;
    criteriaChanges?: GateSection<GateCriteriaChange[]>;
    options?: GateSection<string[]>;
    reviewTodoId?: GateSection<string>;
    push?: GateSection<GatePush>;
    budget?: GateSection<GateBudget>;
    completion?: GateSection<GateCompletion>;
  };
};

function section<S extends z.ZodType>(schema: S, raw: unknown, error?: unknown): GateSection<z.infer<S>> | undefined {
  if (error !== undefined && error !== null) return { state: "unavailable" };
  if (raw === undefined || raw === null) return undefined;
  const parsed = schema.safeParse(raw);
  return parsed.success ? { state: "ready", value: parsed.data } : { state: "unavailable" };
}

/** Several top-level keys that form one section: absent when none of them is present. */
function pick(record: Record<string, unknown>, keys: readonly string[]) {
  return keys.some((key) => record[key] !== undefined && record[key] !== null)
    ? Object.fromEntries(keys.map((key) => [key, record[key] ?? undefined]))
    : undefined;
}

/** The section a typed gate is defined by: unavailable (not hidden) when its facts are absent. */
function kindSection<S extends z.ZodType>(kind: string, owner: string, schema: S, raw: unknown) {
  return section(schema, raw, kind === owner && raw === undefined ? "missing" : undefined);
}

/** Parse a gate-thread payload; throws only when the thread core itself is unreadable. */
export function parseGateThreadView(raw: unknown): GateThreadView {
  const core = gateThreadCoreSchema.parse(raw);
  const record = raw as Record<string, unknown>;
  const planApproval = core.kind === "plan_approval";
  // A plan approval without a readable card reports it unavailable instead of hiding it.
  const planError = record.plan_error ?? (planApproval && record.plan === undefined ? "plan_missing" : undefined);
  return {
    ...core,
    sections: {
      plan: planApproval || record.plan !== undefined ? section(planCardSchema, record.plan, planError) : undefined,
      criteriaChanges: section(criteriaChangesSchema, record.criteria_changes, record.criteria_changes_error),
      options: section(z.array(z.string()), record.options),
      reviewTodoId: core.kind === "acceptor_blocked"
        ? section(z.string().min(1), record.review_todo_id, record.review_todo_id === undefined ? "missing" : undefined)
        : undefined,
      push: kindSection(core.kind, "push_request", pushSchema, pick(record, ["push_reason", "push_repos"])),
      budget: kindSection(core.kind, "budget_exhausted", budgetSchema,
        pick(record, ["budget_usd", "spent_usd", "spent_ratio", "estimated_usd", "by_role", "default_raise_usd"])),
      completion: kindSection(core.kind, "goal_complete", completionSchema,
        pick(record, ["completion_repos", "completion_todos", "completion_usage", "follow_ups"])),
    },
  };
}

export const gateThreadReplySchema = z.object({
  ok: z.literal(true),
  todo_id: z.string(),
  awaiting: z.enum(["awaiting_user", "awaiting_orchestrator", "closed"]),
  message: gateThreadMessageSchema,
});

/** What changed between two reads of one thread, as seen by the role board row. */
export function gateThreadSignature(view: Pick<GateThreadView, "awaiting" | "messages" | "status">) {
  return `${view.status ?? ""}|${view.awaiting}|${view.messages.length}`;
}
