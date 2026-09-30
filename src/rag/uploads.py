"""Tenant-scoped document uploads: validate, store, ingest, list, delete.

Storage: <upload root>/<tenant_id>/<random uuid><ext>. The user's filename is
never used in a filesystem path; a sanitized copy is kept only as the display
name in the database. The upload root lives outside data/documents/, so the
folder ingestion for the default tenant never picks uploads up.

Ingestion reuses the existing pipeline (load -> chunk -> embed -> one
transaction in store_document). If anything fails, the stored file is removed
and the transaction rolls back, so a failed upload leaves nothing behind and
never touches existing documents.

Every function here takes a trusted tenant_id or AccessScope (from the
authenticated user) and scopes every query by it. Listing and deleting use the
AccessScope, with the same SQL filter as retrieval (access.py).
"""

import hashlib
import logging
import os
import re
import threading
import unicodedata
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import psycopg
from sentence_transformers import SentenceTransformer

from .access import DOCUMENT_ACCESS_FILTER, AccessScope
from .chunking import CHUNK_OVERLAP, CHUNK_SIZE, chunk_document
from .ingest import store_document
from .loader import load_document

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md", ".markdown"}
DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MiB
UPLOAD_ORIGIN = "upload"
UPLOAD_SOURCE_TYPE = "upload"
MAX_FILENAME_LENGTH = 120

log = logging.getLogger("rag.uploads")


