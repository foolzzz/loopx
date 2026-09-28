// User gate discussion thread (fork slice S6): the `GET /api/chat/gate-thread`
// wire contract (`loopx gate show`). The kind-specific fields of gates LoopX
// opens are optional and degrade to `undefined` when malformed, so a drifted
// backend hides one section instead of blanking the thread.
// It has no runtime imports besides zod so contract smokes can compile it standalone.
import { z } from "zod";

const optionalNumber = z.number().nullable().optional().catch(undefined);
const optionalText = z.string().nullable().optional().catch(undefined);

export const gateThreadMessageSchema = z.object({
  seq: z.number().int().positive(),
  message_id: z.string(),
  author: z.enum(["user", "orchestrator"]),
  agent_id: z.string().nullable().optional(),
  text: z.string(),
  at: z.string(),
});

const gateRoleSpendSchema = z.object({
  role: z.string(),
  cost_usd: z.number().catch(0),
  turns: optionalNumber,
});

// Decision 12: what approving a plan_approval gate applies (`plan_card_view`).
const gatePlanCardSchema = z.object({
  plan_id: z.string(),
  status: optionalText,
  revision: optionalNumber,
  title: optionalText,
  summary: optionalText,
  todos: z.array(z.object({
    key: z.string(),
    text: z.string(),
    required_role: optionalText,
    depends_on: z.array(z.string()).catch([]),
    task_repositories: z.array(z.string()).catch([]),
    acceptance: optionalText,
    validation_command: optionalText,
  })).catch([]),
});

// G8: one repo a push_request gate would push (`repo_push_plan`).
const gatePushRepoSchema = z.object({
  name: z.string(),
  status: z.string().catch("unknown"),
  branch: optionalText,
  remote: optionalText,
  commit_range: optionalText,
  unpushed_commits: optionalNumber,
  log: z.array(z.string()).catch([]),
  note: optionalText,
});

// Decision 42: one repo of a finished goal (`goal_completion_snapshot`).
const gateCompletionRepoSchema = z.object({
  name: z.string(),
  branch: optionalText,
  remote: optionalText,
  push: optionalText,
  merged_todo_commits: z.array(z.unknown()).catch([]),
});

export const gateThreadViewSchema = z.object({
  ok: z.literal(true),
  goal_id: z.string(),
  todo_id: z.string(),
  status: z.string().nullable().optional(),
  kind: z.string(),
  awaiting: z.enum(["awaiting_user", "awaiting_orchestrator", "closed"]),
  plan_id: z.string().optional(),
  plan: gatePlanCardSchema.optional().catch(undefined),
  // G12: an acceptor_blocked gate names the review todo and its resolution options.
  review_todo_id: z.string().optional(),
  options: z.array(z.string()).optional(),
  // Decision 40: a plan card's acceptance-criteria changes, old and new side by side.
  criteria_changes: z.array(z.object({
    todo_id: z.string(),
    old: z.string().nullable().optional(),
    new: z.string().nullable().optional(),
    reason: z.string().nullable().optional(),
    result: z.string().nullable().optional(),
  })).optional(),
  // G8: push_request.
  push_reason: optionalText,
  push_repos: z.array(gatePushRepoSchema).optional().catch(undefined),
  // Decision 41: budget_exhausted.
  budget_usd: optionalNumber,
  spent_usd: optionalNumber,
  spent_ratio: optionalNumber,
  estimated_usd: optionalNumber,
  by_role: z.array(gateRoleSpendSchema).optional().catch(undefined),
  default_raise_usd: optionalNumber,
  // Decision 42: goal_complete.
  completion_repos: z.array(gateCompletionRepoSchema).optional().catch(undefined),
  completion_todos: z.object({
    accepted: z.number().catch(0),
    rejects: z.number().catch(0),
    superseded: z.number().catch(0),
  }).optional().catch(undefined),
  completion_usage: z.object({
    cost_usd: optionalNumber,
    cost_estimated_usd: optionalNumber,
    turns: optionalNumber,
    agent_hours: optionalNumber,
    cost_per_accepted_todo_usd: optionalNumber,
  }).optional().catch(undefined),
  follow_ups: z.array(z.object({ todo_id: optionalText, text: z.string().catch("") })).optional().catch(undefined),
  messages: z.array(gateThreadMessageSchema),
});

export const gateThreadReplySchema = z.object({
  ok: z.literal(true),
  todo_id: z.string(),
  awaiting: z.enum(["awaiting_user", "awaiting_orchestrator", "closed"]),
  message: gateThreadMessageSchema,
});

export type GateThreadMessage = z.infer<typeof gateThreadMessageSchema>;
export type GateThreadView = z.infer<typeof gateThreadViewSchema>;
export type GatePlanCard = z.infer<typeof gatePlanCardSchema>;

/** What changed between two reads of one thread, as seen by the role board row. */
export function gateThreadSignature(view: Pick<GateThreadView, "awaiting" | "messages" | "status">) {
  return `${view.status ?? ""}|${view.awaiting}|${view.messages.length}`;
}
