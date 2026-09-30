import { useMemo } from "react";

import { splitCitations } from "../utils/citations";

interface Props {
  answer: string;
  sourceNumbers: number[];
  selected: number | null;
  onSelect: (number: number) => void;
}

/** The answer text with each valid [n] rendered as a button that selects source n. */
export function AnswerText({ answer, sourceNumbers, selected, onSelect }: Props) {
  const segments = useMemo(() => splitCitations(answer, new Set(sourceNumbers)), [answer, sourceNumbers]);
  return (
    <p className="answer-text">
      {segments.map((segment, index) =>
        segment.kind === "text" ? (
          <span key={index}>{segment.text}</span>
        ) : (
          <button
            key={index}
            type="button"
            className={`citation${selected === segment.number ? " citation--selected" : ""}`}
            aria-label={`Show source ${segment.number}`}
            aria-pressed={selected === segment.number}
            onClick={() => onSelect(segment.number)}
          >
            [{segment.number}]
          </button>
        ),
      )}
    </p>
  );
}
