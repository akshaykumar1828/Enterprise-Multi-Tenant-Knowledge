"""Load Markdown, plain-text, or PDF documents from disk."""

import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | {".pdf"}

# Dataset housekeeping files that live next to documents but are not documents.
SKIPPED_FILE_NAMES = {"readme.md", "license", "license.md", "license.txt"}

# EnterpriseRAG-Bench file names start with the benchmark id: dsid_<32 hex>__<slug>.txt
DOC_ID_PATTERN = re.compile(r"^(dsid_[0-9a-f]{32})__")

LOCAL_SOURCE_TYPE = "local"


@dataclass
class Page:
    page_number: int | None  # 1-based for PDFs, None for Markdown/text files
    text: str


@dataclass
class Document:
    source: str  # file name, for display
    relative_path: str  # unique key: path under the documents folder, "/"-separated
    source_type: str  # "local" for top-level files, else the containing folder (slack, jira, ...)
    doc_id: str | None  # benchmark id (dsid_...) when the file name carries one
    title: str  # first line of the document, or the file name
    path: Path
    pages: list[Page]


def title_of(pages: list[Page], fallback: str) -> str:
    """The document's first non-empty line, without Markdown heading marks."""
    for line in pages[0].text.splitlines():
        line = line.lstrip("#").strip()
        if line:
            return line[:200]
    return fallback


def relative_path_of(path: Path, root: Path | None) -> str:
    """Path under `root` with "/" separators; just the file name if outside `root`."""
    if root is not None:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    return path.name


def source_type_of(relative_path: str) -> str:
    parts = relative_path.split("/")
    return parts[-2] if len(parts) > 1 else LOCAL_SOURCE_TYPE


def doc_id_of(file_name: str) -> str | None:
    match = DOC_ID_PATTERN.match(file_name)
    return match.group(1) if match else None


def clean_text(text: str) -> str:
    """Normalize line endings and whitespace without changing the content."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def load_text_pages(path: Path) -> list[Page]:
    text = clean_text(path.read_text(encoding="utf-8"))
    return [Page(page_number=None, text=text)] if text else []


def load_pdf_pages(path: Path) -> list[Page]:
    try:
        reader = PdfReader(path)
    except PdfReadError as error:
        raise ValueError(f"Could not read PDF {path}: {error}") from error

    pages = []
    for page_number, pdf_page in enumerate(reader.pages, start=1):
        text = clean_text(pdf_page.extract_text() or "")
        # Skip blank pages but keep the real page numbers of the others.
        if text:
            pages.append(Page(page_number=page_number, text=text))
    return pages


def load_document(path: str | Path, root: str | Path | None = None) -> Document:
    """Load one file. `root` is the documents folder its relative_path is measured from."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Document not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        pages = load_pdf_pages(path)
    elif suffix in TEXT_SUFFIXES:
        pages = load_text_pages(path)
    else:
        raise ValueError(f"Unsupported document type '{suffix}': {path}")

    if not pages:
        # For PDFs this usually means a scanned document with no text layer.
        raise ValueError(f"No extractable text in document: {path}")

    relative_path = relative_path_of(path, Path(root) if root is not None else None)
    return Document(
        source=path.name,
        relative_path=relative_path,
        source_type=source_type_of(relative_path),
        doc_id=doc_id_of(path.name),
        title=title_of(pages, fallback=path.name),
        path=path,
        pages=pages,
    )


def discover_documents(directory: str | Path) -> list[Path]:
    """Every supported file under `directory` (recursively), in path order."""
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"Documents folder not found: {directory}")

    paths = sorted(
        path
        for path in directory.rglob("*")
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_SUFFIXES
        and path.name.lower() not in SKIPPED_FILE_NAMES
    )
    if not paths:
        raise ValueError(f"No supported documents (.md, .txt, .pdf) found in {directory}")
    return paths


def load_documents(directory: str | Path) -> list[Document]:
    return [load_document(path, root=directory) for path in discover_documents(directory)]
