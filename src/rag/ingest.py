"""Store documents, chunks, and their embeddings in PostgreSQL.

A document is (re-)embedded only when its file content, metadata, the chunk
settings, the embedding model, or the embedding input format
(EMBEDDING_INPUT_VERSION) changed since the last run. Each document is
written in its own transaction, so an interrupted run never leaves a
half-stored document: running again simply picks up whatever is missing or
outdated.

Every document belongs to one tenant; all lookups and writes here are scoped
by tenant_id, so the same relative path can exist in several tenants.

Run on its own (no Gemini calls):
    .venv\\Scripts\\python.exe -m src.rag.ingest                     # data/documents/ -> tenant "default"
    .venv\\Scripts\\python.exe -m src.rag.ingest --tenant acme --documents-dir D:\\acme-docs --create-tenant
"""

import argparse
import hashlib
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer

from .chunking import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    EMBEDDING_INPUT_VERSION,
    Chunk,
    chunk_document,
    embedding_input,
)
from .db import connect, ensure_schema
from .embeddings import MODEL_NAME, describe_device, embed_texts, load_model
from .loader import Document, load_documents
from .tenants import DEFAULT_TENANT, TenantNotFound, get_or_create_tenant, get_tenant

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS_DIR = PROJECT_ROOT / "data" / "documents"


@dataclass
class SyncStats:
    stored: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    lexical_filled: list[str] = field(default_factory=list)  # up to date, but full-text index was missing
    failed: list[tuple[str, str]] = field(default_factory=list)  # (relative_path, error)


# Full-text index for a chunk: title counts most (A), then source type and
# section (B), then the chunk text (D). Parameters: title, "source section", text.
LEXICAL_VECTOR_SQL = (
    "setweight(to_tsvector('english', %s), 'A')"
    " || setweight(to_tsvector('english', %s), 'B')"
    " || setweight(to_tsvector('english', %s), 'D')"
)


def lexical_parts(chunk: Chunk) -> tuple[str, str, str]:
    return chunk.title, f"{chunk.source_type} {chunk.section}".strip(), chunk.text


def file_hash(document: Document) -> str:
    return hashlib.sha256(document.path.read_bytes()).hexdigest()


def is_up_to_date(
    conn: psycopg.Connection,
    tenant_id: int,
    document: Document,
    content_hash: str,
    chunk_size: int,
    chunk_overlap: int,
) -> bool:
    row = conn.execute(
        """
        SELECT source, source_type, doc_id, content_hash, chunk_size, chunk_overlap,
               embedding_model, embedding_input_version
        FROM documents WHERE tenant_id = %s AND relative_path = %s
        """,
        (tenant_id, document.relative_path),
    ).fetchone()
    expected = (
        document.source,
        document.source_type,
        document.doc_id,
        content_hash,
        chunk_size,
        chunk_overlap,
        MODEL_NAME,
        EMBEDDING_INPUT_VERSION,
    )
    return row == expected


def store_document(
    conn: psycopg.Connection,
    model: SentenceTransformer,
    tenant_id: int,
    document: Document,
    chunks: list[Chunk],
    content_hash: str,
    chunk_size: int,
    chunk_overlap: int,
    origin: str = "folder",
    size_bytes: int | None = None,
) -> int:
    """Embed and store one document with its chunks in a single transaction; return its id."""
    embeddings = embed_texts(model, [embedding_input(chunk) for chunk in chunks])

    with conn.transaction():
        document_id = conn.execute(
            """
            INSERT INTO documents
                (tenant_id, relative_path, source, source_type, doc_id, path, content_hash,
                 chunk_size, chunk_overlap, embedding_model, embedding_input_version, origin, size_bytes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, relative_path) DO UPDATE SET
                origin = EXCLUDED.origin,
                size_bytes = EXCLUDED.size_bytes,
                source = EXCLUDED.source,
                source_type = EXCLUDED.source_type,
                doc_id = EXCLUDED.doc_id,
                path = EXCLUDED.path,
                content_hash = EXCLUDED.content_hash,
                chunk_size = EXCLUDED.chunk_size,
                chunk_overlap = EXCLUDED.chunk_overlap,
                embedding_model = EXCLUDED.embedding_model,
                embedding_input_version = EXCLUDED.embedding_input_version,
                ingested_at = now()
            RETURNING id
            """,
            (
                tenant_id,
                document.relative_path,
                document.source,
                document.source_type,
                document.doc_id,
                str(document.path),
                content_hash,
                chunk_size,
                chunk_overlap,
                MODEL_NAME,
                EMBEDDING_INPUT_VERSION,
                origin,
                size_bytes,
            ),
        ).fetchone()[0]

        # Upsert by chunk_id so existing chunk rows keep their ids, then drop
        # any chunks left over from a longer previous version of the document.
        with conn.cursor() as cursor:
            cursor.executemany(
                f"""
                INSERT INTO document_chunks
                    (tenant_id, document_id, chunk_id, chunk_index, page_number, section, text,
                     start_char, end_char, embedding, lexical_vector)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, {LEXICAL_VECTOR_SQL})
                ON CONFLICT (tenant_id, chunk_id) DO UPDATE SET
                    document_id = EXCLUDED.document_id,
                    chunk_index = EXCLUDED.chunk_index,
                    page_number = EXCLUDED.page_number,
                    section = EXCLUDED.section,
                    text = EXCLUDED.text,
                    start_char = EXCLUDED.start_char,
                    end_char = EXCLUDED.end_char,
                    embedding = EXCLUDED.embedding,
                    lexical_vector = EXCLUDED.lexical_vector
                """,
                [
                    (
                        tenant_id,
                        document_id,
                        chunk.chunk_id,
                        index,
                        chunk.page_number,
                        chunk.section,
                        chunk.text,
                        chunk.start_char,
                        chunk.end_char,
                        embedding,
                        *lexical_parts(chunk),
                    )
                    for index, (chunk, embedding) in enumerate(zip(chunks, embeddings))
                ],
            )
        conn.execute(
            "DELETE FROM document_chunks WHERE document_id = %s AND chunk_index >= %s",
            (document_id, len(chunks)),
        )
    return document_id


