"""LangChain retriever over the access-filtered search.

`AccessScopedRetriever` is a standard LangChain `BaseRetriever`, so it composes with any
LangChain chain (`retriever.invoke(question)`, LCEL `|`, callbacks, tracing). The search
itself is not LangChain's: it calls RAGPipeline.retrieve, which runs the access filter
inside the SQL (company-wide documents plus the caller's departments; admins see all),
then reranks with the cross-encoder and keeps at most 2 passages per document.

LangChain's PGVector store is deliberately not used. Its metadata filters cannot express
"documents shared with one of this user's departments" as a join, so authorization would
have to happen after retrieval, which could leak through ranking and return fewer results.

Each Document carries the original SearchResult in metadata["result"], so citations and
sources are still built from our own retrieval data, never from model output.
"""

from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict

from .access import AccessScope
from .retriever import SearchResult


def to_document(result: SearchResult) -> Document:
    return Document(
        page_content=result.text,
        id=result.chunk_id,
        metadata={
            "source": result.source,
            "page": result.page_number,
            "section": result.section,
            "score": result.score,
            "result": result,
        },
    )


def to_results(documents: list[Document]) -> list[SearchResult]:
    return [document.metadata["result"] for document in documents]


class AccessScopedRetriever(BaseRetriever):
    """Retrieves only passages the given scope may read. Build one per request."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    pipeline: Any  # a RAGPipeline (anything with retrieve(question, top_k, *, scope))
    scope: AccessScope  # trusted: loaded from the database for the authenticated user
    top_k: int = 3

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        return [to_document(result) for result in self.pipeline.retrieve(query, self.top_k, scope=self.scope)]
