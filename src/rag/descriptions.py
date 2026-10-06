"""Short, plain-language document descriptions for the document list.

A description is extracted (never generated) from the document's own opening text: the
title line, headings, metadata sections, e-mail/meeting header fields, greetings and
speaker labels are skipped, and the first one or two meaningful sentences are kept,
capped at MAX_LENGTH. Nothing is invented and no model is called. The text comes from
the document's first stored passages, read with the same access filter as the list
itself, so a description is only ever shown for a document the caller may already read.
"""

import re
from collections.abc import Sequence

MAX_LENGTH = 220
MIN_LENGTH = 25
SHORT_ENOUGH = 90  # a first sentence shorter than this gets the second one added
MAX_OVERLAP = 200  # stored passages overlap (CHUNK_OVERLAP = 100); joined without repeating it

_BENCHMARK_ID = re.compile(r"\bdsid_[0-9a-f]{32}(?:__)?", re.I)
_HEADING = re.compile(r"^(#{1,6}\s*|h[1-6]\.\s*|={3,}|-{3,}$|```)", re.I)
# Header-style metadata lines ("From: …", "Date: …", "Attendees: …"): dropped entirely.
_METADATA = re.compile(
    r"^(from|to|cc|bcc|sent|date|date/time|time|subject|attachments?|duration|start(?: time)?|end(?: time)?|location|"
    r"recording|attendees(?: \([^)]*\))?|participants|organizer|meeting(?: header)?|header|status|owners?|created|"
    r"updated|last (?:updated|reviewed)|labels?|priority|assignee|reporter|reviewers?|author|version|release|"
    r"call date|company|segment|title|ticket|component|severity|environment|channel|tags?|approvers?)\s*[:|]", re.I)
# Headings whose section is metadata, not content ("## Status" + "Published (…)").
_METADATA_SECTION = re.compile(r"^(status|owners?|ownership|approvers?|revision history|change ?log|metadata|"
                               r"document control|version|contacts?|stakeholders|attendees|participants)\b", re.I)
# Section labels whose text is the useful part ("Summary: Quick sync on …"): label removed, text kept.
_LABEL = re.compile(
    r"^(executive summary|issue summary|summary|overview|purpose|doc(?:ument)? purpose|context|motivation|background|"
    r"description|goal|goals|objective|tl;dr|topline|abstract|intro(?:duction)?|problem(?: statement)?|why|"
    r"high-level ask|ask)\s*(?:\([^)]*\))?\s*[:\-–—]\s*", re.I)
_BARE_LABEL = re.compile(r"^[\w ,/&()'.-]{1,60}:$")
_GREETING_LINE = re.compile(r"^(hi|hello|hey|dear|thanks|thank you|good (?:morning|afternoon|evening))\b[^.!?]{0,60}[,!.]?$",
                            re.I)
_GREETING_PREFIX = re.compile(r"^(?:hi|hello|hey|dear)\b(?: (?:team|all|everyone|folks|there|y'all|both))?[\w ]{0,12}[,:!—–-]\s+",
                              re.I)
_PLEASANTRY = re.compile(r"^(thanks|thank you|hope (?:you|this|all)|great (?:speaking|chatting|meeting)|following up|"
                         r"just following up|quick (?:note|follow-up|update)[,:]?$)", re.I)
_TIMESTAMP = re.compile(r"^\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*")
_SPEAKER = re.compile(r"^[\w .'()-]{1,40}?(?:\s\([^)]{1,40}\))?:\s+")
_BULLET = re.compile(r"^(?:[-*•]|\d{1,2}[.)])\s+")
_MARKUP = re.compile(r"(\*\*|__|`|\*(?=\S)|(?<=\S)\*)")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(])")
_CHANNEL = re.compile(r"#?[a-z][\w-]{0,39}", re.I)


def merge_passages(passages: Sequence[str]) -> str:
    """Join consecutive stored passages, dropping the overlap each one repeats from the previous one."""
    merged = ""
    for passage in passages:
        if not merged:
            merged = passage
            continue
        overlap = 0
        for size in range(min(MAX_OVERLAP, len(merged), len(passage)), 9, -1):
            if merged.endswith(passage[:size]):
                overlap = size
                break
        merged += ("" if overlap else "\n") + passage[overlap:]
    return merged


def _is_heading_like(line: str) -> bool:
    """A short line without sentence punctuation, e.g. "Overview" or "Key decisions"."""
    return len(line) <= 40 and len(line.split()) <= 4 and not re.search(r"[.!?,;:]", line)


def _clean_line(line: str, source_type: str) -> str:
    if source_type in ("slack", "fireflies"):
        line = _TIMESTAMP.sub("", line)
        line = _SPEAKER.sub("", line)
    line = _BULLET.sub("", line)
    line = _LABEL.sub("", line)
    line = _GREETING_PREFIX.sub("", line)
    return line.strip()


def describe(text: str | Sequence[str] | None, source_type: str = "") -> str | None:
    """One or two sentences describing a document, from its opening text; None if nothing useful is there."""
    if not text:
        return None
    if not isinstance(text, str):
        text = merge_passages([part for part in text if part])
    lines = [line.strip() for line in text.replace("\\n", "\n").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return None
    title, body = lines[0], lines[1:]
    channel = None
    if source_type == "slack" and _CHANNEL.fullmatch(title) and not _BENCHMARK_ID.search(title):
        channel = title.lstrip("#")

    kept: list[str] = []
    in_metadata_section = False
    for raw in body:
        if raw == title or raw.startswith(("|", ">")) or _METADATA.match(raw):
            continue
        if _HEADING.match(raw):
            in_metadata_section = bool(_METADATA_SECTION.match(_HEADING.sub("", raw).strip()))
            continue
        if _BARE_LABEL.match(raw):
            in_metadata_section = bool(_METADATA_SECTION.match(raw))
            continue
        if _is_heading_like(raw):
            in_metadata_section = bool(_METADATA_SECTION.match(raw))
            continue
        if in_metadata_section or _GREETING_LINE.match(raw):
            continue
        line = _clean_line(raw, source_type)
        if len(line) < 3 or _BARE_LABEL.match(line):
            continue
        kept.append(line)
        if sum(len(part) for part in kept) > 4 * MAX_LENGTH:
            break

    prose = _MARKUP.sub("", _BENCHMARK_ID.sub("", " ".join(kept)))
    prose = re.sub(r"\s+", " ", prose).strip()
    if not prose:
        return None

    sentences = [s for s in _SENTENCE_END.split(prose) if s]
    # Skip a leading pleasantry ("Thanks again for the demo…") when there is more to say.
    while len(sentences) > 1 and _PLEASANTRY.match(sentences[0]):
        sentences.pop(0)
    summary = sentences[0]
    if len(summary) < SHORT_ENOUGH and len(sentences) > 1:
        summary = f"{summary} {sentences[1]}"
    summary = summary[0].upper() + summary[1:]
    if channel:
        summary = f"Discussion in #{channel}: {summary}"
    if len(summary) > MAX_LENGTH:
        cut = summary[: MAX_LENGTH - 1]
        summary = (cut[: cut.rfind(" ")] if " " in cut else cut).rstrip(" ,;:-–—") + "…"
    return summary if len(summary) >= MIN_LENGTH else None
