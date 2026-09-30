"""Chunk retrieval over the stored chunk embeddings.

RerankingRetriever is what the pipeline uses by default:
    question -> vector top-50 -> cross-encoder rerank -> diversify -> top-K
PgVectorRetriever is the vector-only baseline (CLI --no-rerank).
HybridRetriever (vector + full-text, RRF fusion) is an experiment, and
InMemoryRetriever computes the same vector scores with numpy as a reference.

Every retriever fetches a larger candidate pool and then diversifies it, so
the final top-K is not crowded by several chunks of the same document.
"""

from dataclasses import dataclass

import numpy as np
import psycopg
from sentence_transformers import CrossEncoder, SentenceTransformer

from .chunking import Chunk, embedding_input
from .embeddings import embed_texts, hf_offline, pick_device

# How many nearest chunks to consider before diversifying into the final top-K.
CANDIDATE_POOL = 50
# Upper bound on chunks from one document in the final results.
MAX_CHUNKS_PER_DOCUMENT = 2

# Hybrid retrieval. Each list contributes weight / (RRF_K + rank) to a chunk's
# fused score (Reciprocal Rank Fusion); a larger RRF_K flattens rank differences.
VECTOR_CANDIDATES = 50
LEXICAL_CANDIDATES = 50
RRF_K = 60
VECTOR_WEIGHT = 1.0
LEXICAL_WEIGHT = 1.0

# Cross-encoder reranking (the default pipeline; CLI --no-rerank skips it).
RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L6-v2"
RERANK_CANDIDATES = 50


@dataclass
class SearchResult:
    chunk_id: str
    source: str
    relative_path: str
    source_type: str
    doc_id: str | None
    page_number: int | None
    section: str
    text: str
    score: float  # cosine similarity to the question
    # Filled in by HybridRetriever: 1-based rank in each list (None if absent) and the RRF score.
    vector_rank: int | None = None
    lexical_rank: int | None = None
    fused_score: float | None = None
    # Filled in by RerankingRetriever: the cross-encoder's relevance score.
    rerank_score: float | None = None


def validate_query(question: str, top_k: int) -> None:
    if not question.strip():
        raise ValueError("Question must not be empty")
    if top_k < 1:
        raise ValueError("top_k must be at least 1")


def diversify(
    candidates: list["SearchResult"], top_k: int, max_per_document: int = MAX_CHUNKS_PER_DOCUMENT
) -> list["SearchResult"]:
    """Pick top_k results from ranked candidates (best first), spreading them across documents.

    First pass: the best chunk of each document, best documents first.
    Second pass (only if that gives fewer than top_k): the next-best chunks,
    at most `max_per_document` per document. Scores are never changed; the
    selection keeps the candidates' ranking order.
    """
    selected: list[SearchResult] = []
    per_document: dict[str, int] = {}

    for candidate in candidates:
        if len(selected) == top_k:
            break
        if candidate.relative_path not in per_document:
            selected.append(candidate)
            per_document[candidate.relative_path] = 1

    for candidate in candidates:
        if len(selected) == top_k:
            break
        count = per_document[candidate.relative_path]
        if candidate not in selected and count < max_per_document:
            selected.append(candidate)
            per_document[candidate.relative_path] = count + 1

    position = {id(candidate): index for index, candidate in enumerate(candidates)}
    return sorted(selected, key=lambda result: position[id(result)])


def reciprocal_rank_fusion(
    vector: list["SearchResult"],
    lexical: list["SearchResult"],
    k: int = RRF_K,
    vector_weight: float = VECTOR_WEIGHT,
    lexical_weight: float = LEXICAL_WEIGHT,
) -> list["SearchResult"]:
    """Merge two ranked lists into one, best first.

    A chunk's fused score is the sum over the lists it appears in of
    weight / (k + rank). Ties are broken by cosine similarity.
    """
    fused: dict[str, SearchResult] = {}
    for weight, ranking, rank_field in (
        (vector_weight, vector, "vector_rank"),
        (lexical_weight, lexical, "lexical_rank"),
    ):
        for rank, result in enumerate(ranking, start=1):
            merged = fused.setdefault(result.chunk_id, result)
            setattr(merged, rank_field, rank)
            merged.fused_score = (merged.fused_score or 0.0) + weight / (k + rank)
    return sorted(fused.values(), key=lambda r: (r.fused_score, r.score), reverse=True)


