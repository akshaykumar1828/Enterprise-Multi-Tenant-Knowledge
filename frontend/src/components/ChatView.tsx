import { useRef, useState, type FormEvent } from "react";

import { ApiError } from "../api/client";
import { askQuestion } from "../api/endpoints";
import type { QueryResponse } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { AssistantReply } from "./AssistantReply";

interface Exchange {
  id: number;
  question: string;
  retrieveOnly: boolean;
  status: "pending" | "done" | "error";
  response?: QueryResponse;
  error?: { message: string; quotaExceeded: boolean };
}

function describeError(error: unknown): { message: string; quotaExceeded: boolean } {
  if (error instanceof ApiError) {
    if (error.status === 429) {
      const wait = error.retryAfterSeconds ? ` Try again in about ${Math.ceil(error.retryAfterSeconds)} seconds,` : " Try again later,";
      return {
        message: `The AI answer service has reached its usage limit.${wait} or view the matching sources without an AI answer.`,
        quotaExceeded: true,
      };
    }
    return { message: error.message, quotaExceeded: false };
  }
  return { message: "Something went wrong. Please try again.", quotaExceeded: false };
}

export function ChatView() {
  const { token } = useAuth();
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [question, setQuestion] = useState("");
  const [sourcesOnly, setSourcesOnly] = useState(false);
  const nextId = useRef(1);
  const busy = exchanges.some((exchange) => exchange.status === "pending");

  const update = (id: number, changes: Partial<Exchange>) =>
    setExchanges((current) => current.map((exchange) => (exchange.id === id ? { ...exchange, ...changes } : exchange)));

  async function ask(text: string, retrieveOnly: boolean) {
    if (!token) return;
    const id = nextId.current++;
    setExchanges((current) => [...current, { id, question: text, retrieveOnly, status: "pending" }]);
    try {
      const response = await askQuestion(token, { question: text, retrieve_only: retrieveOnly });
      update(id, { status: "done", response });
    } catch (error) {
      // A 401 is handled globally (back to login); everything else is shown inline.
      update(id, { status: "error", error: describeError(error) });
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    const text = question.trim();
    if (!text || busy) return;
    setQuestion("");
    void ask(text, sourcesOnly);
  }

  return (
    <main className="chat">
      <div className="chat__messages" aria-live="polite">
        {exchanges.length === 0 && (
          <p className="chat__empty">Ask a question about your organization's documents.</p>
        )}
        {exchanges.map((exchange) => (
          <article key={exchange.id} className="exchange">
            <p className="exchange__question">{exchange.question}</p>
            {exchange.status === "pending" && (
              <p className="exchange__loading" role="status">
                {exchange.retrieveOnly ? "Searching documents…" : "Searching documents and writing an answer…"}
              </p>
            )}
            {exchange.status === "done" && exchange.response && <AssistantReply response={exchange.response} />}
            {exchange.status === "error" && exchange.error && (
              <div className="alert alert--error" role="alert">
                <p>{exchange.error.message}</p>
                {exchange.error.quotaExceeded && (
                  <button type="button" className="button button--secondary" disabled={busy}
                          onClick={() => void ask(exchange.question, true)}>
                    Show sources only
                  </button>
                )}
              </div>
            )}
          </article>
        ))}
      </div>

      <form className="chat__form" onSubmit={onSubmit}>
        <label htmlFor="question" className="visually-hidden">Question</label>
        <textarea
          id="question"
          value={question}
          maxLength={2000}
          rows={2}
          placeholder="Ask a question…"
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) onSubmit(event);
          }}
        />
        <div className="chat__actions">
          <label className="checkbox">
            <input type="checkbox" checked={sourcesOnly} onChange={(event) => setSourcesOnly(event.target.checked)} />
            Sources only (no AI answer)
          </label>
          <button type="submit" className="button" disabled={busy || !question.trim()}>
            {busy ? "Working…" : "Ask"}
          </button>
        </div>
      </form>
    </main>
  );
}
