"""Knowledge-base tests: upload, list, delete, isolation and failure cleanup. No Gemini calls.

Uploads go to a temporary UPLOAD_DIR; every test tenant, user, document and
file is removed afterwards.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_documents -v
"""

import os
import secrets
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import bearer, login, make_user, new_password, unique_email  # noqa: I001  (test JWT secret first)

import psycopg
from fastapi.testclient import TestClient

from src.api.main import app
from src.rag.db import connect
from src.rag.llm import generate_answer
from src.rag.tenants import DEFAULT_TENANT, get_tenant

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCS = "/api/v1/documents"
QUERY = "/api/v1/query"
SAMPLE_PDF = (PROJECT_ROOT / "data" / "documents" / "sample_it_security_policy.pdf").read_bytes()


def markdown(secret: str) -> bytes:
    return (f"# Nebula Expense Handbook\n\n## Travel\n\nThe Nebula quarterly travel budget code is {secret}. "
            "Budgets reset on the first day of each quarter.\n").encode()


def text_file(secret: str) -> bytes:
    return f"Harbor office notes\n\nThe Harbor loading dock access phrase is {secret}.\n".encode()


class DocumentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A private sandbox with the upload root inside it, so "nothing escaped the
        # upload root" can be checked without noise from the shared temp folder.
        cls.sandbox = tempfile.mkdtemp(prefix="rag-uploads-test-")
        cls.upload_dir = str(Path(cls.sandbox) / "uploads")
        os.environ["UPLOAD_DIR"] = cls.upload_dir
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        cls.pipeline = app.state.pipeline
        cls.conn = connect()
        cls.default = get_tenant(cls.conn, DEFAULT_TENANT)
        cls.default_counts = cls.counts(cls.default.id)
        cls.tenant_ids, cls.user_ids = [], []

        cls.tokens, cls.tenants = {}, {}
        for key in ("a", "b"):
            email, password = unique_email(f"docs-{key}"), new_password()
            response = cls.client.post("/api/v1/auth/register", json={
                "organization_name": f"Docs Test {key.upper()}", "email": email, "password": password})
            assert response.status_code == 201, response.text
            tenant = get_tenant(cls.conn, response.json()["tenant"]["slug"])
            cls.tenant_ids.append(tenant.id)
            cls.tenants[key] = tenant
            cls.tokens[key] = login(cls.client, email, password)

        user, password = make_user(cls.conn, cls.default.id, "docs-default")
        cls.user_ids.append(user.id)
        cls.tokens["default"] = login(cls.client, user.email, password)

    @classmethod
    def tearDownClass(cls):
        cls.conn.execute("DELETE FROM documents WHERE tenant_id = ANY(%s)", (cls.tenant_ids,))  # chunks cascade
        cls.conn.execute("DELETE FROM users WHERE id = ANY(%s)", (cls.user_ids,))
        cls.conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", (cls.tenant_ids,))  # cascades their users
        cls.conn.close()
        cls.client_context.__exit__(None, None, None)
        shutil.rmtree(cls.sandbox, ignore_errors=True)
        os.environ.pop("UPLOAD_DIR", None)

    def setUp(self):
        self.pipeline.generate = lambda client, question, sources: "unused"  # never a real Gemini call
        self.real_client = self.pipeline.gemini_client
        self.pipeline.gemini_client = self.real_client or object()

    def tearDown(self):
        self.pipeline.generate, self.pipeline.gemini_client = generate_answer, self.real_client
        # The default tenant's corpus must be untouched by every test.
        self.assertEqual(self.counts(self.default.id), self.default_counts)

    # --- helpers ------------------------------------------------------------------
    @classmethod
    def counts(cls, tenant_id: int) -> tuple[int, int]:
        return cls.conn.execute(
            "SELECT (SELECT count(*) FROM documents WHERE tenant_id = %s),"
            "       (SELECT count(*) FROM document_chunks WHERE tenant_id = %s)", (tenant_id, tenant_id)).fetchone()

    def upload(self, key: str, filename: str, data: bytes, content_type: str = "application/octet-stream"):
        return self.client.post(DOCS, files={"file": (filename, data, content_type)}, headers=bearer(self.tokens[key]))

    def stored_files(self, key: str) -> list[Path]:
        folder = Path(self.upload_dir) / str(self.tenants[key].id)
        return sorted(p for p in folder.glob("*") if p.is_file()) if folder.exists() else []

    def ask(self, key: str, question: str, top_k: int = 5):
        response = self.client.post(QUERY, json={"question": question, "top_k": top_k, "retrieve_only": True},
                                    headers=bearer(self.tokens[key]))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["sources"]

    def delete_all_uploads(self, key: str) -> None:
        for item in self.client.get(DOCS, headers=bearer(self.tokens[key])).json()["items"]:
            self.client.delete(f"{DOCS}/{item['id']}", headers=bearer(self.tokens[key]))

    # 1, 2, 11. upload each supported type and retrieve it immediately ------------------
    def test_upload_markdown_text_and_pdf_then_retrieve_them(self):
        secret_md, secret_txt = secrets.token_hex(4).upper(), secrets.token_hex(4).upper()
        cases = [
            ("handbook.md", markdown(secret_md), "What is the Nebula quarterly travel budget code?", secret_md),
            ("dock-notes.txt", text_file(secret_txt), "What is the Harbor loading dock access phrase?", secret_txt),
            ("security-policy.pdf", SAMPLE_PDF, "How long are customer records retained?", "7 years"),
        ]
        try:
            for filename, data, question, expected in cases:
                with self.subTest(file=filename):
                    response = self.upload("a", filename, data)
                    self.assertEqual(response.status_code, 201, response.text)
                    body = response.json()
                    self.assertEqual(body["filename"], filename)
                    self.assertEqual((body["origin"], body["source_type"], body["deletable"]), ("upload", "upload", True))
                    self.assertGreater(body["chunk_count"], 0)
                    top = self.ask("a", question)[0]
                    self.assertEqual(top["source"], filename)
                    self.assertIn(expected, top["text"])
            # The PDF keeps its page numbers through upload.
            pdf_top = self.ask("a", "How long are customer records retained?")[0]
            self.assertEqual(pdf_top["page_number"], 3)
            self.assertEqual(len(self.stored_files("a")), 3)
        finally:
            self.delete_all_uploads("a")

    def test_uploaded_document_works_with_answers_and_citations(self):
        secret = secrets.token_hex(4).upper()
        self.assertEqual(self.upload("a", "handbook.md", markdown(secret)).status_code, 201)
        try:
            self.pipeline.generate = lambda client, question, sources: f"The code is {secret} [1]."
            response = self.client.post(QUERY, json={"question": "What is the Nebula quarterly travel budget code?"},
                                        headers=bearer(self.tokens["a"]))
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["answer"], f"The code is {secret} [1].")
            self.assertEqual(body["citations"][0]["source"], "handbook.md")
            self.assertEqual(body["citations"][0]["source_type"], "upload")
        finally:
            self.delete_all_uploads("a")

    # 3. tenant isolation ------------------------------------------------------------
    def test_uploads_are_invisible_to_other_tenants(self):
        secret = secrets.token_hex(4).upper()
        self.assertEqual(self.upload("a", "handbook.md", markdown(secret)).status_code, 201)
        try:
            for other in ("b", "default"):
                with self.subTest(tenant=other):
                    listed = self.client.get(DOCS, headers=bearer(self.tokens[other])).json()["items"]
                    self.assertNotIn("handbook.md", [d["filename"] for d in listed if d["origin"] == "upload"])
                    if other == "b":
                        # Tenant B has no documents at all, so its query finds nothing to search.
                        response = self.client.post(QUERY, headers=bearer(self.tokens["b"]), json={
                            "question": "What is the Nebula quarterly travel budget code?", "retrieve_only": True})
                        self.assertEqual(response.status_code, 503)
                        self.assertEqual(response.json()["error"]["code"], "corpus_empty")
                    else:
                        texts = "\n".join(s["text"] for s in self.ask(other, "What is the Nebula quarterly travel budget code?", 10))
                        self.assertNotIn(secret, texts)
        finally:
            self.delete_all_uploads("a")

    # 4. unauthenticated access ---------------------------------------------------------
    def test_upload_list_and_delete_require_authentication(self):
        requests = {
            "upload": lambda: self.client.post(DOCS, files={"file": ("x.md", b"# x\n\ntext", "text/markdown")}),
            "list": lambda: self.client.get(DOCS),
            "delete": lambda: self.client.delete(f"{DOCS}/1"),
            "upload with bad token": lambda: self.client.post(
                DOCS, files={"file": ("x.md", b"# x\n\ntext", "text/markdown")}, headers=bearer("not.a.token")),
        }
        for name, send in requests.items():
            with self.subTest(request=name):
                response = send()
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(self.stored_files("a"), [])

    # 5. cross-tenant deletion ------------------------------------------------------------
    def test_a_tenant_cannot_delete_another_tenants_document(self):
        secret = secrets.token_hex(4).upper()
        document_id = self.upload("a", "handbook.md", markdown(secret)).json()["id"]
        try:
            response = self.client.delete(f"{DOCS}/{document_id}", headers=bearer(self.tokens["b"]))
            self.assertEqual(response.status_code, 404)  # indistinguishable from "does not exist"
            response = self.client.delete(f"{DOCS}/{document_id}", headers=bearer(self.tokens["default"]))
            self.assertEqual(response.status_code, 404)
            self.assertIn(secret, self.ask("a", "What is the Nebula quarterly travel budget code?")[0]["text"])
            self.assertEqual(len(self.stored_files("a")), 1)
        finally:
            self.delete_all_uploads("a")

    def test_folder_managed_documents_cannot_be_deleted_through_the_api(self):
        # A folder-ingested document of tenant A (this test's own; nothing is assumed about
        # what the default tenant holds).
        document_id = self.conn.execute(
            """INSERT INTO documents (tenant_id, relative_path, source, source_type, path, content_hash, chunk_size,
                                      chunk_overlap, embedding_model, embedding_input_version, origin)
               VALUES (%s, 'managed.md', 'managed.md', 'local', 'managed.md', repeat('0', 64), 500, 100,
                       'test-model', 'test', 'folder') RETURNING id""", (self.tenants["a"].id,)).fetchone()[0]
        try:
            listed = {d["id"]: d for d in self.client.get(DOCS, headers=bearer(self.tokens["a"])).json()["items"]}
            self.assertEqual((listed[document_id]["origin"], listed[document_id]["deletable"]), ("folder", False))
            response = self.client.delete(f"{DOCS}/{document_id}", headers=bearer(self.tokens["a"]))
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["error"]["code"], "document_managed_by_ingestion")
            self.assertEqual(self.conn.execute("SELECT count(*) FROM documents WHERE id = %s", (document_id,)).fetchone()[0], 1)
        finally:
            self.conn.execute("DELETE FROM documents WHERE id = %s", (document_id,))
        # tearDown also checks the default corpus is unchanged.

    # 6. invalid file types ------------------------------------------------------------
    def test_invalid_file_types_are_rejected(self):
        cases = {
            "program.exe": (b"MZ\x90\x00binary", 415),
            "report.docx": (b"PK\x03\x04zip", 415),
            "no-extension": (b"just text", 415),
            "fake.pdf": (b"this is plain text pretending to be a PDF", 415),
            "binary.txt": (b"text\x00with\x00nul", 415),
            "latin1.md": ("caf\xe9 men\xfa".encode("latin-1"), 415),
            "empty.md": (b"   \n  ", 422),
            "handbook.md.exe": (b"# not markdown", 415),
        }
        for filename, (data, status) in cases.items():
            with self.subTest(file=filename):
                response = self.upload("a", filename, data, "text/markdown")  # the client's content type is ignored
                self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(self.stored_files("a"), [])
        self.assertEqual(self.counts(self.tenants["a"].id), (0, 0))

    # 7. oversized files -----------------------------------------------------------------
    def test_oversized_files_are_rejected(self):
        with mock.patch.dict(os.environ, {"UPLOAD_MAX_BYTES": "2000"}):
            # Slightly over the limit: caught by the route's own size check.
            response = self.upload("a", "big.md", b"# Big\n\n" + b"x" * 3000)
            self.assertEqual(response.status_code, 413)
            self.assertEqual(response.json()["error"]["code"], "file_too_large")
            # Far over the limit: refused from Content-Length before the body is read.
            response = self.upload("a", "huge.md", b"# Huge\n\n" + b"x" * 200_000)
            self.assertEqual(response.status_code, 413)
        self.assertEqual(self.stored_files("a"), [])

    # 8. failed ingestion leaves nothing behind ------------------------------------------
    def test_unreadable_pdf_is_cleaned_up(self):
        response = self.upload("a", "broken.pdf", b"%PDF-1.4\nthis is not really a pdf\n%%EOF")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "unreadable_document")
        self.assertEqual(self.stored_files("a"), [])
        self.assertEqual(self.counts(self.tenants["a"].id), (0, 0))

    def test_database_failure_during_ingestion_is_cleaned_up(self):
        with mock.patch("src.rag.uploads.store_document", side_effect=psycopg.OperationalError("simulated failure")):
            response = self.upload("a", "handbook.md", markdown("ROLLBACK"))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "ingestion_failed")
        self.assertEqual(self.stored_files("a"), [])
        self.assertEqual(self.counts(self.tenants["a"].id), (0, 0))

    def test_failure_inside_the_database_transaction_rolls_back(self):
        # Fail after the document row is written but before chunks are: the whole transaction must roll back.
        from src.rag import ingest

        with mock.patch.object(ingest, "lexical_parts", side_effect=psycopg.DataError("simulated mid-transaction failure")):
            response = self.upload("a", "handbook.md", markdown("MIDWAY"))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.counts(self.tenants["a"].id), (0, 0))
        self.assertEqual(self.stored_files("a"), [])

    # 9. listing ------------------------------------------------------------------------
    def test_listing_is_scoped_paginated_and_newest_first(self):
        # What the default user sees before tenant A uploads anything (whatever that tenant holds).
        default_before = self.client.get(DOCS, headers=bearer(self.tokens["default"])).json()["total"]
        ids = [self.upload("a", f"note-{i}.md", markdown(f"LIST{i}{secrets.token_hex(2)}")).json()["id"] for i in range(3)]
        try:
            body = self.client.get(DOCS, headers=bearer(self.tokens["a"])).json()
            self.assertEqual(body["total"], 3)
            self.assertEqual([d["id"] for d in body["items"]], list(reversed(ids)))
            self.assertEqual(set(body["items"][0]), {"id", "filename", "source_type", "origin", "chunk_count",
                                                     "ingested_at", "deletable"})
            page = self.client.get(DOCS, headers=bearer(self.tokens["a"]), params={"limit": 2, "offset": 2}).json()
            self.assertEqual((page["total"], len(page["items"]), page["items"][0]["id"]), (3, 1, ids[0]))
            self.assertEqual(self.client.get(DOCS, headers=bearer(self.tokens["b"])).json()["total"], 0)
            default = self.client.get(DOCS, headers=bearer(self.tokens["default"])).json()
            self.assertEqual(default["total"], default_before)  # tenant A's uploads never appear there
            self.assertEqual(self.client.get(DOCS, headers=bearer(self.tokens["a"]), params={"limit": 500}).status_code, 422)
        finally:
            self.delete_all_uploads("a")

    # 10. deletion ------------------------------------------------------------------------
    def test_deleting_removes_the_document_its_chunks_and_its_file(self):
        secret = secrets.token_hex(4).upper()
        document_id = self.upload("a", "handbook.md", markdown(secret)).json()["id"]
        self.assertEqual(len(self.stored_files("a")), 1)

        response = self.client.delete(f"{DOCS}/{document_id}", headers=bearer(self.tokens["a"]))
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.counts(self.tenants["a"].id), (0, 0))
        self.assertEqual(self.stored_files("a"), [])
        self.assertEqual(self.client.delete(f"{DOCS}/{document_id}", headers=bearer(self.tokens["a"])).status_code, 404)

    # security: filenames and duplicates -------------------------------------------------
    def test_path_traversal_filenames_are_neutralized(self):
        outside = Path(self.sandbox)
        before = {p for p in outside.rglob("*") if Path(self.upload_dir) not in (p, *p.parents)}
        names = ["../../../evil.md", "..\\..\\evil.md", "C:\\Windows\\System32\\drivers\\evil.md", "/etc/evil.md",
                 ".hidden.md", "we<i>rd:na*me?.md"]
        try:
            for index, name in enumerate(names):
                with self.subTest(name=name):
                    response = self.upload("a", name, markdown(f"TRAVERSAL{index}"))
                    self.assertEqual(response.status_code, 201, response.text)
                    filename = response.json()["filename"]
                    self.assertNotIn("/", filename)
                    self.assertNotIn("\\", filename)
                    self.assertNotIn("..", filename)
                    self.assertFalse(filename.startswith("."))
            stored = self.stored_files("a")
            self.assertEqual(len(stored), len(names))
            root = Path(self.upload_dir).resolve()
            for path in stored:
                self.assertEqual(path.resolve().parent, root / str(self.tenants["a"].id))
                self.assertRegex(path.name, r"^[0-9a-f]{32}\.md$")  # server-generated name, never the client's
            after = {p for p in outside.rglob("*") if Path(self.upload_dir) not in (p, *p.parents)}
            self.assertEqual(after, before)  # nothing written anywhere outside the upload root
        finally:
            self.delete_all_uploads("a")

    def test_duplicate_upload_is_rejected(self):
        data = markdown(secrets.token_hex(4).upper())
        first = self.upload("a", "handbook.md", data)
        try:
            again = self.upload("a", "copy.md", data)
            self.assertEqual(again.status_code, 409)
            self.assertEqual(again.json()["error"]["code"], "duplicate_document")
            # The same bytes are fine in a different tenant.
            other = self.upload("b", "handbook.md", data)
            self.assertEqual(other.status_code, 201)
        finally:
            self.delete_all_uploads("a")
            self.delete_all_uploads("b")
        self.assertEqual(first.status_code, 201)

    def test_tenant_cannot_be_chosen_for_uploads(self):
        response = self.client.post(DOCS, files={"file": ("a.md", markdown("X1"), "text/markdown")},
                                    data={"tenant_id": str(self.default.id), "tenant": DEFAULT_TENANT},
                                    headers=bearer(self.tokens["b"]))
        try:
            self.assertEqual(response.status_code, 201)
            # It went to tenant B (the caller), not to the default tenant.
            self.assertEqual(self.client.get(DOCS, headers=bearer(self.tokens["b"])).json()["total"], 1)
        finally:
            self.delete_all_uploads("b")


if __name__ == "__main__":
    unittest.main()
