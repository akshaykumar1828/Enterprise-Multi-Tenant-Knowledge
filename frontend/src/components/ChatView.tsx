import { useEffect, useRef, useState, type FormEvent } from "react";

import { ApiError } from "../api/client";
import { askQuestion } from "../api/endpoints";
import type { QueryResponse } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { AssistantReply } from "./AssistantReply";
import { Icon } from "./ui/Icon";

interface Exchange {
  id: number;
  question: string;
  retrieveOnly: boolean;
  status: "pending" | "done" | "error";
  response?: QueryResponse;
  error?: { message: string; quotaExceeded: boolean };
}

export const EXAMPLE_QUESTIONS = [
  "What home-office stipend do new employees get?",
  "What are the steps for an emergency rollback of the Serving Runtime?",
  "How often must customers be updated during a Sev-1 incident?",
  "How do I get approval to put up large artwork in the office?",
];

function describeError(error: unknown): { message: string; quotaExceeded: boolean } {
  if (error instanceof ApiError) {
    if (error.status === 429) {
      const wait = error.retryAfterSeconds ? ` Try again in about ${Math.ceil(error.retryAfterSeconds)} seconds,` : " Try again later,";
      return {
        message: `The AI answer service has reached its usage limit.${wait} or view the matching sources without an AI answer.`,
        quotaExceeded: true,
      };
    }
    if (error.status === 0) return { message: "We couldn't reach the server. Check your connection and try again.", quotaExceeded: false };
    return { message: error.message, quotaExceeded: false };
  }
  return { message: "Something went wrong. Please try again.", quotaExceeded: false };
}

function Welcome({ firstName, onExample, disabled }: { firstName: string; onExample: (q: string) => void; disabled: boolean }) {
  return (
    <section className="welcome" aria-labelledby="welcome-title">
      <h2 id="welcome-title" className="welcome__title">
        {firstName ? `Hi ${firstName}, what would you like to know?` : "What would you like to know?"}
      </h2>
      <p className="welcome__lead">
        Ask a question in plain language. Answers are written from your organization's documents — only the ones
        you have access to — and every answer lists the sources it used, so you can check them.
      </p>
      <p className="welcome__hint">You can ask about policies, processes, projects, customers and anything else that is written down.</p>
      <div className="examples">
        <p className="examples__label" id="examples-label">Try one of these</p>
        <ul className="examples__list" aria-labelledby="examples-label">
          {EXAMPLE_QUESTIONS.map((example) => (
            <li key={example}>
              <button type="button" className="example" disabled={disabled} onClick={() => onExample(example)}>
                {example}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

export function ChatView() {
  const { token, user } = useAuth();
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [question, setQuestion] = useState("");
  const [sourcesOnly, setSourcesOnly] = useState(false);
  // Bumped on every question: all earlier answers close their open sources.
  const [questionCount, setQuestionCount] = useState(0);
  const nextId = useRef(1);
  const input = useRef<HTMLTextAreaElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const busy = exchanges.some((exchange) => exchange.status === "pending");
  const firstName = (user?.display_name ?? "").split(/\s+/)[0] ?? "";

  useEffect(() => {
    bottom.current?.scrollIntoView?.({ block: "end", behavior: "smooth" });
  }, [exchanges]);

  const update = (id: number, changes: Partial<Exchange>) =>
    setExchanges((current) => current.map((exchange) => (exchange.id === id ? { ...exchange, ...changes } : exchange)));

  async function ask(text: string, retrieveOnly: boolean) {
    if (!token) return;
    const id = nextId.current++;
    setQuestionCount((count) => count + 1);
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

  function newConversation() {
    setExchanges([]);
    input.current?.focus();
  }

  return (
    <div className="chat">
      <div className="chat__scroll">
        <div className="chat__column" aria-live="polite">
          {exchanges.length === 0 ? (
            <Welcome firstName={firstName} disabled={busy} onExample={(example) => void ask(example, sourcesOnly)} />
          ) : (
            <div className="chat__toolbar">
              <button type="button" className="button button--ghost button--small" disabled={busy} onClick={newConversation}>
                <Icon name="plus" size={14} /> New conversation
              </button>
            </div>
          )}
          {exchanges.map((exchange) => (
            <article key={exchange.id} className="exchange" aria-label={`Question: ${exchange.question}`}>
              <div className="exchange__question">
                <p>{exchange.question}</p>
              </div>
              {exchange.status === "pending" && (
                <div className="thinking" role="status">
                  <span className="thinking__dots" aria-hidden="true"><span /><span /><span /></span>
                  {exchange.retrieveOnly ? "Searching documents…" : "Searching documents and writing an answer…"}
                </div>
              )}
              {exchange.status === "done" && exchange.response && (
                <AssistantReply response={exchange.response} resetKey={questionCount} />
              )}
              {exchange.status === "error" && exchange.error && (
                <div className="notice notice--error" role="alert">
                  <Icon name="alert" />
                  <div>
                    <p>{exchange.error.message}</p>
                    <div className="notice__actions">
                      {exchange.error.quotaExceeded ? (
                        <button type="button" className="button button--ghost button--small" disabled={busy}
                                onClick={() => void ask(exchange.question, true)}>
                          Show sources only
                        </button>
                      ) : (
                        <button type="button" className="button button--ghost button--small" disabled={busy}
                                onClick={() => void ask(exchange.question, exchange.retrieveOnly)}>
                          <Icon name="refresh" size={14} /> Try again
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              )}
            </article>
          ))}
          <div ref={bottom} />
        </div>
      </div>

      <form className="composer" onSubmit={onSubmit}>
        <div className="composer__box">
          <label htmlFor="question" className="visually-hidden">Question</label>
          <textarea
            id="question"
            ref={input}
            value={question}
            maxLength={2000}
            rows={2}
            placeholder="Ask a question about your organization's documents…"
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) onSubmit(event);
            }}
          />
          <button type="submit" className="button composer__send" disabled={busy || !question.trim()}>
            {busy ? "Working…" : <><Icon name="send" size={16} /> Ask</>}
          </button>
        </div>
        <div className="composer__meta">
          <label className="toggle">
            <input type="checkbox" checked={sourcesOnly} onChange={(event) => setSourcesOnly(event.target.checked)} />
            Sources only — show matching documents without a written answer
          </label>
          <span className="composer__hint">Enter to send · Shift + Enter for a new line</span>
        </div>
      </form>
    </div>
  );
}
