"""The LangChain retriever is a thin adapter: same results, same scope, nothing added or dropped.

No database or models: RAGPipeline.retrieve is replaced by a recorder.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_lc_retriever -v
"""

import unittest

from langchain_core.retrievers import BaseRetriever

from src.rag.access import AccessScope
from src.rag.lc_retriever import AccessScopedRetriever, to_results
from src.rag.retriever import SearchResult


def result(n: int) -> SearchResult:
    return SearchResult(chunk_id=f"doc{n}.md#{n}", source=f"doc{n}.md", relative_path=f"doc{n}.md", source_type="local",
                        doc_id=None, page_number=n, section=f"Section {n}", text=f"Passage {n}.", score=1.0 / n)


class FakePipeline:
    def __init__(self, results):
        self.results, self.calls = results, []

    def retrieve(self, question, top_k, *, scope):
        self.calls.append((question, top_k, scope))
        return self.results


class AccessScopedRetrieverTests(unittest.TestCase):
    def setUp(self):
        self.scope = AccessScope(tenant_id=1, user_id=7, role="employee", department_ids=(3,))
        self.results = [result(1), result(2), result(3)]
        self.pipeline = FakePipeline(self.results)

    def test_is_a_langchain_retriever_that_passes_the_scope_through(self):
        retriever = AccessScopedRetriever(pipeline=self.pipeline, scope=self.scope, top_k=3)
        self.assertIsInstance(retriever, BaseRetriever)
        documents = retriever.invoke("How many leave days?")
        self.assertEqual(self.pipeline.calls, [("How many leave days?", 3, self.scope)])
        self.assertEqual([d.page_content for d in documents], ["Passage 1.", "Passage 2.", "Passage 3."])
        self.assertEqual(documents[1].metadata["page"], 2)
        self.assertEqual(documents[1].metadata["section"], "Section 2")

    def test_round_trip_keeps_the_exact_results_in_order(self):
        documents = AccessScopedRetriever(pipeline=self.pipeline, scope=self.scope).invoke("q")
        recovered = to_results(documents)
        self.assertEqual(len(recovered), 3)
        for original, back in zip(self.results, recovered):
            self.assertIs(back, original)

    def test_retrieval_errors_propagate(self):
        class Failing(FakePipeline):
            def retrieve(self, question, top_k, *, scope):
                raise RuntimeError("database down")

        with self.assertRaisesRegex(RuntimeError, "database down"):
            AccessScopedRetriever(pipeline=Failing([]), scope=self.scope).invoke("q")


if __name__ == "__main__":
    unittest.main()
