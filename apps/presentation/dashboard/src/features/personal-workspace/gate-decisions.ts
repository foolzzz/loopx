// Decisions on the typed user gates LoopX opens (G12, decisions 41 and 42).
// Each option implies its decision (`loopx gate resolve --option`); the drawer
// sends the option explicitly, so no button relies on the backend's default.
// It has no runtime imports so contract smokes can compile it standalone.

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
