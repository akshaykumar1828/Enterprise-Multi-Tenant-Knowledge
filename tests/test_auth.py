"""Authentication and user-to-tenant isolation tests. No Gemini calls.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_auth -v
"""

import os
import secrets
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.helpers import bearer, ensure_sample_corpus, login, make_user, new_password, unique_email  # noqa: I001  (test JWT secret first)

import jwt
from fastapi.testclient import TestClient
from pgvector.psycopg import register_vector

from src.api.auth import ALGORITHM
from src.api.main import app
from src.rag.access import AccessScope
from src.rag.chunking import chunk_document
from src.rag.db import connect
from src.rag.ingest import sync_documents
from src.rag.llm import generate_answer
from src.rag.loader import load_documents
from src.rag.retriever import RerankingRetriever
from src.rag.tenants import DEFAULT_TENANT, get_tenant

QUERY = "/api/v1/query"
SECRET_FACT = f"The Orion vault code is {secrets.token_hex(3).upper()}"
ORION_QUESTION = "What is the Orion vault code?"
HANDBOOK_QUESTION = "How many annual leave days do employees receive?"
PDF_QUESTION = "How long are customer records retained?"


def token(sub, *, key=None, minutes=5, algorithm=ALGORITHM, **extra) -> str:
    now = datetime.now(timezone.utc)
    claims = {"sub": str(sub), "typ": "access", "iat": now, "exp": now + timedelta(minutes=minutes), **extra}
    return jwt.encode(claims, key or os.environ["JWT_SECRET_KEY"], algorithm=algorithm)


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        cls.pipeline = app.state.pipeline
        ensure_sample_corpus(cls.pipeline.embedding_model)  # the handbook + PDF policy these tests ask about
        cls.conn = connect()
        register_vector(cls.conn)
        cls.default = get_tenant(cls.conn, DEFAULT_TENANT)
        cls.created_tenant_ids, cls.created_user_ids = [], []

        # A user of the existing default tenant.
        cls.default_user, cls.default_password = make_user(cls.conn, cls.default.id, "auth-default")
        cls.created_user_ids.append(cls.default_user.id)

        # A self-registered organization with one private document.
        cls.org_email, cls.org_password = unique_email("auth-org"), new_password()
        response = cls.client.post("/api/v1/auth/register", json={
            "organization_name": "Orion Test Labs", "email": cls.org_email,
            "password": cls.org_password, "display_name": "Orion Admin"})
        assert response.status_code == 201, response.text
        cls.registered = response.json()
        cls.org_tenant = get_tenant(cls.conn, cls.registered["tenant"]["slug"])
        cls.created_tenant_ids.append(cls.org_tenant.id)

        cls.tmp = tempfile.TemporaryDirectory()
        folder = Path(cls.tmp.name)
        (folder / "vault.md").write_text(f"# Orion Vault\n\n{SECRET_FACT}. It rotates every quarter.\n", encoding="utf-8")
        documents = load_documents(folder)
        chunks = {d.relative_path: chunk_document(d) for d in documents}
        stats = sync_documents(cls.conn, cls.pipeline.embedding_model, documents, chunks, 500, 100,
                               tenant_id=cls.org_tenant.id)
        assert not stats.failed, stats.failed

        cls.default_token = login(cls.client, cls.default_user.email, cls.default_password)
        cls.org_token = login(cls.client, cls.org_email, cls.org_password)

    @classmethod
    def tearDownClass(cls):
        cls.conn.execute("DELETE FROM documents WHERE tenant_id = ANY(%s)", (cls.created_tenant_ids,))
        cls.conn.execute("DELETE FROM users WHERE id = ANY(%s)", (cls.created_user_ids,))
        cls.conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", (cls.created_tenant_ids,))  # cascades their users
        cls.conn.close()
        cls.tmp.cleanup()
        cls.client_context.__exit__(None, None, None)

    def setUp(self):
        self.real_generate, self.real_client = self.pipeline.generate, self.pipeline.gemini_client
        self.pipeline.generate = lambda client, question, sources: "unused"  # never a real Gemini call
        self.pipeline.gemini_client = self.real_client or object()

    def tearDown(self):
        self.pipeline.generate, self.pipeline.gemini_client = generate_answer, self.real_client

    def query(self, token_value, **body):
        return self.client.post(QUERY, json={"retrieve_only": True, **body}, headers=bearer(token_value))

    # 1. registration -------------------------------------------------------------
    def test_registration_creates_a_tenant_and_stores_only_a_password_hash(self):
        self.assertEqual(self.registered["email"], self.org_email)
        self.assertEqual(self.registered["tenant"]["name"], "Orion Test Labs")
        self.assertTrue(self.registered["tenant"]["slug"].startswith("orion-test-labs-"))
        self.assertNotIn("password", str(self.registered).lower())
        stored = self.conn.execute(
            "SELECT password_hash, tenant_id FROM users WHERE email = %s", (self.org_email,)).fetchone()
        self.assertNotEqual(stored[0], self.org_password)
        self.assertTrue(stored[0].startswith("$argon2id$"))
        self.assertEqual(stored[1], self.org_tenant.id)

    def test_duplicate_email_is_rejected_without_creating_a_tenant(self):
        tenants_before = self.conn.execute("SELECT count(*) FROM tenants").fetchone()[0]
        response = self.client.post("/api/v1/auth/register", json={
            "organization_name": "Copycat", "email": self.org_email.upper(), "password": new_password()})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "email_already_registered")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM tenants").fetchone()[0], tenants_before)

    def test_registration_validation(self):
        for payload in (
            {"organization_name": "X Corp", "email": "not-an-email", "password": new_password()},
            {"organization_name": "X Corp", "email": unique_email("v"), "password": "short"},
            {"organization_name": " ", "email": unique_email("v"), "password": new_password()},
            {"organization_name": "X Corp", "email": unique_email("v"), "password": new_password(), "tenant": "default"},
        ):
            with self.subTest(payload=str(payload)[:70]):
                self.assertEqual(self.client.post("/api/v1/auth/register", json=payload).status_code, 422)

    # 2. login -------------------------------------------------------------------------
    def test_login_returns_a_bearer_token_carrying_only_the_user_id(self):
        response = self.client.post("/api/v1/auth/login", json={"email": self.org_email, "password": self.org_password})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["token_type"], "bearer")
        self.assertEqual(body["expires_in"], 3600)
        claims = jwt.decode(body["access_token"], os.environ["JWT_SECRET_KEY"], algorithms=[ALGORITHM])
        self.assertEqual(set(claims), {"sub", "typ", "iat", "exp"})  # no tenant in the token
        me = self.client.get("/api/v1/auth/me", headers=bearer(body["access_token"]))
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["tenant"]["slug"], self.org_tenant.slug)

    def test_login_email_is_case_insensitive(self):
        response = self.client.post("/api/v1/auth/login",
                                    json={"email": f"  {self.org_email.upper()} ", "password": self.org_password})
        self.assertEqual(response.status_code, 200)

    # 3. invalid credentials ---------------------------------------------------------
    def test_wrong_password_and_unknown_email_get_the_same_401(self):
        wrong = self.client.post("/api/v1/auth/login", json={"email": self.org_email, "password": "wrong-" + new_password()})
        unknown = self.client.post("/api/v1/auth/login", json={"email": unique_email("nobody"), "password": new_password()})
        for response in (wrong, unknown):
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["error"]["code"], "invalid_credentials")
        self.assertEqual(wrong.json(), unknown.json())

    # 4. missing / invalid tokens ----------------------------------------------------
    def test_query_and_me_require_a_token(self):
        requests = {
            QUERY: lambda: self.client.post(QUERY, json={"question": ORION_QUESTION}),
            "/api/v1/auth/me": lambda: self.client.get("/api/v1/auth/me"),
        }
        for path, send in requests.items():
            response = send()
            self.assertEqual(response.status_code, 401, path)
            self.assertEqual(response.json()["error"]["code"], "not_authenticated")
            self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")

    def test_invalid_tokens_are_rejected(self):
        deleted_user, _ = make_user(self.conn, self.default.id, "auth-deleted")
        self.conn.execute("DELETE FROM users WHERE id = %s", (deleted_user.id,))
        cases = {
            "garbage": ("not.a.jwt", "invalid_token"),
            "wrong signing key": (token(self.default_user.id, key=secrets.token_urlsafe(64)), "invalid_token"),
            "expired": (token(self.default_user.id, minutes=-1), "token_expired"),
            "alg none": (jwt.encode({"sub": str(self.default_user.id), "typ": "access",
                                     "iat": datetime.now(timezone.utc),
                                     "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}, None, algorithm="none"),
                         "invalid_token"),
            "wrong type": (token(self.default_user.id, typ="refresh"), "invalid_token"),
            "non-numeric subject": (token("admin"), "invalid_token"),
            "deleted user": (token(deleted_user.id), "invalid_token"),
        }
        for name, (value, code) in cases.items():
            with self.subTest(case=name):
                response = self.query(value, question=ORION_QUESTION)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"]["code"], code)

    # 5. authenticated query -----------------------------------------------------------
    def test_authenticated_query_returns_the_users_tenant_documents(self):
        response = self.query(self.org_token, question=ORION_QUESTION)
        self.assertEqual(response.status_code, 200)
        sources = response.json()["sources"]
        self.assertIn(SECRET_FACT, sources[0]["text"])
        self.assertEqual({s["relative_path"] for s in sources}, {"vault.md"})

    # 6. cross-tenant access -----------------------------------------------------------
    def test_users_never_see_another_tenants_documents(self):
        # The default tenant's user cannot reach the organization's secret...
        default_text = "\n".join(s["text"] for s in self.query(self.default_token, question=ORION_QUESTION, top_k=10).json()["sources"])
        self.assertNotIn(SECRET_FACT, default_text)
        # ...and the organization's user cannot reach the default corpus, even with a question it answers.
        org_sources = self.query(self.org_token, question=HANDBOOK_QUESTION, top_k=10).json()["sources"]
        self.assertEqual({s["relative_path"] for s in org_sources}, {"vault.md"})

    # 7. the tenant cannot be overridden by the client -----------------------------------
    def test_tenant_cannot_be_chosen_in_the_request(self):
        for field, value in (("tenant", DEFAULT_TENANT), ("tenant_id", self.default.id), ("tenant_slug", DEFAULT_TENANT)):
            with self.subTest(field=field):
                response = self.query(self.org_token, question=HANDBOOK_QUESTION, **{field: value})
                self.assertEqual(response.status_code, 422)
        # Unknown query parameters and headers are ignored: still only the user's own tenant.
        response = self.client.post(f"{QUERY}?tenant={DEFAULT_TENANT}&tenant_id={self.default.id}",
                                    json={"question": HANDBOOK_QUESTION, "retrieve_only": True, "top_k": 10},
                                    headers={**bearer(self.org_token), "X-Tenant": DEFAULT_TENANT})
        self.assertEqual(response.status_code, 200)
        self.assertEqual({s["relative_path"] for s in response.json()["sources"]}, {"vault.md"})

    def test_tenant_claims_added_to_a_token_are_ignored(self):
        # Even a validly signed token carrying tenant claims cannot move a user to another tenant.
        forged = token(self.registered["id"], tenant_id=self.default.id, tenant=DEFAULT_TENANT)
        sources = self.query(forged, question=HANDBOOK_QUESTION, top_k=10).json()["sources"]
        self.assertEqual({s["relative_path"] for s in sources}, {"vault.md"})

    # 8. existing RAG behavior is unchanged ---------------------------------------------
    def test_default_tenant_results_match_the_retriever_directly(self):
        paths = [r[0] for r in self.conn.execute(
            "SELECT relative_path FROM documents WHERE tenant_id = %s", (self.default.id,))]
        # The previous API path (every tenant path listed) must match the new one (SQL scope, no paths).
        direct = RerankingRetriever(self.conn, self.pipeline.embedding_model, paths, self.pipeline.reranker,
                                    scope=AccessScope.operator(self.default.id))
        for question in (HANDBOOK_QUESTION, PDF_QUESTION):
            with self.subTest(question=question):
                expected = direct.search(question, top_k=5)
                sources = self.query(self.default_token, question=question, top_k=5).json()["sources"]
                self.assertEqual([s["chunk_id"] for s in sources], [r.chunk_id for r in expected])
                self.assertEqual([s["similarity"] for s in sources], [round(r.score, 4) for r in expected])
                self.assertEqual([s["page_number"] for s in sources], [r.page_number for r in expected])

    def test_generation_and_citations_unchanged_for_authenticated_users(self):
        self.pipeline.generate = lambda client, question, sources: "Employees receive 20 days of annual leave [1]. Made up [7]."
        response = self.client.post(QUERY, json={"question": HANDBOOK_QUESTION}, headers=bearer(self.default_token))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"question", "answer", "citations", "sources", "removed_citations", "llm_model", "timings"})
        self.assertEqual(body["answer"], "Employees receive 20 days of annual leave [1]. Made up.")
        self.assertEqual(body["removed_citations"], [7])
        self.assertEqual(body["citations"][0]["chunk_id"], "sample_company_handbook.md#2")


if __name__ == "__main__":
    unittest.main()