class UploadError(Exception):
    """A rejected upload or document operation, with the HTTP status the API should use."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def max_upload_bytes() -> int:
    return int(os.environ.get("UPLOAD_MAX_BYTES", DEFAULT_MAX_UPLOAD_BYTES))


# Per-tenant limits on *uploaded* documents (origin = 'upload'). Folder-managed
# documents are operator-owned, cannot be deleted through the API, and are not counted.
DEFAULT_TENANT_MAX_DOCUMENTS = 500
DEFAULT_TENANT_MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GiB
# Namespace for pg_advisory_xact_lock(namespace, tenant_id): serializes one tenant's
# upload commits so two concurrent uploads cannot both slip under a limit.
QUOTA_LOCK_NAMESPACE = 7301


def _non_negative_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, default))
    if value < 0:
        raise ValueError(f"{name} must be 0 (unlimited) or a positive number")
    return value


def tenant_max_documents() -> int:
    """0 means unlimited."""
    return _non_negative_int("TENANT_MAX_DOCUMENTS", DEFAULT_TENANT_MAX_DOCUMENTS)


def tenant_max_upload_bytes() -> int:
    """0 means unlimited."""
    return _non_negative_int("TENANT_MAX_UPLOAD_BYTES", DEFAULT_TENANT_MAX_UPLOAD_BYTES)


def tenant_upload_usage(conn: psycopg.Connection, tenant_id: int) -> tuple[int, int]:
    """(uploaded document count, uploaded bytes) for one tenant."""
    return conn.execute(
        "SELECT count(*), coalesce(sum(size_bytes), 0) FROM documents WHERE tenant_id = %s AND origin = %s",
        (tenant_id, UPLOAD_ORIGIN),
    ).fetchone()


def check_upload_allowed(conn: psycopg.Connection, scope: AccessScope, content_hash: str, size: int) -> None:
    """Duplicate and tenant-limit checks for one new upload of `size` bytes by `scope`'s user.

    Only duplicates the user may read count (the same filter as retrieval and
    listing): an identical document they cannot read behaves exactly as if it did
    not exist, so its existence and id never reach them.

    The limits are tenant-wide and count every upload. Admins may read every
    document, so they get the figures; employees get the same errors without any
    numbers, which would otherwise describe documents they may not see.
    """
    duplicate = conn.execute(
        f"""SELECT d.id FROM documents d
            WHERE {DOCUMENT_ACCESS_FILTER} AND d.origin = %(origin)s AND d.content_hash = %(content_hash)s
            ORDER BY d.id LIMIT 1""",
        {**scope.sql_params(), "origin": UPLOAD_ORIGIN, "content_hash": content_hash},
    ).fetchone()
    if duplicate:
        raise UploadError(409, "duplicate_document", f"This file was already uploaded (document {duplicate[0]}).")

    count, used = tenant_upload_usage(conn, scope.tenant_id)
    max_documents, max_bytes = tenant_max_documents(), tenant_max_upload_bytes()
    if max_documents and count + 1 > max_documents:
        if scope.is_admin:
            message = (f"Your organization has reached its limit of {max_documents} uploaded documents. "
                       "Delete a document to upload another.")
        else:
            message = "Your organization has reached its limit of uploaded documents. Please contact an administrator."
        raise UploadError(409, "tenant_document_limit_reached", message)
    if max_bytes and used + size > max_bytes:
        if scope.is_admin:
            message = (f"This upload would exceed your organization's storage limit of "
                       f"{max_bytes / (1024 * 1024):.0f} MB ({used / (1024 * 1024):.1f} MB used).")
        else:
            message = "This upload would exceed your organization's storage limit. Please contact an administrator."
        raise UploadError(409, "tenant_storage_limit_reached", message)


def upload_root() -> Path:
    return Path(os.environ.get("UPLOAD_DIR") or PROJECT_ROOT / "data" / "uploads").resolve()


# --- validation ---------------------------------------------------------------

def safe_filename(name: str) -> str:
    """A display-safe file name: no directories, no control or odd characters, bounded length."""
    name = unicodedata.normalize("NFKC", name or "")
    name = re.split(r"[\\/]", name)[-1]  # drop any path, Windows or POSIX style
    name = re.sub(r"[^\w.\- ]", "_", name).strip(" .")  # also removes leading dots ("..", ".env")
    name = re.sub(r"_+", "_", name)
    stem, ext = os.path.splitext(name)
    stem = stem[: MAX_FILENAME_LENGTH - len(ext)] or "document"
    return f"{stem}{ext.lower()}"


def validate_upload(filename: str, data: bytes) -> str:
    """Check extension, size and content; return the (lower-case) extension."""
    extension = os.path.splitext(safe_filename(filename))[1]
    if extension not in ALLOWED_EXTENSIONS:
        raise UploadError(415, "unsupported_file_type",
                          f"Only PDF, TXT and Markdown files can be uploaded (got {extension or 'no extension'!r}).")
    if len(data) > max_upload_bytes():
        raise UploadError(413, "file_too_large", f"Files can be at most {max_upload_bytes() // (1024 * 1024)} MB.")
    if not data.strip():
        raise UploadError(422, "empty_file", "The file is empty.")
    # The client's content type is not trusted; look at the bytes themselves.
    if extension == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise UploadError(415, "unsupported_file_type", "The file does not look like a PDF.")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise UploadError(415, "unsupported_file_type", "Text and Markdown files must be UTF-8 encoded.") from None
        if "\x00" in text:
            raise UploadError(415, "unsupported_file_type", "The file looks binary, not like text.")
    return extension


# --- storage --------------------------------------------------------------------

def _inside_root(path: Path) -> bool:
    try:
        path.resolve().relative_to(upload_root())
        return True
    except ValueError:
        return False


def _remove_file(path: Path) -> None:
    if not _inside_root(path):  # never delete anything outside the upload root
        log.error("Refusing to delete a file outside the upload root: %s", path)
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        log.warning("Could not delete uploaded file %s: %s", path, error)


def _write_file(tenant_id: int, extension: str, data: bytes) -> tuple[str, Path]:
    upload_id = uuid.uuid4().hex
    folder = upload_root() / str(int(tenant_id))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{upload_id}{extension}"
    temporary = folder / f".{upload_id}.partial"
    temporary.write_bytes(data)
    temporary.replace(path)  # the final name only ever holds a complete file
    return upload_id, path


# --- operations -------------------------------------------------------------------

@dataclass(frozen=True)
class DocumentInfo:
    id: int
    filename: str
    source_type: str
    origin: str
    chunk_count: int
    ingested_at: datetime

    @property
    def deletable(self) -> bool:
        return self.origin == UPLOAD_ORIGIN


_INFO_SELECT = """
    SELECT d.id, d.source, d.source_type, d.origin,
           (SELECT count(*) FROM document_chunks c WHERE c.document_id = d.id AND c.tenant_id = d.tenant_id),
           d.ingested_at
    FROM documents d
