import { describe, expect, it } from "vitest";

import { splitCitations } from "./citations";

describe("splitCitations", () => {
  it("splits text and citation markers", () => {
    expect(splitCitations("A [1] and B [2][3].", new Set([1, 2, 3]))).toEqual([
      { kind: "text", text: "A " },
      { kind: "citation", number: 1 },
      { kind: "text", text: " and B " },
      { kind: "citation", number: 2 },
      { kind: "citation", number: 3 },
      { kind: "text", text: "." },
    ]);
  });

  it("never links a number that is not a returned source", () => {
    expect(splitCitations("Real [1], unknown [7].", new Set([1]))).toEqual([
      { kind: "text", text: "Real " },
      { kind: "citation", number: 1 },
      { kind: "text", text: ", unknown [7]." },
    ]);
  });

  it("leaves years and plain text alone", () => {
    expect(splitCitations("Founded [2026].", new Set([1]))).toEqual([{ kind: "text", text: "Founded [2026]." }]);
    expect(splitCitations("No citations here.", new Set([1]))).toEqual([{ kind: "text", text: "No citations here." }]);
  });
});
