"""API tests against the real models and database, with Gemini replaced by fakes.

Requests are made as a throwaway user of the "default" tenant, so these tests
also show the existing RAG behavior is unchanged behind authentication.

Run from the project root (uses no Gemini quota):
    .venv\\Scripts\\python.exe -m unittest tests.test_api -v
"""

import unittest

from tests.helpers import bearer, login, make_user  # noqa: I001  (sets the test JWT secret first)

from fastapi.testclient import TestClient
from google.genai import errors as genai_errors

from src.api.main import app
from src.rag.db import connect
from src.rag.llm import generate_answer
from src.rag.tenants import DEFAULT_TENANT, get_tenant

QUERY = "/api/v1/query"
HANDBOOK_QUESTION = "How many annual leave days do employees receive?"
PDF_QUESTION = "How long are customer records retained?"


def quota_exceeded(*args, **kwargs):
    raise genai_errors.ClientError(
        429,
        {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                   "message": "Quota exceeded for metric: free_tier_requests. Please retry in 6.8s."}},
    )


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()  # runs the lifespan: loads models once
        cls.pipeline = app.state.pipeline
        cls.real_client = cls.pipeline.gemini_client
        with connect() as conn:
            cls.user, password = make_user(conn, get_tenant(conn, DEFAULT_TENANT).id, "api-test")
        cls.client.headers.update(bearer(login(cls.client, cls.user.email, password)))

    @classmethod
    def tearDownClass(cls):
        with connect() as conn:
            conn.execute("DELETE FROM users WHERE id = %s", (cls.user.id,))
        cls.client_context.__exit__(None, None, None)

    def setUp(self):
        # Every test starts with generation faked, so no test can spend Gemini quota.
        self.pipeline.generate = lambda client, question, sources: "unused"
        self.pipeline.gemini_client = self.real_client or object()

    def tearDown(self):
        self.pipeline.generate = generate_answer
        self.pipeline.gemini_client = self.real_client

    # --- health ---------------------------------------------------------------
    def test_health(self):
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["database"]["ok"])
        self.assertGreater(body["database"]["documents"], 0)
        self.assertGreater(body["database"]["chunks"], 0)
        self.assertTrue(body["embedding_device"].startswith(("cuda", "cpu")))
        self.assertEqual(body["reranker_model"], "cross-encoder/ms-marco-MiniLM-L6-v2")

    # --- retrieval only (no Gemini) -------------------------------------------
    def test_retrieve_only_returns_sources_without_answer(self):
        response = self.client.post(QUERY, json={"question": PDF_QUESTION, "retrieve_only": True})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNone(body["answer"])
        self.assertEqual(body["citations"], [])
        self.assertEqual(len(body["sources"]), 3)
        self.assertEqual([s["number"] for s in body["sources"]], [1, 2, 3])
        top = body["sources"][0]
        self.assertEqual(top["source"], "sample_it_security_policy.pdf")
        self.assertEqual(top["page_number"], 3)
        self.assertIsNotNone(top["rerank_score"])
        self.assertIsNone(body["timings"]["generation_ms"])

    def test_top_k_is_respected(self):
        response = self.client.post(QUERY, json={"question": HANDBOOK_QUESTION, "top_k": 5, "retrieve_only": True})
        self.assertEqual(len(response.json()["sources"]), 5)

    # --- generation path with a fake Gemini -----------------------------------
    def test_citations_map_to_retrieved_sources_and_invalid_ones_are_removed(self):
        self.pipeline.generate = lambda client, question, sources: (
            "Employees receive 20 days of annual paid leave per calendar year [1]. Invented claim [9]."
        )
        response = self.client.post(QUERY, json={"question": HANDBOOK_QUESTION})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["answer"], "Employees receive 20 days of annual paid leave per calendar year [1]. Invented claim.")
        self.assertEqual(body["removed_citations"], [9])
        self.assertEqual([c["number"] for c in body["citations"]], [1])
        citation, source = body["citations"][0], body["sources"][0]
        for key in ("source", "relative_path", "doc_id", "page_number", "section", "chunk_id"):
            self.assertEqual(citation[key], source[key], key)
        self.assertEqual(citation["chunk_id"], "sample_company_handbook.md#2")

    def test_fake_generator_receives_the_same_sources_that_are_returned(self):
        seen = {}

        def fake(client, question, sources):
            seen["chunk_ids"] = [s.chunk_id for s in sources]
            return "Not available in the provided documents."

        self.pipeline.generate = fake
        body = self.client.post(QUERY, json={"question": HANDBOOK_QUESTION}).json()
        self.assertEqual(seen["chunk_ids"], [s["chunk_id"] for s in body["sources"]])
        self.assertEqual(body["citations"], [])

    # --- errors ----------------------------------------------------------------
    def test_gemini_quota_error_becomes_429(self):
        self.pipeline.generate = quota_exceeded
        response = self.client.post(QUERY, json={"question": HANDBOOK_QUESTION})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "llm_quota_exceeded")
        self.assertEqual(response.json()["error"]["retry_after_seconds"], 6.8)
        self.assertEqual(response.headers["Retry-After"], "7")

    def test_other_gemini_error_becomes_502(self):
        def server_error(*args, **kwargs):
            raise genai_errors.ServerError(500, {"error": {"code": 500, "message": "internal", "status": "INTERNAL"}})

        self.pipeline.generate = server_error
        response = self.client.post(QUERY, json={"question": HANDBOOK_QUESTION})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "llm_error")

    def test_missing_gemini_key_becomes_503_but_retrieve_only_still_works(self):
        self.pipeline.gemini_client = None
        response = self.client.post(QUERY, json={"question": HANDBOOK_QUESTION})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "llm_not_configured")
        response = self.client.post(QUERY, json={"question": HANDBOOK_QUESTION, "retrieve_only": True})
        self.assertEqual(response.status_code, 200)

    # --- request validation ----------------------------------------------------
    def test_invalid_requests_are_rejected(self):
        for payload in (
            {},
            {"question": ""},
            {"question": "   "},
            {"question": "x" * 2001},
            {"question": HANDBOOK_QUESTION, "top_k": 0},
            {"question": HANDBOOK_QUESTION, "top_k": 11},
            {"question": HANDBOOK_QUESTION, "unexpected": True},
        ):
            with self.subTest(payload=str(payload)[:60]):
                self.assertEqual(self.client.post(QUERY, json=payload).status_code, 422)


if __name__ == "__main__":
    unittest.main()