"""


def ingest_upload(
    conn: psycopg.Connection,
    model: SentenceTransformer,
    model_lock: threading.Lock,
    scope: AccessScope,
    filename: str,
    data: bytes,
) -> DocumentInfo:
    """Store one upload for `scope`'s user, in their tenant (uploads stay company-wide for now)."""
    tenant_id = scope.tenant_id
    extension = validate_upload(filename, data)
    display_name = safe_filename(filename)
    content_hash = hashlib.sha256(data).hexdigest()

    # Cheap early rejection, before anything is written, parsed or embedded.
    check_upload_allowed(conn, scope, content_hash, len(data))

    upload_id, path = _write_file(tenant_id, extension, data)
    try:
        try:
            document = load_document(path)
        except Exception as error:  # unreadable PDF, no text layer, bad encoding...
            raise UploadError(422, "unreadable_document", f"Could not extract text from the file: {error}") from None
        document = replace(
            document,
            source=display_name,
            relative_path=f"uploads/{upload_id}/{display_name}",  # unique per upload
            source_type=UPLOAD_SOURCE_TYPE,
            doc_id=None,
            title=document.title if document.title != path.name else display_name,
        )
        chunks = chunk_document(document)
        if not chunks:
            raise UploadError(422, "unreadable_document", "The file contains no text to index.")
        try:
            with model_lock, conn.transaction():
                # Authoritative check: the per-tenant advisory lock (held until this
                # transaction ends) makes check-and-store atomic across concurrent uploads.
                conn.execute("SELECT pg_advisory_xact_lock(%s::int, %s::int)", (QUOTA_LOCK_NAMESPACE, int(tenant_id)))
                check_upload_allowed(conn, scope, content_hash, len(data))
                document_id = store_document(
                    conn, model, tenant_id, document, chunks, content_hash, CHUNK_SIZE, CHUNK_OVERLAP,
                    origin=UPLOAD_ORIGIN, size_bytes=len(data),
                )
        except psycopg.Error as error:
            log.exception("Storing upload failed")
            raise UploadError(500, "ingestion_failed", "The document could not be stored. Nothing was saved.") from error
    except BaseException:
        _remove_file(path)
        raise
    return get_document(conn, tenant_id, document_id)


def get_document(conn: psycopg.Connection, tenant_id: int, document_id: int) -> DocumentInfo:
    row = conn.execute(f"{_INFO_SELECT} WHERE d.tenant_id = %s AND d.id = %s", (tenant_id, document_id)).fetchone()
    if row is None:
        # Same answer whether the document does not exist or belongs to another tenant.
        raise UploadError(404, "document_not_found", "Document not found.")
    return DocumentInfo(*row)


def list_documents(
    conn: psycopg.Connection, scope: AccessScope, limit: int, offset: int
) -> tuple[int, list[DocumentInfo]]:
    """The documents `scope` may read (the same filter retrieval uses); total and pages count only those."""
    total = conn.execute(
        f"SELECT count(*) FROM documents d WHERE {DOCUMENT_ACCESS_FILTER}", scope.sql_params()
    ).fetchone()[0]
    rows = conn.execute(
        f"""{_INFO_SELECT} WHERE {DOCUMENT_ACCESS_FILTER}
            ORDER BY d.ingested_at DESC, d.id DESC LIMIT %(limit)s OFFSET %(offset)s""",
        {**scope.sql_params(), "limit": limit, "offset": offset},
    ).fetchall()
    return total, [DocumentInfo(*row) for row in rows]


def delete_document(conn: psycopg.Connection, scope: AccessScope, document_id: int) -> None:
    """Delete an uploaded document the scope may read. A document it may not read gets the
    same 404 as one that does not exist, before any other check."""
    readable = {**scope.sql_params(), "document_id": document_id}
    row = conn.execute(
        f"SELECT origin, path FROM documents d WHERE d.id = %(document_id)s AND {DOCUMENT_ACCESS_FILTER}", readable
    ).fetchone()
    if row is None:
        raise UploadError(404, "document_not_found", "Document not found.")
    origin, stored_path = row
    if origin != UPLOAD_ORIGIN:
        raise UploadError(409, "document_managed_by_ingestion",
                          "This document is managed by server-side ingestion and cannot be deleted here.")
    with conn.transaction():
        # The access conditions again, so this statement alone can never touch a row the scope may not read.
        conn.execute(f"DELETE FROM documents d WHERE d.id = %(document_id)s AND {DOCUMENT_ACCESS_FILTER}",
                     readable)  # chunks cascade
    _remove_file(Path(stored_path))
