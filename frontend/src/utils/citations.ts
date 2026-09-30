// Split an answer into plain text and [n] citation markers.
//
// The backend has already validated citations against the retrieved sources,
// so the answer only contains numbers that exist. As a second safeguard a
// marker is only linked when its number is one of `validNumbers`; anything
// else stays plain text and can never point at a source that was not returned.

export type AnswerSegment = { kind: "text"; text: string } | { kind: "citation"; number: number };

const CITATION = /\[(\d{1,2})\]/g;

export function splitCitations(answer: string, validNumbers: ReadonlySet<number>): AnswerSegment[] {
  const segments: AnswerSegment[] = [];
  let last = 0;
  for (const match of answer.matchAll(CITATION)) {
    const number = Number(match[1]);
    if (!validNumbers.has(number)) continue;
    const start = match.index ?? 0;
    if (start > last) segments.push({ kind: "text", text: answer.slice(last, start) });
    segments.push({ kind: "citation", number });
    last = start + match[0].length;
  }
  if (last < answer.length) segments.push({ kind: "text", text: answer.slice(last) });
  return segments;
}
