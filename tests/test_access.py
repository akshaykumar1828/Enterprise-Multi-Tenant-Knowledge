"""Employee-level authorization in retrieval: roles, departments, visibility.

Tenant A holds four documents that all answer the same question, each with its
own secret:
    company     visibility 'company'                      -> every user of A
    finance     'departments', assigned to Finance        -> Finance members, admins
    engineering 'departments', assigned to Engineering    -> Engineering members, admins
    orphan      'departments', assigned to no department  -> admins only
Tenant B has its own company and Finance documents; tenant C only an orphan
document; tenant D nothing at all. Every retriever (vector, reranking,
hybrid/lexical), the pipeline and the API are checked. Candidate queries are
called directly with a limit far above the corpus size, which shows the
filtering happens in SQL rather than after LIMIT. No Gemini calls.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_access -v
"""

import inspect
import os
import secrets
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.helpers import bearer, login, make_user  # noqa: I001  (sets the test JWT secret first)

import jwt
from fastapi.testclient import TestClient
from pgvector.psycopg import register_vector

from src.api.auth import ALGORITHM
from src.api.main import app
from src.rag.access import AccessScope, load_access_scope
from src.rag.chunking import chunk_document
from src.rag.db import connect
from src.rag.embeddings import embed_texts
from src.rag.ingest import sync_documents
from src.rag.loader import load_documents
from src.rag.pipeline import CorpusEmpty, RAGPipeline
from src.rag.retriever import HybridRetriever, PgVectorRetriever, RerankingRetriever
from src.rag.tenants import get_or_create_tenant

QUERY = "/api/v1/query"
QUESTION = "What is the Kestrel vault code?"
SUFFIX = secrets.token_hex(4)
SECRETS = {name: f"{name.upper()}-{secrets.token_hex(3).upper()}"
           for name in ("company", "finance", "engineering", "orphan", "b_company", "b_finance", "c_orphan")}
EVERYTHING_IN_A = {"company", "finance", "engineering", "orphan"}
FAR_ABOVE_CORPUS = 100_000


def document_text(group: str, secret: str) -> str:
    return (f"# Kestrel vault ({group})\n\nThe Kestrel vault code for {group} is {secret}. "
            "Keep the Kestrel vault code private.\n")


def ingest(conn, model, tenant_id: int, files: dict[str, tuple[str, str]], folder: Path) -> None:
    """files: relative path -> (group label, secret)."""
    for relative_path, (group, secret) in files.items():
        path = folder / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document_text(group, secret), encoding="utf-8")
    documents = load_documents(folder)
    chunks = {d.relative_path: chunk_document(d) for d in documents}
    stats = sync_documents(conn, model, documents, chunks, 500, 100, tenant_id=tenant_id)
    assert not stats.failed, stats.failed


def forged_token(user_id: int, **claims) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": str(user_id), "typ": "access", "iat": now, "exp": now + timedelta(minutes=5), **claims},
                      os.environ["JWT_SECRET_KEY"], algorithm=ALGORITHM)


class SpyReranker:
    """Records every text handed to the cross-encoder, then delegates to the real one."""

    def __init__(self, real):
        self.real, self.seen = real, []

    def predict(self, pairs, **kwargs):
        self.seen.extend(text for _, text in pairs)
        return self.real.predict(pairs, **kwargs)


class AccessControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        cls.pipeline = app.state.pipeline
        cls.model, cls.reranker = cls.pipeline.embedding_model, cls.pipeline.reranker
        cls.conn = connect()
        register_vector(cls.conn)
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)

        cls.tenant = {key: get_or_create_tenant(cls.conn, f"test-access-{key}-{SUFFIX}", f"Access {key.upper()}").id
                      for key in ("a", "b", "c", "d")}
        a, b, c = cls.tenant["a"], cls.tenant["b"], cls.tenant["c"]

        ingest(cls.conn, cls.model, a, {
            "company.md": ("the company", SECRETS["company"]),
            "finance/budget.md": ("finance", SECRETS["finance"]),
            "engineering/deploy.md": ("engineering", SECRETS["engineering"]),
            "hr/orphan.md": ("human resources", SECRETS["orphan"]),
        }, root / "a")
        ingest(cls.conn, cls.model, b, {
            "company.md": ("tenant b", SECRETS["b_company"]),
            "finance/budget.md": ("tenant b finance", SECRETS["b_finance"]),
        }, root / "b")
        ingest(cls.conn, cls.model, c, {"restricted.md": ("tenant c", SECRETS["c_orphan"])}, root / "c")

        cls.dept = {
            "a_finance": cls.add_department(a, "finance"),
            "a_engineering": cls.add_department(a, "engineering"),
            "b_finance": cls.add_department(b, "finance"),
        }
        cls.restrict(a, "finance/budget.md", [cls.dept["a_finance"]])
        cls.restrict(a, "engineering/deploy.md", [cls.dept["a_engineering"]])
        cls.restrict(a, "hr/orphan.md", [])
        cls.restrict(b, "finance/budget.md", [cls.dept["b_finance"]])
        cls.restrict(c, "restricted.md", [])

        cls.users, cls.passwords = {}, {}
        for name, tenant_key, role, departments in (
            ("admin", "a", "admin", []),
            ("finance", "a", "employee", ["a_finance"]),
            ("engineering", "a", "employee", ["a_engineering"]),
            ("both", "a", "employee", ["a_finance", "a_engineering"]),
            ("nobody", "a", "employee", []),
            ("b_admin", "b", "admin", []),
            ("b_finance", "b", "employee", ["b_finance"]),
            ("c_employee", "c", "employee", []),
            ("c_admin", "c", "admin", []),
            ("d_employee", "d", "employee", []),
        ):
            user, password = make_user(cls.conn, cls.tenant[tenant_key], f"access-{name}")
            cls.conn.execute("UPDATE users SET role = %s WHERE id = %s", (role, user.id))
            for department in departments:
                cls.conn.execute("INSERT INTO user_departments (tenant_id, user_id, department_id) VALUES (%s, %s, %s)",
                                 (cls.tenant[tenant_key], user.id, cls.dept[department]))
            cls.users[name], cls.passwords[name] = user, password

    @classmethod
    def tearDownClass(cls):
        ids = list(cls.tenant.values())
        cls.conn.execute("DELETE FROM documents WHERE tenant_id = ANY(%s)", (ids,))  # chunks, assignments cascade
        cls.conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", (ids,))  # users, departments, memberships cascade
        cls.conn.close()
        cls.tmp.cleanup()
        cls.client_context.__exit__(None, None, None)

    @classmethod
    def add_department(cls, tenant_id: int, slug: str) -> int:
        return cls.conn.execute("INSERT INTO departments (tenant_id, slug, name) VALUES (%s, %s, %s) RETURNING id",
                                (tenant_id, slug, slug.title())).fetchone()[0]

    @classmethod
    def restrict(cls, tenant_id: int, relative_path: str, department_ids: list[int]) -> None:
        document_id = cls.conn.execute(
            "UPDATE documents SET visibility = 'departments' WHERE tenant_id = %s AND relative_path = %s RETURNING id",
            (tenant_id, relative_path)).fetchone()[0]
        for department_id in department_ids:
            cls.conn.execute("INSERT INTO document_departments (tenant_id, document_id, department_id) VALUES (%s, %s, %s)",
                             (tenant_id, document_id, department_id))

    # --- helpers --------------------------------------------------------------------------
    def scope(self, name: str) -> AccessScope:
        return load_access_scope(self.conn, self.users[name].id)

    def retrievers(self, scope: AccessScope, paths=None) -> dict:
        return {
            "vector": PgVectorRetriever(self.conn, self.model, paths, scope=scope),
            "reranking": RerankingRetriever(self.conn, self.model, paths, self.reranker, scope=scope),
            "hybrid": HybridRetriever(self.conn, self.model, paths, scope=scope),
        }

    def all_candidate_text(self, scope: AccessScope, paths=None) -> str:
        """Every chunk the SQL candidate queries return, with a LIMIT far above the corpus size."""
        retriever = HybridRetriever(self.conn, self.model, paths, scope=scope)
        vector = embed_texts(self.model, [QUESTION])[0]
        rows = retriever.vector_candidates(vector, FAR_ABOVE_CORPUS) + \
            retriever.lexical_candidates(QUESTION, vector, FAR_ABOVE_CORPUS)
        return "\n".join(r.text for r in rows)

    def assert_sees_exactly(self, scope: AccessScope, visible: set[str], paths=None):
        hidden = set(SECRETS) - visible
        for kind, retriever in self.retrievers(scope, paths).items():
            with self.subTest(retriever=kind):
                text = "\n".join(r.text for r in retriever.search(QUESTION, top_k=10))
                for name in visible:
                    self.assertIn(SECRETS[name], text, f"{kind} should return {name}")
                for name in hidden:
                    self.assertNotIn(SECRETS[name], text, f"{kind} must not return {name}")
        with self.subTest(retriever="raw SQL candidates"):
            text = self.all_candidate_text(scope, paths)
            for name in hidden:
                self.assertNotIn(SECRETS[name], text)

    def api_texts(self, token: str) -> str:
        response = self.client.post(QUERY, headers=bearer(token),
                                    json={"question": QUESTION, "top_k": 10, "retrieve_only": True})
        self.assertEqual(response.status_code, 200, response.text)
        return "\n".join(s["text"] for s in response.json()["sources"])

    # --- the scope ----------------------------------------------------------------------------
    def test_scope_is_loaded_from_the_database(self):
        both = self.scope("both")
        self.assertEqual(both.tenant_id, self.tenant["a"])
        self.assertEqual(both.user_id, self.users["both"].id)
        self.assertEqual(both.role, "employee")
        self.assertFalse(both.is_admin)
        self.assertEqual(both.department_ids, tuple(sorted((self.dept["a_finance"], self.dept["a_engineering"]))))
        self.assertTrue(self.scope("admin").is_admin)
        self.assertEqual(self.scope("nobody").department_ids, ())
        self.assertIsNone(load_access_scope(self.conn, 2**62))

    def test_scope_is_a_required_keyword_argument(self):
        for cls in (PgVectorRetriever, RerankingRetriever, HybridRetriever):
            with self.subTest(retriever=cls.__name__):
                parameter = inspect.signature(cls).parameters["scope"]
                self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
                self.assertIs(parameter.default, inspect.Parameter.empty)
                self.assertNotIn("tenant_id", inspect.signature(cls).parameters)
        for method in (RAGPipeline.retrieve, RAGPipeline.answer):
            with self.subTest(method=method.__name__):
                parameter = inspect.signature(method).parameters["scope"]
                self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
                self.assertIs(parameter.default, inspect.Parameter.empty)

    # --- department rules ------------------------------------------------------------------
    def test_department_a_cannot_retrieve_department_b_documents(self):
        self.assert_sees_exactly(self.scope("finance"), {"company", "finance"})
        self.assert_sees_exactly(self.scope("engineering"), {"company", "engineering"})

    def test_employee_with_several_departments_sees_each_of_them(self):
        self.assert_sees_exactly(self.scope("both"), {"company", "finance", "engineering"})

    def test_company_documents_are_visible_to_every_employee(self):
        for name in ("finance", "engineering", "both", "nobody"):
            with self.subTest(user=name):
                texts = "\n".join(r.text for r in PgVectorRetriever(
                    self.conn, self.model, None, scope=self.scope(name)).search(QUESTION, top_k=10))
                self.assertIn(SECRETS["company"], texts)
        self.assert_sees_exactly(self.scope("nobody"), {"company"})

    def test_admin_sees_every_document_of_their_tenant(self):
        self.assert_sees_exactly(self.scope("admin"), EVERYTHING_IN_A)

    def test_restricted_document_without_departments_is_admin_only(self):
        for name in ("finance", "engineering", "both", "nobody"):
            with self.subTest(user=name):
                self.assertNotIn(SECRETS["orphan"], self.all_candidate_text(self.scope(name)))
        self.assertIn(SECRETS["orphan"], self.all_candidate_text(self.scope("admin")))

    # --- tenant isolation ------------------------------------------------------------------
    def test_tenant_isolation_still_holds(self):
        self.assert_sees_exactly(self.scope("b_admin"), {"b_company", "b_finance"})
        self.assert_sees_exactly(self.scope("b_finance"), {"b_company", "b_finance"})
        # A tenant-A scope naming tenant B's department still sees nothing of tenant B.
        finance = self.scope("finance")
        forged = AccessScope(tenant_id=finance.tenant_id, user_id=finance.user_id, role="employee",
                             department_ids=(self.dept["b_finance"],))
        self.assert_sees_exactly(forged, {"company"})

    # --- document paths only ever narrow a search ---------------------------------------------
    def test_document_paths_cannot_widen_access(self):
        every_path = [r[0] for r in self.conn.execute("SELECT relative_path FROM documents WHERE tenant_id = ANY(%s)",
                                                      (list(self.tenant.values()),))]
        finance = self.scope("finance")
        self.assert_sees_exactly(finance, {"company", "finance"}, paths=every_path)
        for forbidden in (["engineering/deploy.md"], ["hr/orphan.md"], ["engineering/deploy.md", "hr/orphan.md"]):
            with self.subTest(paths=forbidden):
                self.assertEqual(self.all_candidate_text(finance, forbidden), "")
                self.assertEqual(PgVectorRetriever(self.conn, self.model, forbidden, scope=finance)
                                 .search(QUESTION, top_k=10), [])
        # Narrowing still works for documents the user may read.
        only = PgVectorRetriever(self.conn, self.model, ["finance/budget.md"], scope=finance).search(QUESTION, top_k=10)
        self.assertEqual({r.relative_path for r in only}, {"finance/budget.md"})

    # --- the reranker only ever sees authorized text ----------------------------------------------
    def test_unauthorized_chunk_text_never_reaches_the_reranker(self):
        spy = SpyReranker(self.reranker)
        RerankingRetriever(self.conn, self.model, None, spy, scope=self.scope("finance")).search(QUESTION, top_k=10)
        seen = "\n".join(spy.seen)
        self.assertIn(SECRETS["finance"], seen)
        for name in ("engineering", "orphan", "b_company", "b_finance", "c_orphan"):
            self.assertNotIn(SECRETS[name], seen)

    def test_lexical_retrieval_cannot_bypass_authorization(self):
        retriever = HybridRetriever(self.conn, self.model, None, scope=self.scope("engineering"))
        vector = embed_texts(self.model, [QUESTION])[0]
        # Searching literally for the other departments' secrets finds nothing.
        for name in ("finance", "orphan", "b_finance"):
            with self.subTest(secret=name):
                self.assertEqual(retriever.lexical_candidates(SECRETS[name], vector, FAR_ABOVE_CORPUS), [])
        own = retriever.lexical_candidates(SECRETS["engineering"], vector, FAR_ABOVE_CORPUS)
        self.assertEqual({r.relative_path for r in own}, {"engineering/deploy.md"})

    # --- empty authorized corpus ----------------------------------------------------------------
    def test_empty_authorized_corpus_behaves_like_an_empty_corpus(self):
        with self.assertRaises(CorpusEmpty) as restricted:
            self.pipeline.retrieve(QUESTION, 5, scope=self.scope("c_employee"))
        with self.assertRaises(CorpusEmpty) as empty:
            self.pipeline.retrieve(QUESTION, 5, scope=self.scope("d_employee"))
        self.assertEqual(str(restricted.exception), str(empty.exception))

        body = {"question": QUESTION, "top_k": 5, "retrieve_only": True}
        responses = {name: self.client.post(QUERY, json=body, headers=bearer(login(
            self.client, self.users[name].email, self.passwords[name]))) for name in ("c_employee", "d_employee")}
        self.assertEqual(responses["c_employee"].status_code, 503)
        self.assertEqual(responses["c_employee"].json(), responses["d_employee"].json())
        self.assertEqual(responses["c_employee"].json()["error"]["code"], "corpus_empty")
        # The admin of tenant C does see the restricted document.
        admin = self.pipeline.retrieve(QUESTION, 5, scope=self.scope("c_admin"))
        self.assertIn(SECRETS["c_orphan"], "\n".join(r.text for r in admin))

    # --- the API: permissions come from the database on every request -------------------------------
    def test_api_applies_the_users_database_permissions(self):
        tokens = {name: login(self.client, self.users[name].email, self.passwords[name])
                  for name in ("admin", "finance", "nobody")}
        admin = self.api_texts(tokens["admin"])
        for name in EVERYTHING_IN_A:
            self.assertIn(SECRETS[name], admin)
        finance = self.api_texts(tokens["finance"])
        self.assertIn(SECRETS["finance"], finance)
        for name in ("engineering", "orphan", "b_company", "b_finance", "c_orphan"):
            self.assertNotIn(SECRETS[name], finance)

        # A membership change applies to the very next request with the same token.
        user_id = self.users["nobody"].id
        self.assertNotIn(SECRETS["engineering"], self.api_texts(tokens["nobody"]))
        self.conn.execute("INSERT INTO user_departments (tenant_id, user_id, department_id) VALUES (%s, %s, %s)",
                          (self.tenant["a"], user_id, self.dept["a_engineering"]))
        try:
            self.assertIn(SECRETS["engineering"], self.api_texts(tokens["nobody"]))
        finally:
            self.conn.execute("DELETE FROM user_departments WHERE user_id = %s", (user_id,))
        self.assertNotIn(SECRETS["engineering"], self.api_texts(tokens["nobody"]))

    def test_permission_claims_in_a_token_are_ignored(self):
        forged = forged_token(self.users["nobody"].id, role="admin",
                              department_ids=[self.dept["a_finance"], self.dept["a_engineering"]])
        text = self.api_texts(forged)
        self.assertIn(SECRETS["company"], text)
        for name in ("finance", "engineering", "orphan"):
            self.assertNotIn(SECRETS[name], text)


if __name__ == "__main__":
    unittest.main()
