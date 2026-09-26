import { useCallback, useEffect, useState } from "react";
import { Send } from "lucide-react";
import { ChatApiError, fetchGateThread, replyToGateThread, type GateThreadView } from "../../data/chat";
import { useWorkspaceI18n } from "./i18n";

const GATE_REPLY_LIMIT = 4000;
const HIDDEN_ERROR_CODES = new Set(["not_a_user_gate", "gate_not_found", "goal_not_registered"]);

/** Discussion thread of a user gate: read the messages and append an owner reply.
 * Replying never closes the gate; approve/reject/cancel stay on gate.resolve. */
export function GateThreadPanel({ goalId, todoId, readOnly }: { goalId: string; todoId: string; readOnly: boolean }) {
  const { t } = useWorkspaceI18n();
  const [view, setView] = useState<GateThreadView | null>(null);
  const [hidden, setHidden] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);

  const load = useCallback(async (signal?: { aborted: boolean }) => {
    try {
      const next = await fetchGateThread(goalId, todoId);
      if (signal?.aborted) return;
      setView(next);
      setLoadError(false);
    } catch (error) {
      if (signal?.aborted) return;
      const code = error instanceof ChatApiError ? String(error.payload.error_code ?? "") : "";
      if (HIDDEN_ERROR_CODES.has(code)) setHidden(true);
      else setLoadError(true);
    }
  }, [goalId, todoId]);

  useEffect(() => {
    const signal = { aborted: false };
    setView(null);
    setHidden(false);
    setDraft("");
    setSendError(null);
    void load(signal);
    return () => { signal.aborted = true; };
  }, [load]);

  async function send() {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    setSendError(null);
    try {
      await replyToGateThread(goalId, todoId, text);
      setDraft("");
      await load();
    } catch (error) {
      setSendError(error instanceof Error ? error.message : String(error));
    } finally {
      setSending(false);
    }
  }

  if (hidden) return null;
  const closed = view?.awaiting === "closed";
  return (
    <section className="personal-detail-card personal-gate-thread" data-testid="gate-thread" aria-label={t("gateThread.title")}>
      <header>
        <strong>{t("gateThread.title")}</strong>
        {view ? <small data-awaiting={view.awaiting}>{t(`gateThread.${view.awaiting}`)}</small> : null}
      </header>
      {view?.kind === "plan_approval" && view.plan_id ? <p className="personal-gate-thread-plan">{t("gateThread.planCard", { planId: view.plan_id })}</p> : null}
      {loadError ? <p className="personal-gate-thread-empty">{t("gateThread.loadError")}</p> : null}
      {view && view.messages.length === 0 ? <p className="personal-gate-thread-empty">{t("gateThread.empty")}</p> : null}
      {view && view.messages.length ? (
        <ol className="personal-gate-thread-messages">
          {view.messages.map((message) => (
            <li className={`is-${message.author}`} key={message.message_id}>
              <small>{message.author === "user" ? t("gateThread.you") : t("gateThread.orchestrator", { agent: message.agent_id ?? "" })} · {message.at}</small>
              <p>{message.text}</p>
            </li>
          ))}
        </ol>
      ) : null}
      {!readOnly && view && !closed ? (
        <div className="personal-gate-thread-reply">
          <textarea
            aria-label={t("gateThread.reply")}
            maxLength={GATE_REPLY_LIMIT}
            onChange={(event) => setDraft(event.target.value.slice(0, GATE_REPLY_LIMIT))}
            placeholder={t("gateThread.replyPlaceholder")}
            rows={3}
            value={draft}
          />
          <button className="personal-secondary-action" disabled={!draft.trim() || sending} onClick={() => void send()} type="button">
            <Send size={15} />{sending ? t("gateThread.sending") : t("gateThread.send")}
          </button>
          {sendError ? <small className="personal-gate-thread-error">{sendError}</small> : null}
        </div>
      ) : null}
    </section>
  );
}
