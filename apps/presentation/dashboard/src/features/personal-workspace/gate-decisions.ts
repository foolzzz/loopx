// Decisions on the typed user gates LoopX opens (G12, decisions 41 and 42).
// Each option implies its decision (`loopx gate resolve --option`); the drawer
// sends the option explicitly, so no button relies on the backend's default.
// It has no runtime imports so contract smokes can compile it standalone.
import type { GateSection, GateThreadView } from "../../data/gate-thread";

export type GateResolution = "approve" | "reject" | "cancel";

/** Localized option labels; rendering them with `t()` checks each against the i18n catalog. */
export type GateOptionLabelKey =
  | "drawer.reviewOption.retryAcceptance" | "drawer.reviewOption.acceptManually"
  | "drawer.reviewOption.returnToDeveloper" | "drawer.reviewOption.cancelTodo"
  | "drawer.budgetOption.raiseBudget" | "drawer.budgetOption.continueWithoutLimit" | "drawer.budgetOption.stopGoal"
  | "drawer.goalCompleteOption.closeGoal" | "drawer.goalCompleteOption.addWork" | "drawer.goalCompleteOption.leaveOpen";

export type GateOptionChoice = {
  key: GateOptionLabelKey;
  option: string;
  /** The option the primary approve button selects (the backend's approve default). */
  primary?: true;
  resolution: GateResolution;
};

const acceptorBlockedOptions = [
  { key: "drawer.reviewOption.retryAcceptance", option: "retry_acceptance", primary: true, resolution: "approve" },
  { key: "drawer.reviewOption.acceptManually", option: "accept_manually", resolution: "approve" },
  { key: "drawer.reviewOption.returnToDeveloper", option: "return_to_developer", resolution: "reject" },
  { key: "drawer.reviewOption.cancelTodo", option: "cancel_todo", resolution: "cancel" },
] as const satisfies readonly GateOptionChoice[];
// The note carries a raised amount (default +50%).
const budgetExhaustedOptions = [
  { key: "drawer.budgetOption.raiseBudget", option: "raise_budget", primary: true, resolution: "approve" },
  { key: "drawer.budgetOption.continueWithoutLimit", option: "continue_without_limit", resolution: "approve" },
  { key: "drawer.budgetOption.stopGoal", option: "stop_goal", resolution: "reject" },
] as const satisfies readonly GateOptionChoice[];
// Add work: the note is the follow-up.
const goalCompleteOptions = [
  { key: "drawer.goalCompleteOption.closeGoal", option: "close_goal", primary: true, resolution: "approve" },
  { key: "drawer.goalCompleteOption.addWork", option: "add_work", resolution: "reject" },
  { key: "drawer.goalCompleteOption.leaveOpen", option: "leave_open", resolution: "cancel" },
] as const satisfies readonly GateOptionChoice[];

const optionChoicesByKind: Record<string, readonly GateOptionChoice[]> = {
  acceptor_blocked: acceptorBlockedOptions,
  budget_exhausted: budgetExhaustedOptions,
  goal_complete: goalCompleteOptions,
};

/** The options of a typed gate, or null for a plain decision, plan approval or push request. */
export function gateOptionChoices(kind: string | null | undefined): readonly GateOptionChoice[] | null {
  return kind ? optionChoicesByKind[kind] ?? null : null;
}

/** The option the drawer's primary approve button selects on a typed gate. */
export function primaryGateOption(kind: string | null | undefined): GateOptionChoice | null {
  return gateOptionChoices(kind)?.find((choice) => choice.primary) ?? null;
}

export type OfferedGateChoice = GateOptionChoice & { offered: boolean };

/**
 * A typed gate's named options as the gate itself lists them: a choice is
 * offered only when the gate's `options` list is readable and names it. An
 * unreadable or missing list offers none, the primary included. Null for a
 * gate without named options (plain decision, plan approval, push request).
 */
export function typedGateChoices(
  kind: string | null | undefined,
  options: GateSection<string[]> | undefined,
): { choices: OfferedGateChoice[]; optionsReadable: boolean; primary: OfferedGateChoice | null } | null {
  const choices = gateOptionChoices(kind);
  if (!choices) return null;
  const listed = options?.state === "ready" ? new Set(options.value) : null;
  const marked = choices.map((choice) => ({ ...choice, offered: Boolean(listed?.has(choice.option)) }));
  return {
    choices: listed ? marked.filter((choice) => choice.offered) : marked,
    optionsReadable: listed !== null,
    primary: marked.find((choice) => choice.primary) ?? null,
  };
}

/** What the drawer's thread panel last learned about one gate (goal + todo identity). */
export type GateThreadState =
  | { goalId: string; todoId: string; status: "ready" | "closed"; view: GateThreadView }
  // A read after a successful one failed: the last view stays shown, nothing is decidable until a read succeeds.
  | { goalId: string; todoId: string; status: "retrying"; view: GateThreadView }
  | { goalId: string; todoId: string; status: "unavailable" | "not_gate" };

/**
 * Whether the drawer may offer decisions on the gate it shows now. Only a
 * successful read of this exact gate that is still open is actionable; a read
 * of another gate, no read yet, a failed read and a closed gate are not.
 */
export type GateDecisionAccess = {
  actionable: boolean;
  reason: "pending" | "ready" | "closed" | "retrying" | "unavailable" | "not_gate";
  view: GateThreadView | null;
};

export function gateDecisionAccess(
  state: GateThreadState | null,
  gate: { goalId: string; todoId: string } | null,
): GateDecisionAccess {
  if (!state || !gate || state.goalId !== gate.goalId || state.todoId !== gate.todoId) {
    return { actionable: false, reason: "pending", view: null };
  }
  if (state.status === "ready" || state.status === "closed" || state.status === "retrying") {
    return { actionable: state.status === "ready", reason: state.status, view: state.view };
  }
  return { actionable: false, reason: state.status, view: null };
}