def cosine_similarity(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Cosine similarity between one query vector and every row of `matrix`."""
    query_norm = np.linalg.norm(query)
    row_norms = np.linalg.norm(matrix, axis=1)
    # Guard against division by zero for an all-zero vector.
    denominator = np.maximum(row_norms * query_norm, 1e-12)
    return (matrix @ query) / denominator


class InMemoryRetriever:
    def __init__(self, model: SentenceTransformer, chunks: list[Chunk]):
        if not chunks:
            raise ValueError("Cannot build a retriever from zero chunks")
        self.model = model
        self.chunks = chunks
        self.embeddings = embed_texts(model, [embedding_input(chunk) for chunk in chunks])

    def search(self, question: str, top_k: int = 3) -> list[SearchResult]:
        validate_query(question, top_k)
        query_vector = embed_texts(self.model, [question])[0]
        scores = cosine_similarity(query_vector, self.embeddings)
        best = np.argsort(-scores)[: max(top_k, CANDIDATE_POOL)]

        candidates = [
            SearchResult(
                chunk_id=self.chunks[i].chunk_id,
                source=self.chunks[i].source,
                relative_path=self.chunks[i].relative_path,
                source_type=self.chunks[i].source_type,
                doc_id=self.chunks[i].doc_id,
                page_number=self.chunks[i].page_number,
                section=self.chunks[i].section,
                text=self.chunks[i].text,
                score=float(scores[i]),
            )
            for i in best
        ]
        single_document = len({chunk.relative_path for chunk in self.chunks}) == 1
        return diversify(candidates, top_k, top_k if single_document else MAX_CHUNKS_PER_DOCUMENT)


class PgVectorRetriever:
    """Search one tenant's chunks stored in PostgreSQL, limited to the given documents.

    tenant_id is required and every candidate query filters on it in SQL, so a
    search can never return another tenant's chunks, whatever relative_paths
    contains.
    """

    def __init__(
        self, conn: psycopg.Connection, model: SentenceTransformer, relative_paths: list[str], *, tenant_id: int
    ):
        if not relative_paths:
            raise ValueError("Cannot search without any documents")
        self.conn = conn
        self.model = model
        self.relative_paths = relative_paths
        self.tenant_id = tenant_id

    # Tenant filter shared by every candidate query. It is applied to both
    # tables; the schema also guarantees they agree (composite foreign key).
    TENANT_FILTER = "c.tenant_id = %(tenant_id)s AND d.tenant_id = %(tenant_id)s"

    # Columns shared by every candidate query; <=> is pgvector's cosine
    # distance, so similarity = 1 - distance.
    RESULT_COLUMNS = """
        c.chunk_id, d.source, d.relative_path, d.source_type, d.doc_id,
        c.page_number, c.section, c.text, 1 - (c.embedding <=> %(query)s) AS score
    """

    def _to_results(self, rows) -> list[SearchResult]:
        return [
            SearchResult(
                chunk_id=chunk_id,
                source=source,
                relative_path=relative_path,
                source_type=source_type,
                doc_id=doc_id,
                page_number=page_number,
                section=section,
                text=text,
                score=float(score),
            )
            for chunk_id, source, relative_path, source_type, doc_id, page_number, section, text, score in rows
        ]

    def _max_per_document(self, top_k: int) -> int:
        # Searching a single document (e.g. --document): nothing to diversify across.
        return top_k if len(self.relative_paths) == 1 else MAX_CHUNKS_PER_DOCUMENT

    def vector_candidates(self, query_vector: np.ndarray, limit: int) -> list[SearchResult]:
        rows = self.conn.execute(
            f"""
            SELECT {self.RESULT_COLUMNS}
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE {self.TENANT_FILTER}
              AND d.relative_path = ANY(%(paths)s)
            ORDER BY c.embedding <=> %(query)s
            LIMIT %(limit)s
            """,
            {"query": query_vector, "tenant_id": self.tenant_id, "paths": self.relative_paths, "limit": limit},
        ).fetchall()
        return self._to_results(rows)

    def search(self, question: str, top_k: int = 3) -> list[SearchResult]:
        validate_query(question, top_k)
        query_vector = embed_texts(self.model, [question])[0]
        candidates = self.vector_candidates(query_vector, max(top_k, CANDIDATE_POOL))
        return diversify(candidates, top_k, self._max_per_document(top_k))


def load_reranker(model_name: str = RERANKER_MODEL, device: str | None = None) -> CrossEncoder:
    return CrossEncoder(model_name, device=device or pick_device(), local_files_only=hf_offline())


class RerankingRetriever(PgVectorRetriever):
    """Vector candidates re-ordered by a cross-encoder, then diversified. The default retriever."""

    def __init__(
        self,
        conn: psycopg.Connection,
        model: SentenceTransformer,
        relative_paths: list[str],
        reranker: CrossEncoder,
        candidates: int = RERANK_CANDIDATES,
        *,
        tenant_id: int,
    ):
        super().__init__(conn, model, relative_paths, tenant_id=tenant_id)
        self.reranker = reranker
        self.candidates = candidates

    def search(self, question: str, top_k: int = 3) -> list[SearchResult]:
        validate_query(question, top_k)
        query_vector = embed_texts(self.model, [question])[0]
        candidates = self.vector_candidates(query_vector, max(top_k, self.candidates))
        # The cross-encoder reads the question and the original chunk text together.
        scores = self.reranker.predict([(question, c.text) for c in candidates], batch_size=32)
        for candidate, score in zip(candidates, scores):
            candidate.rerank_score = float(score)
        reranked = sorted(candidates, key=lambda c: c.rerank_score, reverse=True)
        return diversify(reranked, top_k, self._max_per_document(top_k))


class HybridRetriever(PgVectorRetriever):
    """Vector search + PostgreSQL full-text search, merged with Reciprocal Rank Fusion."""

    def lexical_candidates(self, question: str, query_vector: np.ndarray, limit: int) -> list[SearchResult]:
        # plainto_tsquery ANDs every word, which is too strict for natural-language
        # questions; OR the words instead and let ts_rank order the matches.
        rows = self.conn.execute(
            f"""
            WITH q AS (
                SELECT replace(plainto_tsquery('english', %(question)s)::text, ' & ', ' | ')::tsquery AS query
            )
            SELECT {self.RESULT_COLUMNS}
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id, q
            WHERE {self.TENANT_FILTER}
              AND d.relative_path = ANY(%(paths)s)
              AND c.lexical_vector @@ q.query
            ORDER BY ts_rank(c.lexical_vector, q.query) DESC, c.id
            LIMIT %(limit)s
            """,
            {"question": question, "query": query_vector, "tenant_id": self.tenant_id,
             "paths": self.relative_paths, "limit": limit},
            # Never reuse a prepared plan here. Without the actual values the planner
            # guesses the path list and the OR-query match only a handful of rows and
            # picks a nested loop that re-scans the full-text index once per document
            # (~16 s instead of ~0.2 s). Parameters are still bound server-side.
            prepare=False,
        ).fetchall()
        return self._to_results(rows)

    def search(self, question: str, top_k: int = 3) -> list[SearchResult]:
        validate_query(question, top_k)
        query_vector = embed_texts(self.model, [question])[0]
        vector = self.vector_candidates(query_vector, max(top_k, VECTOR_CANDIDATES))
        lexical = self.lexical_candidates(question, query_vector, max(top_k, LEXICAL_CANDIDATES))
        fused = reciprocal_rank_fusion(vector, lexical)
        return diversify(fused, top_k, self._max_per_document(top_k))
