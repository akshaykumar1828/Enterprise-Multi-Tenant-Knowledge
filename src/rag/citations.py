"""Validate the inline [n] citations in a generated answer.

The LLM only ever writes citation numbers. What a number refers to is decided
here, from the retrieval results we sent as context, so the model cannot
invent a source, document id, or page.
"""

import re
from dataclasses import dataclass

# [1], [1][2], [1, 2], [Source 1]. At most two digits, so years like [2026]
# are not mistaken for citations.
CITATION_PATTERN = re.compile(r"\[(?:Source\s+)?(\d{1,2}(?:\s*,\s*\d{1,2})*)\]", re.IGNORECASE)


@dataclass
class CheckedAnswer:
    text: str  # answer with citations normalized to [n] and invalid ones removed
    cited: list[int]  # valid source numbers used, in order of first appearance
    invalid: list[int]  # numbers the model used that do not match any sent source


def check_citations(answer: str, source_count: int) -> CheckedAnswer:
    cited: list[int] = []
    invalid: list[int] = []

    def replace(match: re.Match) -> str:
        numbers = [int(n) for n in re.split(r"\s*,\s*", match.group(1))]
        kept = []
        for number in numbers:
            if 1 <= number <= source_count:
                kept.append(number)
                if number not in cited:
                    cited.append(number)
            elif number not in invalid:
                invalid.append(number)
        return "".join(f"[{n}]" for n in kept)

    text = CITATION_PATTERN.sub(replace, answer)
    # Tidy spaces left behind where an invalid citation was removed.
    if invalid:
        text = re.sub(r" {2,}", " ", text)
        text = re.sub(r" +([.,;:])", r"\1", text)
    return CheckedAnswer(text=text.strip(), cited=cited, invalid=invalid)
