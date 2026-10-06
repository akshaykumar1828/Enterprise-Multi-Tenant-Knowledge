import { useEffect, useId, useRef, useState } from "react";

import type { QueryResponse } from "../api/types";
import { documentTitle, documentTypeLabel } from "../utils/documents";
import { AnswerText } from "./AnswerText";
import { SourceDetails } from "./SourceDetails";
import { Icon } from "./ui/Icon";

/** "Source 2: Q3 pricing review, PDF document, page 3, cited" — the visible text, read in order. */
function sourceLabel(number: number, file: string, type: string, page: number | null, cited: boolean): string {
  return [`Source ${number}: ${documentTitle(file)}`, documentTypeLabel(type, file),
          page !== null ? `page ${page}` : null, cited ? "cited" : null].filter(Boolean).join(", ");
}

/**
 * One answer: text with clickable citations, then the list of sources it drew on.
 *
 * The source LIST starts collapsed behind a "Show sources" button (except in sources-only mode,
 * where the sources are the whole reply). Every source card starts CLOSED. A source opens only
 * when the user clicks it (its card or a [n] citation, which also reveals the list) and closes
 * again on a second click or its Close button. `resetKey` changes whenever a new question is
 * asked, which collapses everything again.
 */
export function AssistantReply({ response, resetKey = 0 }: { response: QueryResponse; resetKey?: number }) {
  const cited = new Set(response.citations.map((citation) => citation.number));
  const sourcesOnly = response.answer === null;
  const [selected, setSelected] = useState<number | null>(null);
  const [sourcesOpen, setSourcesOpen] = useState(sourcesOnly);
  const listId = useId();
  const headers = useRef(new Map<number, HTMLButtonElement | null>());
  const toggle = (number: number) => {
    setSourcesOpen(true);
    setSelected((current) => (current === number ? null : number));
  };
  const close = (number: number) => {
    setSelected(null);
    headers.current.get(number)?.focus(); // back to the card that was opened
  };
  const firstRender = useRef(true);
  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    setSelected(null);
    if (!sourcesOnly) setSourcesOpen(false);
  }, [resetKey, sourcesOnly]);
  const count = response.sources.length;

  return (
    <div className="reply">
      <div className="reply__label"><Icon name="sparkle" size={14} /> Answer</div>
      {response.answer === null ? (
        <p className="reply__note">Sources only — no AI answer was generated. These are the passages that best match your question.</p>
      ) : (
        <AnswerText
          answer={response.answer}
          sourceNumbers={response.sources.map((source) => source.number)}
          selected={selected}
          onSelect={toggle}
        />
      )}

      {count === 0 && (
        <p className="reply__note">No matching documents were found among the documents you can access.</p>
      )}
      {count > 0 && (
        <section className="sources" aria-label="Sources">
          <button type="button" className="sources__toggle" aria-expanded={sourcesOpen} aria-controls={listId}
                  onClick={() => setSourcesOpen((open) => !open)}>
            <span className="sources__chevron" aria-hidden="true"><Icon name="chevron" size={14} /></span>
            {sourcesOpen ? "Hide sources" : "Show sources"} <span className="sources__count">{count}</span>
          </button>
          {sourcesOpen && (
            <ol id={listId} className="sources__list">
              {response.sources.map((source) => {
                const open = selected === source.number;
                return (
                  <li key={source.number} className={`source${open ? " source--open" : ""}`}>
                    <button type="button" className="source__header" aria-expanded={open}
                            ref={(element) => { headers.current.set(source.number, element); }}
                            aria-label={sourceLabel(source.number, source.source, source.source_type, source.page_number,
                                                    cited.has(source.number))}
                            onClick={() => toggle(source.number)}>
                      <span className="source__number" aria-hidden="true">{source.number}</span>
                      <span className="source__title">
                        {documentTitle(source.source)}
                        <span className="source__meta">
                          {documentTypeLabel(source.source_type, source.source)}
                          {source.page_number !== null && ` · page ${source.page_number}`}
                          {cited.has(source.number) && <span className="source__cited">Cited</span>}
                        </span>
                      </span>
                      <span className="source__chevron" aria-hidden="true"><Icon name="chevron" size={14} /></span>
                    </button>
                    {open && (
                      <SourceDetails source={source} cited={cited.has(source.number)} onClose={() => close(source.number)} />
                    )}
                  </li>
                );
              })}
            </ol>
          )}
        </section>
      )}
    </div>
  );
}
