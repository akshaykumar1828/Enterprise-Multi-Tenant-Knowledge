"""Split a document into overlapping chunks.

Each page is chunked on its own, so a PDF chunk never spans two pages and its
page number is always exact. Markdown files are a single page.

Within a page, the text is first split at Markdown headings so that a chunk
never mixes two unrelated sections. Sections longer than CHUNK_SIZE are then
cut into overlapping windows at word boundaries.

Every chunk's text is an exact slice of its page text, and the chunk records
where that slice starts and ends within the page.
"""

import re
from dataclasses import dataclass

from .loader import Document

# all-MiniLM-L6-v2 reads at most 256 tokens and truncates the rest. 500
# characters is roughly 100-130 tokens, so a chunk always fits with room to
# spare, and a typical handbook section fits in a single chunk.
CHUNK_SIZE = 500
# 100 characters (about one sentence) is repeated between consecutive windows
# of a long section, so a fact on a boundary still appears whole in one chunk.
CHUNK_OVERLAP = 100

HEADING_PATTERN = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)

# Identifies how embedding_input() builds the text that gets embedded. Stored per
# document, so changing the format (and bumping this) makes ingestion re-embed.
EMBEDDING_INPUT_VERSION = "contextual-v1"


@dataclass
class Chunk:
    chunk_id: str
    source: str
    relative_path: str
    source_type: str
    doc_id: str | None
    title: str
    page_number: int | None
    section: str
    text: str
    start_char: int
    end_char: int


def embedding_input(chunk: Chunk) -> str:
    """The text we embed for a chunk: its document context followed by the chunk itself.

    A chunk deep inside a document often never mentions the customer, project,
    or document it belongs to; the header puts that context into its vector.
    Only the embedding sees this header; chunk.text stays the original text.
    """
    header = [f"Title: {chunk.title}", f"Source: {chunk.source_type}"]
    if chunk.section:
        header.append(f"Section: {chunk.section}")
    return "\n".join(header) + "\n\n" + chunk.text


def split_into_sections(text: str) -> list[tuple[str, int, int]]:
    """Return (heading, start, end) for each section, heading line included."""
    headings = list(HEADING_PATTERN.finditer(text))
    if not headings:
        return [("", 0, len(text))]

    sections = []
    # Text before the first heading becomes its own untitled section.
    if headings[0].start() > 0:
        sections.append(("", 0, headings[0].start()))

    for i, match in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        sections.append((match.group(1).strip(), match.start(), end))
    return sections


def window_spans(text: str, start: int, end: int, size: int, overlap: int) -> list[tuple[int, int]]:
    """Cut text[start:end] into windows of at most `size` chars, breaking at whitespace."""
    spans = []
    while start < end:
        window_end = min(start + size, end)
        if window_end < end:
            # Back up to the last space so we don't cut a word in half.
            last_space = text.rfind(" ", start, window_end)
            if last_space > start:
                window_end = last_space
        spans.append((start, window_end))

        if window_end >= end:
            break
        next_start = max(window_end - overlap, start + 1)
        # Move forward to the start of the next word.
        next_space = text.find(" ", next_start, window_end)
        start = next_space + 1 if next_space != -1 else next_start
    return spans


class TooManyChunks(ValueError):
    """The document would produce more chunks than the caller allows."""

    def __init__(self, limit: int):
        super().__init__(f"document produces more than {limit} chunks")
        self.limit = limit


def chunk_document(
    document: Document,
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
    *,
    max_chunks: int | None = None,
) -> list[Chunk]:
    """max_chunks (used for uploads) stops chunking as soon as it is exceeded, raising
    TooManyChunks; without it (folder ingestion) nothing changes."""
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be smaller than chunk_size")

    chunks: list[Chunk] = []
    for page in document.pages:
        text = page.text
        for section, section_start, section_end in split_into_sections(text):
            for start, end in window_spans(text, section_start, section_end, chunk_size, chunk_overlap):
                # Trim surrounding whitespace but keep the offsets pointing at the real text.
                piece = text[start:end]
                start += len(piece) - len(piece.lstrip())
                end -= len(piece) - len(piece.rstrip())
                if start >= end:
                    continue
                if max_chunks is not None and len(chunks) >= max_chunks:
                    raise TooManyChunks(max_chunks)
                chunks.append(
                    Chunk(
                        # relative_path is unique across the corpus; a file name alone is not.
                        chunk_id=f"{document.relative_path}#{len(chunks)}",
                        source=document.source,
                        relative_path=document.relative_path,
                        source_type=document.source_type,
                        doc_id=document.doc_id,
                        title=document.title,
                        page_number=page.page_number,
                        section=section,
                        text=text[start:end],
                        start_char=start,
                        end_char=end,
                    )
                )
    return chunks
