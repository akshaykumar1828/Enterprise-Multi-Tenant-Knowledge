"""The RAG pipeline as a reusable service (used by the API).

It composes the existing pieces without changing them:
    RerankingRetriever -> generate_answer (Gemini) -> check_citations

Models are loaded once and reused. Each call opens its own short-lived
database connection, and the service only reads from the database: documents
are added with the separate ingestion command (python -m src.rag.ingest).

Every search is scoped by an AccessScope (tenant, role, departments). The API
passes the authenticated user's scope, loaded from the database on each
request; the retriever applies it inside its SQL.
"""

import threading
import time
from dataclasses import dataclass, field

from google import genai
from pgvector.psycopg import register_vector

from .access import AccessScope
from .citations import check_citations
from .db import connect_app
from .embeddings import MODEL_NAME as EMBEDDING_MODEL_NAME
from .embeddings import load_model
from .llm import LLMError, create_client, generate_answer
from .llm import model_name as llm_model_name
from .retriever import RERANKER_MODEL, RerankingRetriever, SearchResult, load_reranker


class GenerationUnavailable(RuntimeError):
    """Answer generation is not configured (for example GEMINI_API_KEY is missing)."""


class CorpusEmpty(RuntimeError):
    """No documents are stored yet; run the ingestion command first."""


@dataclass
class PipelineResult:
    question: str
    sources: list[SearchResult]  # numbered 1..N in this order, exactly as sent to Gemini
    answer: str | None = None  # None when only retrieval was requested
    cited: list[int] = field(default_factory=list)  # valid [n] used in the answer
    removed_citations: list[int] = field(default_factory=list)  # [n] that matched no source
    retrieval_ms: float = 0.0
    generation_ms: float | None = None

    def cited_sources(self) -> list[tuple[int, SearchResult]]:
        """Citation number -> retrieved source. Built from our results, never from the LLM."""
        return [(number, self.sources[number - 1]) for number in sorted(self.cited)]


class RAGPipeline:
    def __init__(self):
        self.embedding_model = load_model()
        self.reranker = load_reranker()
        try:
            self.gemini_client: genai.Client | None = create_client()
            self.gemini_error: str | None = None
        except LLMError as error:
            self.gemini_client, self.gemini_error = None, str(error)
        # The configured answer model (local Ollama model by default, or Gemini).
        self.llm_model_name = llm_model_name()
        # Kept as an attribute so tests can substitute a fake and spend no Gemini quota.
        self.generate = generate_answer
        # One request at a time on the GPU models (search and upload embedding);
        # Gemini calls run outside this lock.
        self.model_lock = threading.Lock()

    # --- descriptive info -------------------------------------------------
    embedding_model_name = EMBEDDING_MODEL_NAME
    reranker_model_name = RERANKER_MODEL

    @property
    def embedding_device(self) -> str:
        return str(self.embedding_model.device)

    @property
    def reranker_device(self) -> str:
        return str(self.reranker.model.device)

    def corpus_counts(self) -> tuple[int, int]:
        """(documents, chunks) currently stored. Raises psycopg errors if the DB is down."""
        with connect_app() as conn:
            return conn.execute(
                "SELECT (SELECT count(*) FROM documents), (SELECT count(*) FROM document_chunks)"
            ).fetchone()

    # --- the pipeline -----------------------------------------------------
    def retrieve(self, question: str, top_k: int, *, scope: AccessScope) -> list[SearchResult]:
        """Search the documents `scope` may read. The caller supplies a trusted scope (loaded from the database)."""
        with connect_app() as conn:
            register_vector(conn)
            # No path list: the retriever's SQL applies the scope to every document.
            retriever = RerankingRetriever(conn, self.embedding_model, None, self.reranker, scope=scope)
            # Same answer whether the tenant has no documents or none this user may
            # read, so the response never reveals that restricted documents exist.
            if retriever.accessible_documents(limit=1) == 0:
                raise CorpusEmpty("Your organization has no documents yet.")
            with self.model_lock:
                return retriever.search(question, top_k=top_k)

    def answer(
        self, question: str, top_k: int = 3, retrieve_only: bool = False, *, scope: AccessScope
    ) -> PipelineResult:
        started = time.perf_counter()
        sources = self.retrieve(question, top_k, scope=scope)
        result = PipelineResult(question=question, sources=sources,
                                retrieval_ms=(time.perf_counter() - started) * 1000)
        if retrieve_only:
            return result

        if self.gemini_client is None:
            raise GenerationUnavailable(self.gemini_error or "Gemini client is not configured")
        started = time.perf_counter()
        raw_answer = self.generate(self.gemini_client, question, sources)
        result.generation_ms = (time.perf_counter() - started) * 1000

        checked = check_citations(raw_answer, len(sources))
        result.answer = checked.text
        result.cited = checked.cited
        result.removed_citations = checked.invalid
        return result
