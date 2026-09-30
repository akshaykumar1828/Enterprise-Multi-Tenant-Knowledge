"""End-to-end authorization through the whole stack, in throwaway tenants only.

Company "Northwind" (its own tenant) is built the way a real deployment would be:
the operator promotes the admin (users set-role), the admin creates departments,
memberships and document access through the Admin API, documents are ingested
with the real embedding model, and every check goes through the HTTP API:

    login -> /auth/me -> document list/pagination -> /query (vector -> reranker ->
    final LLM context) -> upload/duplicates/delete -> admin changes

Users: admin; eng (Engineering); fin (Finance); both (Engineering + Finance).
Documents (all answer the same question, each with its own secret):
    company.md             company-wide
    engineering/runbook.md Engineering only
    finance/forecast.md    Finance only
    board/minutes.md       'departments' with no departments -> admins only
A second tenant ("Other") has its own document and user.

The reranker is wrapped by a spy and the LLM is replaced by a recorder, so the
tests see exactly which chunk texts reach each stage. No Gemini calls. The real
corpus (default tenant) is checked to be unchanged after every test.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_e2e_authorization -v
"""

import os
import secrets
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import bearer, login, make_user  # noqa: I001  (sets the test JWT secret first)

from fastapi.testclient import TestClient
from pgvector.psycopg import register_vector

from src.api.main import app
from src.rag.chunking import chunk_document
from src.rag.db import connect
from src.rag.ingest import sync_documents
from src.rag.llm import generate_answer
from src.rag.loader import load_documents
from src.rag.tenants import DEFAULT_TENANT, get_or_create_tenant, get_tenant
from src.rag.users import set_role_by_email

ADMIN = "/api/v1/admin"
DOCS = "/api/v1/documents"
QUERY = "/api/v1/query"
SUFFIX = secrets.token_hex(4)
QUESTION = "What is the Heron access code?"
FILES = {  # relative path -> (label, secret)
    "company.md": ("the whole company", f"COMPANY-{secrets.token_hex(3).upper()}"),
    "engineering/runbook.md": ("engineering", f"ENGINEERING-{secrets.token_hex(3).upper()}"),
    "finance/forecast.md": ("finance", f"FINANCE-{secrets.token_hex(3).upper()}"),
    "board/minutes.md": ("the board", f"BOARD-{secrets.token_hex(3).upper()}"),
}
OTHER_SECRET = f"OTHER-{secrets.token_hex(3).upper()}"
SECRET = {path: secret for path, (_, secret) in FILES.items()}
EVERYTHING = set(FILES)
EXPECTED = {
    "admin": EVERYTHING,
    "eng": {"company.md", "engineering/runbook.md"},
    "fin": {"company.md", "finance/forecast.md"},
    "both": {"company.md", "engineering/runbook.md", "finance/forecast.md"},
}


def write_documents(folder: Path, files: dict[str, tuple[str, str]]) -> None:
    for relative_path, (label, secret) in files.items():
        path = folder / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Heron access ({label})\n\nThe Heron access code for {label} is {secret}. "
                        "Rotate the Heron access code every quarter.\n", encoding="utf-8")


class SpyReranker:
    """Records every chunk text handed to the cross-encoder, then delegates to the real one."""

    def __init__(self, real):
        self.real, self.seen = real, []

    def predict(self, pairs, **kwargs):
        self.seen.extend(text for _, text in pairs)
        return self.real.predict(pairs, **kwargs)

    def __getattr__(self, name):  # .model etc. for the health endpoint
        return getattr(self.real, name)


class EndToEndAuthorizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = tempfile.mkdtemp(prefix="rag-e2e-authz-")
        cls.env = mock.patch.dict(os.environ, {"UPLOAD_DIR": str(Path(cls.sandbox) / "uploads"),
                                               "TENANT_MAX_DOCUMENTS": "0", "TENANT_MAX_UPLOAD_BYTES": "0"})
        cls.env.start()
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        cls.pipeline = app.state.pipeline
        cls.conn = connect()
        register_vector(cls.conn)
        cls.default = get_tenant(cls.conn, DEFAULT_TENANT).id
        cls.real_corpus = cls.corpus_fingerprint()

        # Tenants, documents (real ingestion and embeddings).
        cls.tenant = get_or_create_tenant(cls.conn, f"test-e2e-northwind-{SUFFIX}", "Northwind").id
        cls.other = get_or_create_tenant(cls.conn, f"test-e2e-other-{SUFFIX}", "Other Co").id
        for tenant_id, files, folder in ((cls.tenant, FILES, "northwind"),
                                         (cls.other, {"company.md": ("other co", OTHER_SECRET)}, "other")):
            write_documents(Path(cls.sandbox) / folder, files)
            documents = load_documents(Path(cls.sandbox) / folder)
            stats = sync_documents(cls.conn, cls.pipeline.embedding_model, documents,
                                   {d.relative_path: chunk_document(d) for d in documents}, 500, 100, tenant_id=tenant_id)
            assert not stats.failed, stats.failed
        cls.doc = {path: cls.conn.execute("SELECT id FROM documents WHERE tenant_id = %s AND relative_path = %s",
                                          (cls.tenant, path)).fetchone()[0] for path in FILES}
        cls.other_doc = cls.conn.execute("SELECT id FROM documents WHERE tenant_id = %s", (cls.other,)).fetchone()[0]
        cls.baseline_documents = set(cls.doc.values()) | {cls.other_doc}

        # Users; the operator bootstrap makes the admin (no public way to become one).
        cls.tokens, cls.ids, cls.emails = {}, {}, {}
        for name, tenant_id in (("admin", cls.tenant), ("eng", cls.tenant), ("fin", cls.tenant), ("both", cls.tenant),
                                ("other", cls.other)):
            user, password = make_user(cls.conn, tenant_id, f"e2e-{name}")
            cls.ids[name], cls.emails[name] = user.id, user.email
            cls.tokens[name] = login(cls.client, user.email, password)
        set_role_by_email(cls.conn, cls.emails["admin"], "admin")
        set_role_by_email(cls.conn, cls.emails["other"], "admin")

        # The admin sets up the company through the Admin API.
        admin = bearer(cls.tokens["admin"])
        cls.dept = {}
        for slug, name in (("engineering", "Engineering"), ("finance", "Finance")):
            response = cls.client.post(f"{ADMIN}/departments", headers=admin, json={"slug": slug, "name": name})
            assert response.status_code == 201, response.text
            cls.dept[slug] = response.json()["id"]
        for user, departments in (("eng", ["engineering"]), ("fin", ["finance"]), ("both", ["engineering", "finance"])):
            for department in departments:
                assert cls.client.put(f"{ADMIN}/users/{cls.ids[user]}/departments/{cls.dept[department]}",
                                      headers=admin).status_code == 200
        cls.baseline_access = {
            "company.md": ("company", []),
            "engineering/runbook.md": ("departments", [cls.dept["engineering"]]),
            "finance/forecast.md": ("departments", [cls.dept["finance"]]),
            "board/minutes.md": ("departments", []),
        }
        for path, (visibility, departments) in cls.baseline_access.items():
            response = cls.client.put(f"{ADMIN}/documents/{cls.doc[path]}/access", headers=admin,
                                      json={"visibility": visibility, "department_ids": departments})
            assert response.status_code == 200, response.text
        cls.baseline_memberships = cls.memberships()

        # Observe the reranker and the LLM (no Gemini).
        cls.real_reranker = cls.pipeline.reranker
        cls.spy = SpyReranker(cls.real_reranker)
        cls.pipeline.reranker = cls.spy
        cls.real_client = cls.pipeline.gemini_client
        cls.pipeline.gemini_client = cls.real_client or object()
        cls.llm_contexts = []

        def fake_llm(client, question, sources):
            cls.llm_contexts.append([s.text for s in sources])
            return "The Heron access code is in the sources [1]."
        cls.pipeline.generate = fake_llm

    @classmethod
    def tearDownClass(cls):
        cls.pipeline.reranker = cls.real_reranker
        cls.pipeline.generate, cls.pipeline.gemini_client = generate_answer, cls.real_client
        ids = [cls.tenant, cls.other]
        cls.conn.execute("DELETE FROM documents WHERE tenant_id = ANY(%s)", (ids,))
        cls.conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", (ids,))  # users, departments, links cascade
        assert cls.corpus_fingerprint() == cls.real_corpus, "the real corpus changed"
        cls.conn.close()
        cls.client_context.__exit__(None, None, None)
        cls.env.stop()
        shutil.rmtree(cls.sandbox, ignore_errors=True)

    def setUp(self):
        """Every test starts from the company as the admin set it up."""
        self.conn.execute("UPDATE users SET role = CASE WHEN id = %s THEN 'admin' ELSE 'employee' END WHERE tenant_id = %s",
                          (self.ids["admin"], self.tenant))
        self.conn.execute("DELETE FROM user_departments WHERE tenant_id = %s", (self.tenant,))
        for user_id, department_id in self.baseline_memberships:
            self.conn.execute("INSERT INTO user_departments (tenant_id, user_id, department_id) VALUES (%s, %s, %s)",
                              (self.tenant, user_id, department_id))
        self.conn.execute("DELETE FROM document_departments WHERE tenant_id = %s", (self.tenant,))
        for path, (visibility, departments) in self.baseline_access.items():
            self.conn.execute("UPDATE documents SET visibility = %s WHERE id = %s", (visibility, self.doc[path]))
            for department_id in departments:
                self.conn.execute("INSERT INTO document_departments (tenant_id, document_id, department_id) "
                                  "VALUES (%s, %s, %s)", (self.tenant, self.doc[path], department_id))
        self.spy.seen.clear()
        self.llm_contexts.clear()

    def tearDown(self):
        # Remove whatever a test uploaded; the real corpus must be untouched.
        for (stored,) in self.conn.execute(
                "DELETE FROM documents WHERE tenant_id = ANY(%s) AND NOT (id = ANY(%s)) RETURNING path",
                ([self.tenant, self.other], list(self.baseline_documents))).fetchall():
            Path(stored).unlink(missing_ok=True)
        self.assertEqual(self.corpus_fingerprint(), self.real_corpus)

    # --- helpers -------------------------------------------------------------------------------
    @classmethod
    def corpus_fingerprint(cls) -> tuple:
        return cls.conn.execute(
            """SELECT (SELECT count(*) FROM documents WHERE tenant_id = %(t)s),
                      (SELECT count(*) FROM document_chunks WHERE tenant_id = %(t)s),
                      (SELECT max(id) FROM document_chunks WHERE tenant_id = %(t)s),
                      (SELECT count(*) FROM documents WHERE tenant_id = %(t)s AND visibility <> 'company'),
                      (SELECT count(*) FROM document_departments WHERE tenant_id = %(t)s)""",
            {"t": cls.default}).fetchone()

    @classmethod
    def memberships(cls) -> list[tuple[int, int]]:
        return cls.conn.execute("SELECT user_id, department_id FROM user_departments WHERE tenant_id = %s ORDER BY 1, 2",
                                (cls.tenant,)).fetchall()

    def admin_call(self, method: str, path: str, body=None, user: str = "admin"):
        return self.client.request(method, f"{ADMIN}{path}", json=body, headers=bearer(self.tokens[user]))

    def ask(self, user: str, retrieve_only: bool = False) -> dict:
        response = self.client.post(QUERY, headers=bearer(self.tokens[user]),
                                    json={"question": QUESTION, "top_k": 10, "retrieve_only": retrieve_only})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def retrieved(self, user: str) -> set[str]:
        return {s["relative_path"] for s in self.ask(user, retrieve_only=True)["sources"]}

    def listed(self, user: str) -> set[str]:
        body = self.client.get(DOCS, headers=bearer(self.tokens[user]), params={"limit": 200}).json()
        by_id = {v: k for k, v in self.doc.items()}
        names = {by_id.get(item["id"], item["filename"]) for item in body["items"]}
        self.assertEqual(body["total"], len(body["items"]))
        return names

    def assert_sees(self, user: str, expected: set[str]):
        self.assertEqual(self.retrieved(user), expected, f"{user}: retrieval")
        self.assertEqual(self.listed(user), expected, f"{user}: document list")

    # --- 1. login -> /auth/me ------------------------------------------------------------------------
    def test_me_reports_role_and_departments(self):
        expected = {"admin": ("admin", []), "eng": ("employee", ["Engineering"]), "fin": ("employee", ["Finance"]),
                    "both": ("employee", ["Engineering", "Finance"])}
        for user, (role, departments) in expected.items():
            with self.subTest(user=user):
                me = self.client.get("/api/v1/auth/me", headers=bearer(self.tokens[user])).json()
                self.assertEqual((me["role"], [d["name"] for d in me["departments"]]), (role, departments))
                self.assertEqual(me["tenant"]["name"], "Northwind")

    # --- 2. listing, pagination -------------------------------------------------------------------------
    def test_document_list_and_pagination_follow_the_same_rule(self):
        for user, expected in EXPECTED.items():
            with self.subTest(user=user):
                self.assertEqual(self.listed(user), expected)
                seen, offset, by_id = [], 0, {v: k for k, v in self.doc.items()}
                while True:
                    page = self.client.get(DOCS, headers=bearer(self.tokens[user]),
                                           params={"limit": 1, "offset": offset}).json()
                    self.assertEqual(page["total"], len(expected))
                    if not page["items"]:
                        break
                    seen.append(by_id[page["items"][0]["id"]])
                    offset += 1
                self.assertEqual(sorted(seen), sorted(expected))

    # --- 3. retrieval -> reranker -> final LLM context ------------------------------------------------------
    def test_every_stage_sees_only_authorized_chunks(self):
        for user, expected in EXPECTED.items():
            with self.subTest(user=user):
                self.spy.seen.clear()
                self.llm_contexts.clear()
                body = self.ask(user)  # full RAG answer (fake LLM)
                hidden = EVERYTHING - expected
                self.assertEqual({s["relative_path"] for s in body["sources"]}, expected)
                self.assertEqual(body["answer"], "The Heron access code is in the sources [1].")
                reranked = "\n".join(self.spy.seen)
                llm = "\n".join(self.llm_contexts[0])
                for path in expected:
                    self.assertIn(SECRET[path], reranked)
                    self.assertIn(SECRET[path], llm)
                for path in hidden:
                    self.assertNotIn(SECRET[path], reranked, f"{path} reached the reranker")
                    self.assertNotIn(SECRET[path], llm, f"{path} reached the LLM")
                self.assertNotIn(OTHER_SECRET, reranked + llm)

    def test_engineering_and_finance_cannot_read_each_other(self):
        self.assertNotIn("finance/forecast.md", self.retrieved("eng"))
        self.assertNotIn("engineering/runbook.md", self.retrieved("fin"))
        self.assertTrue({"engineering/runbook.md", "finance/forecast.md"} <= self.retrieved("both"))
        for user in ("eng", "fin", "both"):
            self.assertIn("company.md", self.retrieved(user))
            self.assertNotIn("board/minutes.md", self.retrieved(user))
        self.assertIn("board/minutes.md", self.retrieved("admin"))

    # --- 4. upload, duplicates, delete ----------------------------------------------------------------------
    def test_upload_duplicates_and_delete(self):
        payroll = b"# Payroll\n\nThe Heron payroll token is PAYROLL-" + secrets.token_hex(3).upper().encode() + b".\n"
        uploaded = self.client.post(DOCS, headers=bearer(self.tokens["admin"]),
                                    files={"file": ("payroll.md", payroll, "text/markdown")})
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        payroll_id = uploaded.json()["id"]
        self.assertIn(payroll_id, {d["id"] for d in self.client.get(DOCS, headers=bearer(self.tokens["eng"]),
                                                                        params={"limit": 200}).json()["items"]})
        self.assertEqual(self.admin_call("PUT", f"/documents/{payroll_id}/access",
                                         {"visibility": "departments", "department_ids": [self.dept["finance"]]}).status_code, 200)

        # Finance can read it: the existing duplicate answer, with its id.
        visible = self.client.post(DOCS, headers=bearer(self.tokens["fin"]), files={"file": ("copy.md", payroll, "text/markdown")})
        self.assertEqual((visible.status_code, visible.json()["error"]["message"]),
                         (409, f"This file was already uploaded (document {payroll_id})."))
        # Engineering cannot: the same bytes behave like a brand-new file.
        hidden = self.client.post(DOCS, headers=bearer(self.tokens["eng"]), files={"file": ("copy.md", payroll, "text/markdown")})
        self.assertEqual(hidden.status_code, 201, hidden.text)
        self.assertNotEqual(hidden.json()["id"], payroll_id)
        self.assertEqual(set(hidden.json()), {"id", "filename", "source_type", "origin", "chunk_count", "ingested_at", "deletable"})

        # Delete: an unreadable document looks exactly like a missing one and is untouched.
        missing = self.client.delete(f"{DOCS}/{2**62}", headers=bearer(self.tokens["eng"]))
        for target in (payroll_id, self.doc["finance/forecast.md"], self.doc["board/minutes.md"], self.other_doc):
            with self.subTest(target=target):
                response = self.client.delete(f"{DOCS}/{target}", headers=bearer(self.tokens["eng"]))
                self.assertEqual((response.status_code, response.json()), (404, missing.json()))
        remaining = {r[0] for r in self.conn.execute("SELECT id FROM documents WHERE id = ANY(%s)",
                                                     ([payroll_id, self.doc["finance/forecast.md"], self.other_doc],))}
        self.assertEqual(remaining, {payroll_id, self.doc["finance/forecast.md"], self.other_doc})
        # Readable documents keep the existing rules: folder documents 409, own readable upload 204.
        self.assertEqual(self.client.delete(f"{DOCS}/{self.doc['company.md']}", headers=bearer(self.tokens["eng"])).status_code, 409)
        self.assertEqual(self.client.delete(f"{DOCS}/{hidden.json()['id']}", headers=bearer(self.tokens["eng"])).status_code, 204)
        self.assertEqual(self.client.delete(f"{DOCS}/{payroll_id}", headers=bearer(self.tokens["fin"])).status_code, 204)

    # --- 5. immediate permission changes (same tokens throughout) ---------------------------------------------
    def test_membership_changes_apply_on_the_next_request(self):
        self.assert_sees("eng", EXPECTED["eng"])
        self.assertEqual(self.admin_call("PUT", f"/users/{self.ids['eng']}/departments/{self.dept['finance']}").status_code, 200)
        self.assert_sees("eng", EXPECTED["both"])
        self.assertEqual(self.admin_call("DELETE", f"/users/{self.ids['eng']}/departments/{self.dept['finance']}").status_code, 200)
        self.assert_sees("eng", EXPECTED["eng"])

    def test_restricting_and_reopening_a_document_applies_immediately(self):
        self.assert_sees("fin", EXPECTED["fin"])
        self.admin_call("PUT", f"/documents/{self.doc['company.md']}/access",
                        {"visibility": "departments", "department_ids": [self.dept["engineering"]]})
        self.assert_sees("fin", {"finance/forecast.md"})
        self.assert_sees("eng", EXPECTED["eng"])
        self.admin_call("PUT", f"/documents/{self.doc['company.md']}/access", {"visibility": "company"})
        self.assert_sees("fin", EXPECTED["fin"])

    def test_removing_all_departments_makes_a_document_admin_only(self):
        self.admin_call("PUT", f"/documents/{self.doc['finance/forecast.md']}/access",
                        {"visibility": "departments", "department_ids": []})
        self.assert_sees("fin", {"company.md"})
        self.assert_sees("both", {"company.md", "engineering/runbook.md"})
        self.assert_sees("admin", EVERYTHING)

    def test_promotion_and_demotion_apply_immediately(self):
        self.assertEqual(self.admin_call("GET", "/users", user="fin").status_code, 403)
        self.assert_sees("fin", EXPECTED["fin"])
        self.admin_call("PUT", f"/users/{self.ids['fin']}/role", {"role": "admin"})
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=bearer(self.tokens["fin"])).json()["role"], "admin")
        self.assertEqual(self.admin_call("GET", "/users", user="fin").status_code, 200)
        self.assert_sees("fin", EVERYTHING)
        self.admin_call("PUT", f"/users/{self.ids['fin']}/role", {"role": "employee"})
        self.assertEqual(self.admin_call("GET", "/users", user="fin").status_code, 403)
        self.assert_sees("fin", EXPECTED["fin"])

    # --- 6. cross-tenant -------------------------------------------------------------------------------
    def test_cross_tenant_access_is_impossible(self):
        self.assertEqual(self.retrieved("other"), {"company.md"})  # its own company.md only
        other_text = "\n".join(s["text"] for s in self.ask("other", retrieve_only=True)["sources"])
        self.assertIn(OTHER_SECRET, other_text)
        for secret in SECRET.values():
            self.assertNotIn(secret, other_text)
        for user in EXPECTED:
            text = "\n".join(s["text"] for s in self.ask(user, retrieve_only=True)["sources"])
            self.assertNotIn(OTHER_SECRET, text)
        # The Northwind admin cannot touch the other tenant's document, user or listing.
        unknown = self.admin_call("GET", f"/documents/{2**62}/access").json()
        self.assertEqual(self.admin_call("GET", f"/documents/{self.other_doc}/access").json(), unknown)
        self.assertEqual(self.admin_call("PUT", f"/documents/{self.other_doc}/access", {"visibility": "company"}).status_code, 404)
        self.assertEqual(self.admin_call("PUT", f"/users/{self.ids['other']}/role", {"role": "employee"}).status_code, 404)
        self.assertNotIn(self.ids["other"], {u["id"] for u in self.admin_call("GET", "/users").json()["items"]})
        self.assertEqual(self.conn.execute("SELECT role FROM users WHERE id = %s", (self.ids["other"],)).fetchone()[0], "admin")


if __name__ == "__main__":
    unittest.main()