def fill_missing_lexical_vectors(
    conn: psycopg.Connection, tenant_id: int, document: Document, chunks: list[Chunk]
) -> bool:
    """Build the full-text index for chunks stored before it existed. No re-embedding."""
    missing = conn.execute(
        """
        SELECT count(*) FROM document_chunks c JOIN documents d ON d.id = c.document_id
        WHERE d.tenant_id = %s AND d.relative_path = %s AND c.lexical_vector IS NULL
        """,
        (tenant_id, document.relative_path),
    ).fetchone()[0]
    if not missing:
        return False
    with conn.transaction(), conn.cursor() as cursor:
        cursor.executemany(
            f"UPDATE document_chunks SET lexical_vector = {LEXICAL_VECTOR_SQL} "
            "WHERE tenant_id = %s AND chunk_id = %s",
            [(*lexical_parts(chunk), tenant_id, chunk.chunk_id) for chunk in chunks],
        )
    return True


def sync_documents(
    conn: psycopg.Connection,
    model: SentenceTransformer,
    documents: list[Document],
    chunks_by_path: dict[str, list[Chunk]],
    chunk_size: int,
    chunk_overlap: int,
    verbose: bool = False,
    *,
    tenant_id: int,
) -> SyncStats:
    """Bring the tenant's stored documents in line with `documents`.

    With verbose, print a line per embedded document.
    """
    stats = SyncStats()
    total = len(documents)
    for number, document in enumerate(documents, start=1):
        content_hash = file_hash(document)
        chunks = chunks_by_path[document.relative_path]
        if is_up_to_date(conn, tenant_id, document, content_hash, chunk_size, chunk_overlap):
            stats.unchanged.append(document.relative_path)
            try:
                if fill_missing_lexical_vectors(conn, tenant_id, document, chunks):
                    stats.lexical_filled.append(document.relative_path)
            except psycopg.Error as error:
                stats.failed.append((document.relative_path, str(error).strip()))
            continue

        started = time.perf_counter()
        try:
            store_document(conn, model, tenant_id, document, chunks, content_hash, chunk_size, chunk_overlap)
        except psycopg.Error as error:
            # The document's transaction was rolled back; record it and carry on.
            stats.failed.append((document.relative_path, str(error).strip()))
            if verbose:
                print(f"[{number:>{len(str(total))}}/{total}] FAILED {document.relative_path}: {error}")
            continue

        stats.stored.append(document.relative_path)
        if verbose:
            elapsed = time.perf_counter() - started
            print(
                f"[{number:>{len(str(total))}}/{total}] embedded {len(chunks):>3} chunks "
                f"in {elapsed:5.2f}s  {document.relative_path}"
            )
    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest a folder of documents into one tenant.")
    parser.add_argument("--tenant", default=DEFAULT_TENANT, help=f"tenant slug (default: {DEFAULT_TENANT})")
    parser.add_argument(
        "--documents-dir", type=Path, default=DOCUMENTS_DIR, help="folder to ingest (default: data/documents)"
    )
    parser.add_argument("--create-tenant", action="store_true", help="create the tenant if it does not exist")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    started = time.perf_counter()

    try:
        documents = load_documents(args.documents_dir)
    except (FileNotFoundError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    chunks_by_path = {document.relative_path: chunk_document(document) for document in documents}

    try:
        conn = connect()
    except psycopg.OperationalError as error:
        print(f"Error: could not connect to PostgreSQL: {error}", file=sys.stderr)
        return 1

    with conn:
        ensure_schema(conn)
        try:
            tenant = get_or_create_tenant(conn, args.tenant) if args.create_tenant else get_tenant(conn, args.tenant)
        except (TenantNotFound, ValueError) as error:
            print(f"Error: {error} (use --create-tenant to create it)", file=sys.stderr)
            return 1
        model = load_model()
        by_type = Counter(document.source_type for document in documents)
        print(f"Model:     {MODEL_NAME}")
        print(f"Device:    {describe_device(str(model.device.type))}")
        print(f"Database:  {conn.info.dbname}")
        print(f"Tenant:    {tenant.slug} (id {tenant.id})")
        print(f"Folder:    {args.documents_dir}")
        print(
            f"Found:     {len(documents)} documents, "
            f"{sum(len(chunks) for chunks in chunks_by_path.values())} chunks "
            f"({', '.join(f'{name}: {count}' for name, count in sorted(by_type.items()))})"
        )
        print()

        sync_started = time.perf_counter()
        stats = sync_documents(
            conn, model, documents, chunks_by_path, CHUNK_SIZE, CHUNK_OVERLAP, verbose=True, tenant_id=tenant.id
        )
        sync_seconds = time.perf_counter() - sync_started

    print()
    print(f"Embedded and stored: {len(stats.stored)}")
    print(f"Already up to date:  {len(stats.unchanged)} (full-text index filled in for {len(stats.lexical_filled)})")
    print(f"Failed:              {len(stats.failed)}")
    for relative_path, error in stats.failed:
        print(f"  {relative_path}: {error}", file=sys.stderr)
    print(f"Sync time:           {sync_seconds:.1f}s (total {time.perf_counter() - started:.1f}s incl. model load)")
    return 1 if stats.failed else 0


if __name__ == "__main__":
    sys.exit(main())
