"""Ask one question against the documents and print a grounded answer.

Documents are chunked and embedded locally, stored in PostgreSQL + pgvector
(only new or changed files are re-embedded), and searched there.

Retrieval: contextual vector search -> top 50 candidates -> cross-encoder
reranking -> diversification -> top-K. --no-rerank skips the cross-encoder
(vector search + diversification only) for comparison.

By default every supported file in data/documents/ is searched together;
--document restricts the search to a single file.

Usage (from the project root):
    .venv\\Scripts\\python.exe -m src.rag "What home-office stipend do new employees get?"
    .venv\\Scripts\\python.exe -m src.rag "..." --document <relative path of one stored document>
    .venv\\Scripts\\python.exe -m src.rag "..." --retrieve-only   # no Gemini call
    .venv\\Scripts\\python.exe -m src.rag "..." --no-rerank       # vector-only baseline
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from google.genai import errors as genai_errors

from .access import AccessScope
from .chunking import CHUNK_OVERLAP, CHUNK_SIZE, chunk_document
from .citations import check_citations
from .db import connect, ensure_schema
from .embeddings import MODEL_NAME, describe_device, load_model
from .ingest import sync_documents
from .llm import LLMError, create_client, generate_answer
from .llm import model_name as llm_model_name
from .loader import LOCAL_SOURCE_TYPE, load_document, load_documents
from .retriever import RERANKER_MODEL, PgVectorRetriever, RerankingRetriever, SearchResult, load_reranker
from .tenants import DEFAULT_TENANT, TenantNotFound, get_tenant

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS_DIR = PROJECT_ROOT / "data" / "documents"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Retrieve the chunks most relevant to a question.")
    parser.add_argument("question", help="the question to search for")
    parser.add_argument(
        "--document",
        type=Path,
        help="search only this Markdown, text, or PDF file (default: all files in data/documents/)",
    )
    parser.add_argument("--top-k", type=int, default=3, help="number of chunks to return (default: 3)")
    parser.add_argument("--chunk-size", type=int, default=CHUNK_SIZE)
    parser.add_argument("--chunk-overlap", type=int, default=CHUNK_OVERLAP)
    parser.add_argument(
        "--retrieve-only", action="store_true", help="print the retrieved chunks without calling Gemini"
    )
    parser.add_argument(
        "--no-rerank",
        action="store_true",
        help="skip cross-encoder reranking (vector search + diversification only, for comparison)",
    )
    parser.add_argument(
        "--tenant",
        default=DEFAULT_TENANT,
        help=f"tenant whose documents are searched (default: {DEFAULT_TENANT}; only it is synced from data/documents)",
    )
    return parser.parse_args()


def print_chunks(results: list[SearchResult]) -> None:
    for rank, result in enumerate(results, start=1):
        print(f"\nRESULT {rank}")
        print(f"Score:   {result.score:.4f}")
        if result.rerank_score is not None:
            print(f"Rerank:  {result.rerank_score:.4f}")
        if result.fused_score is not None:
            as_rank = lambda value: value if value is not None else "-"
            print(
                f"Fusion:  {result.fused_score:.4f}  "
                f"(vector rank {as_rank(result.vector_rank)}, lexical rank {as_rank(result.lexical_rank)})"
            )
        print(f"Source:  {result.source}")
        if result.source_type != LOCAL_SOURCE_TYPE:
            print(f"Type:    {result.source_type}")
            print(f"Path:    {result.relative_path}")
        if result.doc_id:
            print(f"Doc ID:  {result.doc_id}")
        if result.page_number is not None:
            print(f"Page:    {result.page_number}")
        print(f"Chunk:   {result.chunk_id}  [{result.section or 'n/a'}]")
        print(f"Text:\n{result.text}")


def print_citations(cited: list[int], results: list[SearchResult]) -> None:
    """Map each [n] used in the answer to its retrieved source (our metadata, not the LLM's)."""
    print("\nCITATIONS:")
    if not cited:
        print("(none - the answer does not cite any retrieved source)")
        return
    for number in sorted(cited):
        result = results[number - 1]
        details = [result.source]
        if result.page_number is not None:
            details.append(f"page {result.page_number}")
        if result.section:
            details.append(f"section {result.section}")
        if result.doc_id:
            details.append(result.doc_id)
        print(f"[{number}] " + " | ".join(details) + f"  ({result.chunk_id})")


def print_sources(results: list[SearchResult]) -> None:
    """Print the chunks sent to Gemini as context.

    Everything here comes from our own retrieval metadata, never from the LLM
    output. The numbers match the [n] labels in the prompt context, which are
    also the citation numbers in the answer.
    """
    print("\nSOURCES:")
    for number, result in enumerate(results, start=1):
        print(f"\n[{number}] {result.source}")
        if result.source_type != LOCAL_SOURCE_TYPE:
            print(f"    Type:       {result.source_type}")
            print(f"    Path:       {result.relative_path}")
        if result.doc_id:
            print(f"    Doc ID:     {result.doc_id}")
        if result.page_number is not None:
            print(f"    Page:       {result.page_number}")
        print(f"    Section:    {result.section or 'n/a'}")
        print(f"    Chunk ID:   {result.chunk_id}")
        print(f"    Similarity: {result.score:.4f}")
        print("    Text:")
        for line in result.text.splitlines():
            print(f"      {line}" if line else "")


def main() -> int:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")

    client = None
    if not args.retrieve_only:
        # Fail fast on a missing API key before spending time on embeddings.
        try:
            client = create_client()
        except LLMError as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1

    # data/documents/ belongs to the default tenant, so only that tenant is synced
    # from disk here; other tenants are searched as stored (ingest them with
    # python -m src.rag.ingest --tenant <slug> --documents-dir <folder>).
    sync_from_disk = args.tenant == DEFAULT_TENANT
    documents, chunks_by_path = [], {}
    if sync_from_disk:
        try:
            if args.document:
                documents = [load_document(args.document, root=DOCUMENTS_DIR)]
            else:
                documents = load_documents(DOCUMENTS_DIR)
            chunks_by_path = {
                document.relative_path: chunk_document(document, args.chunk_size, args.chunk_overlap)
                for document in documents
            }
        except (FileNotFoundError, ValueError) as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1

    try:
        conn = connect()
    except psycopg.OperationalError as error:
        print(f"Error: could not connect to PostgreSQL: {error}", file=sys.stderr)
        return 1

    with conn:
        ensure_schema(conn)
        try:
            tenant = get_tenant(conn, args.tenant)
        except TenantNotFound as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
        model = load_model()
        print(f"Model:    {MODEL_NAME}")
        print(f"Device:   {describe_device(str(model.device.type))}")
        print(f"Tenant:   {tenant.slug}")

        if sync_from_disk:
            total_chunks = sum(len(chunks) for chunks in chunks_by_path.values())
            by_type = Counter(document.source_type for document in documents)
            type_summary = ", ".join(f"{name}: {count}" for name, count in sorted(by_type.items()))
            print(f"Corpus:   {len(documents)} document(s), {total_chunks} chunks ({type_summary})")

            stats = sync_documents(
                conn, model, documents, chunks_by_path, args.chunk_size, args.chunk_overlap, tenant_id=tenant.id
            )
            print(
                f"Database: {conn.info.dbname} (embedded and stored: {len(stats.stored)}, "
                f"already up to date: {len(stats.unchanged)}, failed: {len(stats.failed)})"
            )
            for relative_path, error in stats.failed:
                print(f"Warning: could not store {relative_path}: {error}", file=sys.stderr)
            relative_paths = [document.relative_path for document in documents]
        else:
            relative_paths = [row[0] for row in conn.execute(
                "SELECT relative_path FROM documents WHERE tenant_id = %s", (tenant.id,))]
            if args.document:
                wanted = args.document.as_posix()
                relative_paths = [path for path in relative_paths if path == wanted]
            print(f"Corpus:   {len(relative_paths)} stored document(s) (search only; not synced from disk)")
            if not relative_paths:
                print(f"Error: no matching documents stored for tenant {tenant.slug!r}", file=sys.stderr)
                return 1
        if not args.retrieve_only:
            print(f"LLM:      {llm_model_name()}")

        # Operator tool: full access to the chosen tenant, narrowed to the listed documents.
        scope = AccessScope.operator(tenant.id)
        if args.no_rerank:
            retriever = PgVectorRetriever(conn, model, relative_paths, scope=scope)
            print("Retriever: vector search + diversification (--no-rerank)")
        else:
            reranker = load_reranker()
            retriever = RerankingRetriever(conn, model, relative_paths, reranker, scope=scope)
            print(f"Retriever: vector top-50 -> {RERANKER_MODEL} on {reranker.model.device} -> diversification")
        try:
            results = retriever.search(args.question, top_k=args.top_k)
        except ValueError as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1

    print(f"\nQUESTION:\n{args.question}")
    if args.retrieve_only:
        print_chunks(results)
        return 0

    try:
        answer = generate_answer(client, args.question, results)
    except genai_errors.APIError as error:
        print(f"Error: Gemini API request failed ({error.code}): {error.message}", file=sys.stderr)
        return 1
    except LLMError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    checked = check_citations(answer, len(results))
    if checked.invalid:
        print(
            f"Warning: removed citation(s) {checked.invalid} that do not match any of the "
            f"{len(results)} retrieved sources",
            file=sys.stderr,
        )
    print(f"\nANSWER:\n{checked.text}")
    print_citations(checked.cited, results)
    print_sources(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
