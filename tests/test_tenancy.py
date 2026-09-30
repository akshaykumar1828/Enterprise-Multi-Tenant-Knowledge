"""Multi-tenancy tests: no chunk may ever cross a tenant boundary.

Two throwaway tenants get documents at the SAME relative path with different
secrets. The tests check every retriever, the API, and the database
constraint, then delete the test tenants and their data. No Gemini calls.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_tenancy -v
"""

import secrets
import tempfile
import unittest
from pathlib import Path

from tests.helpers import bearer, login, make_user  # noqa: I001  (sets the test JWT secret first)

import psycopg
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from pgvector.psycopg import register_vector

from src.api.main import app
from src.rag.chunking import chunk_document
from src.rag.db import connect, ensure_schema
from src.rag.embeddings import load_model
from src.rag.ingest import sync_documents
from src.rag.loader import load_documents
from src.rag.retriever import HybridRetriever, PgVectorRetriever, RerankingRetriever, load_reranker
from src.rag.tenants import DEFAULT_TENANT, get_or_create_tenant, get_tenant

PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUESTION = "What is the Zephyr launch code?"
SECRET = {"a": "ALPHA-7", "b": "BRAVO-9"}


def write_tenant_documents(folder: Path, secret: str) -> None:
    (folder / "policies").mkdir(parents=True)
    # Same relative path in both tenants, different content.
    (folder / "policies" / "launch.md").write_text(
        f"# Project Zephyr\n\nThe Zephyr launch code is {secret}. "
        "Only the launch director may read the Zephyr launch code aloud.\n",
        encoding="utf-8",
    )
    (folder / "handbook.md").write_text(
        "# Office Handbook\n\nThe office opens at 08:00 and closes at 18:00 on weekdays.\n", encoding="utf-8"
    )


class TenantIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_dotenv(PROJECT_ROOT / ".env")
        cls.model = load_model()
        cls.reranker = load_reranker()
        cls.conn = connect()
        ensure_schema(cls.conn)
        register_vector(cls.conn)

        suffix = secrets.token_hex(4)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tenants, cls.paths = {}, {}
        for key in ("a", "b"):
            tenant = get_or_create_tenant(cls.conn, f"test-tenancy-{key}-{suffix}", f"Test tenant {key.upper()}")
            folder = Path(cls.tmp.name) / key
            write_tenant_documents(folder, SECRET[key])
            documents = load_documents(folder)
            chunks = {d.relative_path: chunk_document(d) for d in documents}
            stats = sync_documents(cls.conn, cls.model, documents, chunks, 500, 100, tenant_id=tenant.id)
            assert not stats.failed, stats.failed
            cls.tenants[key] = tenant
            cls.paths[key] = [d.relative_path for d in documents]
        cls.default = get_tenant(cls.conn, DEFAULT_TENANT)
        cls.default_paths = [r[0] for r in cls.conn.execute(
            "SELECT relative_path FROM documents WHERE tenant_id = %s", (cls.default.id,))]

    @classmethod
    def tearDownClass(cls):
        ids = [t.id for t in cls.tenants.values()]
        cls.conn.execute("DELETE FROM documents WHERE tenant_id = ANY(%s)", (ids,))  # chunks cascade
        cls.conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", (ids,))
        cls.conn.close()
        cls.tmp.cleanup()

    def texts(self, results) -> str:
        return "\n".join(r.text for r in results)

    # --- storage ------------------------------------------------------------------
    def test_same_relative_path_is_stored_separately_per_tenant(self):
        rows = self.conn.execute(
            "SELECT tenant_id, id FROM documents WHERE relative_path = 'policies/launch.md' AND tenant_id = ANY(%s)",
            ([t.id for t in self.tenants.values()],),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r[0] for r in rows}, {t.id for t in self.tenants.values()})

    def test_reingesting_one_tenant_is_idempotent(self):
        tenant = self.tenants["a"]
        documents = load_documents(Path(self.tmp.name) / "a")
        chunks = {d.relative_path: chunk_document(d) for d in documents}
        stats = sync_documents(self.conn, self.model, documents, chunks, 500, 100, tenant_id=tenant.id)
        self.assertEqual((len(stats.stored), len(stats.unchanged)), (0, len(documents)))

    # --- retrieval never crosses tenants --------------------------------------------
    def test_every_retriever_returns_only_its_own_tenant(self):
        for key, other in (("a", "b"), ("b", "a")):
            tenant = self.tenants[key]
            retrievers = {
                "reranking": RerankingRetriever(self.conn, self.model, self.paths[key], self.reranker, tenant_id=tenant.id),
                "vector": PgVectorRetriever(self.conn, self.model, self.paths[key], tenant_id=tenant.id),
                "hybrid": HybridRetriever(self.conn, self.model, self.paths[key], tenant_id=tenant.id),
            }
            for name, retriever in retrievers.items():
                with self.subTest(tenant=key, retriever=name):
                    results = retriever.search(QUESTION, top_k=10)
                    self.assertIn(SECRET[key], results[0].text)
                    self.assertNotIn(SECRET[other], self.texts(results))

    def test_other_tenants_paths_cannot_widen_a_search(self):
        # Even if a caller passes every path that exists anywhere, the SQL tenant filter holds.
        all_paths = self.paths["a"] + self.paths["b"] + self.default_paths
        retriever = PgVectorRetriever(self.conn, self.model, all_paths, tenant_id=self.tenants["a"].id)
        results = retriever.search(QUESTION, top_k=10)
        self.assertNotIn(SECRET["b"], self.texts(results))
        stored = self.conn.execute(
            "SELECT count(*) FROM document_chunks WHERE tenant_id = %s", (self.tenants["a"].id,)).fetchone()[0]
        self.assertLessEqual(len(results), stored)  # only tenant A has chunks to return
        self.assertTrue(all(r.relative_path in self.paths["a"] for r in results))

    def test_default_tenant_cannot_see_test_tenants(self):
        retriever = RerankingRetriever(
            self.conn, self.model, self.default_paths + self.paths["a"], self.reranker, tenant_id=self.default.id
        )
        text = self.texts(retriever.search(QUESTION, top_k=10))
        self.assertNotIn(SECRET["a"], text)
        self.assertNotIn(SECRET["b"], text)

    # --- the database itself enforces chunk/document tenant agreement ---------------
    def test_database_rejects_a_chunk_attached_to_another_tenants_document(self):
        doc_a = self.conn.execute(
            "SELECT id FROM documents WHERE tenant_id = %s LIMIT 1", (self.tenants["a"].id,)).fetchone()[0]
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with self.conn.transaction():
                self.conn.execute(
                    """INSERT INTO document_chunks (tenant_id, document_id, chunk_id, chunk_index, text,
                                                    start_char, end_char, embedding)
                       VALUES (%s, %s, 'forged#0', 999, 'forged', 0, 6, array_fill(0.1, ARRAY[384])::vector)""",
                    (self.tenants["b"].id, doc_a),
                )

    # --- API --------------------------------------------------------------------------
    def test_api_scopes_queries_by_the_authenticated_users_tenant(self):
        users = {key: make_user(self.conn, tenant.id, f"tenancy-{key}") for key, tenant in self.tenants.items()}
        users["default"] = make_user(self.conn, self.default.id, "tenancy-default")
        try:
            with TestClient(app) as client:
                tokens = {key: login(client, user.email, password) for key, (user, password) in users.items()}
                body = {"question": QUESTION, "top_k": 10, "retrieve_only": True}
                for key, other in (("a", "b"), ("b", "a")):
                    with self.subTest(tenant=key):
                        response = client.post("/api/v1/query", json=body, headers=bearer(tokens[key]))
                        self.assertEqual(response.status_code, 200)
                        texts = "\n".join(s["text"] for s in response.json()["sources"])
                        self.assertIn(SECRET[key], texts)
                        self.assertNotIn(SECRET[other], texts)

                default = client.post("/api/v1/query", json=body, headers=bearer(tokens["default"]))
                self.assertEqual(default.status_code, 200)
                default_text = "\n".join(s["text"] for s in default.json()["sources"])
                self.assertNotIn(SECRET["a"], default_text)
                self.assertNotIn(SECRET["b"], default_text)
        finally:
            self.conn.execute("DELETE FROM users WHERE id = ANY(%s)", ([u.id for u, _ in users.values()],))


if __name__ == "__main__":
    unittest.main()
