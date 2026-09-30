import { useState } from "react";

import type { QueryResponse } from "../api/types";
import { AnswerText } from "./AnswerText";
import { SourceDetails } from "./SourceDetails";

/** One answer: text with clickable citations, the source list, and the selected source's details. */
export function AssistantReply({ response }: { response: QueryResponse }) {
  const cited = new Set(response.citations.map((citation) => citation.number));
  const [selected, setSelected] = useState<number | null>(null);
  const selectedSource = response.sources.find((source) => source.number === selected) ?? null;
  const toggle = (number: number) => setSelected((current) => (current === number ? null : number));

  return (
    <div className="reply">
      {response.answer === null ? (
        <p className="reply__note">Sources only — no AI answer was generated.</p>
      ) : (
        <AnswerText
          answer={response.answer}
          sourceNumbers={response.sources.map((source) => source.number)}
          selected={selected}
          onSelect={toggle}
        />
      )}

      {response.sources.length > 0 && (
        <div className="sources">
          <h3 className="sources__title">Sources</h3>
          <ol className="sources__list">
            {response.sources.map((source) => (
              <li key={source.number}>
                <button
                  type="button"
                  className={`source-chip${cited.has(source.number) ? " source-chip--cited" : ""}${
                    selected === source.number ? " source-chip--selected" : ""
                  }`}
                  aria-pressed={selected === source.number}
                  onClick={() => toggle(source.number)}
                >
                  [{source.number}] {source.source}
                  {source.page_number !== null && ` · p. ${source.page_number}`}
                </button>
              </li>
            ))}
          </ol>
          {selectedSource && <SourceDetails source={selectedSource} />}
        </div>
      )}
    </div>
  );
}
