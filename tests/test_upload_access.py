"""Upload authorization: duplicate checks and limit errors never reveal unreadable documents.

An admin of tenant A uploads four files; three are then restricted in the
database (the API cannot restrict documents yet):
    company.md      'company'                        -> everyone in A
    finance.md      'departments' -> Finance
    engineering.md  'departments' -> Engineering
    orphan.md       'departments', no departments    -> admins only
Tenant B's admin uploads its own file. Uploading an identical file must give
the existing 409 only when the uploader may read the original; otherwise it
behaves exactly like a new file and the original (row, chunks, file) is never
touched. Limit errors stay tenant-wide but carry no figures for employees.
No Gemini calls.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_upload_access -v
"""

import hashlib
import os
import re
import secrets
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import bearer, login, make_user  # noqa: I001  (sets the test JWT secret first)

from fastapi.testclient import TestClient

from src.api.main import app
from src.rag.db import connect
from src.rag.tenants import get_or_create_tenant

DOCS = "/api/v1/documents"
SUFFIX = secrets.token_hex(4)
UNLIMITED = {"TENANT_MAX_DOCUMENTS": "0", "TENANT_MAX_UPLOAD_BYTES": "0"}


def content(label: str) -> bytes:
    return (f"# {label} handbook\n\nThe {label} Osprey ledger phrase is {secrets.token_hex(6).upper()}. "
            "It changes every month.\n").encode()


class UploadAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = tempfile.mkdtemp(prefix="rag-upload-access-test-")
        cls.upload_dir = Path(cls.sandbox) / "uploads"
        cls.env = mock.patch.dict(os.environ, {"UPLOAD_DIR": str(cls.upload_dir), **UNLIMITED})
        cls.env.start()
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        cls.conn = connect()

        cls.tenant = {key: get_or_create_tenant(cls.conn, f"test-upload-access-{key}-{SUFFIX}", f"Upload access {key}").id
                      for key in ("a", "b")}
        a = cls.tenant["a"]
        cls.dept = {name: cls.conn.execute(
            "INSERT INTO departments (tenant_id, slug, name) VALUES (%s, %s, %s) RETURNING id",
            (a, name, name.title())).fetchone()[0] for name in ("finance", "engineering")}

        cls.tokens = {}
        for name, tenant_key, role, departments in (
            ("admin", "a", "admin", []),
            ("finance", "a", "employee", ["finance"]),
            ("engineering", "a", "employee", ["engineering"]),
            ("nobody", "a", "employee", []),
            ("b_admin", "b", "admin", []),
        ):
            user, password = make_user(cls.conn, cls.tenant[tenant_key], f"upload-access-{name}")
            cls.conn.execute("UPDATE users SET role = %s WHERE id = %s", (role, user.id))
            for department in departments:
                cls.conn.execute("INSERT INTO user_departments (tenant_id, user_id, department_id) VALUES (%s, %s, %s)",
                                 (a, user.id, cls.dept[department]))
            cls.tokens[name] = login(cls.client, user.email, password)

        cls.data, cls.docs = {}, {}
        for name, uploader, departments in (
            ("company.md", "admin", None),
            ("finance.md", "admin", ["finance"]),
            ("engineering.md", "admin", ["engineering"]),
            ("orphan.md", "admin", []),
            ("b.md", "b_admin", None),
        ):
            cls.data[name] = content(name.split(".")[0])
            response = cls.client.post(DOCS, headers=bearer(cls.tokens[uploader]),
                                       files={"file": (name, cls.data[name], "text/markdown")})
            assert response.status_code == 201, response.text
            cls.docs[name] = response.json()["id"]
            if departments is not None:
                cls.conn.execute("UPDATE documents SET visibility = 'departments' WHERE id = %s", (cls.docs[name],))
                for department in departments:
                    cls.conn.execute(
                        "INSERT INTO document_departments (tenant_id, document_id, department_id) VALUES (%s, %s, %s)",
                        (a, cls.docs[name], cls.dept[department]))
        cls.original_ids = set(cls.docs.values())
        cls.fingerprints = {name: cls.fingerprint(document_id) for name, document_id in cls.docs.items()}

    @classmethod
    def tearDownClass(cls):
        ids = list(cls.tenant.values())
        cls.conn.execute("DELETE FROM documents WHERE tenant_id = ANY(%s)", (ids,))
        cls.conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", (ids,))  # users, departments, links cascade
        cls.conn.close()
        cls.client_context.__exit__(None, None, None)
        cls.env.stop()
        shutil.rmtree(cls.sandbox, ignore_errors=True)

    def tearDown(self):
        # Remove whatever a test uploaded (rows and files), keeping the original five.
        for (stored,) in self.conn.execute(
                "DELETE FROM documents WHERE tenant_id = ANY(%s) AND NOT (id = ANY(%s)) RETURNING path",
                (list(self.tenant.values()), list(self.original_ids))).fetchall():
            Path(stored).unlink(missing_ok=True)
        # No original document, chunk or file was modified by anything the test did.
        for name, document_id in self.docs.items():
            self.assertEqual(self.fingerprint(document_id), self.fingerprints[name], f"{name} was modified")

    # --- helpers ---------------------------------------------------------------------------------
    @classmethod
    def fingerprint(cls, document_id: int) -> tuple:
        row = cls.conn.execute(
            """SELECT d.tenant_id, d.relative_path, d.source, d.content_hash, d.size_bytes, d.visibility,
                      d.ingested_at, d.path,
                      (SELECT count(*) FROM document_chunks c WHERE c.document_id = d.id),
                      (SELECT md5(string_agg(c.embedding::text || c.text, '' ORDER BY c.chunk_index))
                       FROM document_chunks c WHERE c.document_id = d.id),
                      (SELECT array_agg(department_id ORDER BY department_id)
                       FROM document_departments WHERE document_id = d.id)
               FROM documents d WHERE d.id = %s""", (document_id,)).fetchone()
        assert row is not None, f"document {document_id} disappeared"
        return (*row, hashlib.sha256(Path(row[7]).read_bytes()).hexdigest())

    def upload(self, user: str, name: str, data: bytes):
        return self.client.post(DOCS, headers=bearer(self.tokens[user]), files={"file": (name, data, "text/markdown")})

    def stored_files(self) -> set[Path]:
        return {p for p in self.upload_dir.rglob("*") if p.is_file()}

    def assert_like_a_new_upload(self, response, hidden_name: str):
        """201 with an ordinary new document: nothing about the unreadable original."""
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        # The additive "description" is derived from this new document's own (just uploaded) text.
        self.assertEqual(set(body), {"id", "filename", "source_type", "origin", "chunk_count", "ingested_at", "deletable",
                                     "description"})
        self.assertNotEqual(body["id"], self.docs[hidden_name])
        self.assertNotIn("error", body)

    # --- duplicates ---------------------------------------------------------------------------------
    def test_visible_duplicate_keeps_the_existing_409(self):
        for user, name in (("nobody", "company.md"), ("finance", "company.md"),
                           ("finance", "finance.md"), ("engineering", "engineering.md"), ("admin", "orphan.md")):
            with self.subTest(user=user, document=name):
                files_before = self.stored_files()
                response = self.upload(user, f"copy-of-{name}", self.data[name])
                self.assertEqual(response.status_code, 409)
                error = response.json()["error"]
                self.assertEqual(error["code"], "duplicate_document")
                self.assertEqual(error["message"], f"This file was already uploaded (document {self.docs[name]}).")
                self.assertEqual(self.stored_files(), files_before)  # rejected before anything was stored

    def test_hidden_duplicate_behaves_like_no_duplicate(self):
        # orphan.md is admin-only: to an employee, its bytes are a brand-new file.
        for user in ("nobody", "finance", "engineering"):
            with self.subTest(user=user):
                response = self.upload(user, "orphan.md", self.data["orphan.md"])
                self.assert_like_a_new_upload(response, "orphan.md")
                # Compare with a file that truly exists nowhere: same status and shape.
                fresh = self.upload(user, "fresh.md", content(f"fresh-{user}"))
                self.assertEqual((fresh.status_code, set(fresh.json())), (response.status_code, set(response.json())))
                self.tearDown()  # remove this round's uploads before the next user

    def test_cross_department_duplicate_reveals_nothing(self):
        self.assert_like_a_new_upload(self.upload("finance", "engineering.md", self.data["engineering.md"]),
                                      "engineering.md")
        self.assert_like_a_new_upload(self.upload("engineering", "finance.md", self.data["finance.md"]), "finance.md")

    def test_cross_tenant_duplicate_reveals_nothing(self):
        self.assert_like_a_new_upload(self.upload("b_admin", "company.md", self.data["company.md"]), "company.md")
        self.assert_like_a_new_upload(self.upload("b_admin", "orphan.md", self.data["orphan.md"]), "orphan.md")
        self.assert_like_a_new_upload(self.upload("admin", "b.md", self.data["b.md"]), "b.md")

    def test_a_hidden_duplicate_upload_is_an_ordinary_company_document(self):
        response = self.upload("nobody", "orphan.md", self.data["orphan.md"])
        self.assertEqual(response.status_code, 201)
        visibility, tenant_id = self.conn.execute("SELECT visibility, tenant_id FROM documents WHERE id = %s",
                                                  (response.json()["id"],)).fetchone()
        self.assertEqual((visibility, tenant_id), ("company", self.tenant["a"]))  # current upload semantics
        # A second identical upload by the same user now finds a duplicate it can read.
        again = self.upload("nobody", "orphan.md", self.data["orphan.md"])
        self.assertEqual(again.json()["error"]["message"],
                         f"This file was already uploaded (document {response.json()['id']}).")

    # --- tenant-wide limits ---------------------------------------------------------------------------
    def tenant_usage(self) -> tuple[int, int]:
        return self.conn.execute(
            "SELECT count(*), coalesce(sum(size_bytes), 0) FROM documents WHERE tenant_id = %s AND origin = 'upload'",
            (self.tenant["a"],)).fetchone()

    def test_document_limit_is_tenant_wide_and_reveals_no_counts_to_employees(self):
        count, _ = self.tenant_usage()  # includes restricted documents the employees cannot see
        with mock.patch.dict(os.environ, {"TENANT_MAX_DOCUMENTS": str(count)}):
            files_before = self.stored_files()
            for user in ("nobody", "finance"):
                with self.subTest(user=user):
                    response = self.upload(user, "one-more.md", content(f"limit-{user}"))
                    self.assertEqual(response.status_code, 409)
                    error = response.json()["error"]
                    self.assertEqual(error["code"], "tenant_document_limit_reached")
                    self.assertIsNone(re.search(r"\d", error["message"]), error["message"])
            admin = self.upload("admin", "one-more.md", content("limit-admin")).json()["error"]
            self.assertEqual(admin["code"], "tenant_document_limit_reached")
            self.assertIn(f"limit of {count} uploaded documents", admin["message"])  # admins may see the figures
            self.assertEqual(self.stored_files(), files_before)
        # One more slot and the same employee upload goes through.
        with mock.patch.dict(os.environ, {"TENANT_MAX_DOCUMENTS": str(count + 1)}):
            self.assertEqual(self.upload("nobody", "one-more.md", content("limit-ok")).status_code, 201)

    def test_storage_limit_is_tenant_wide_and_reveals_no_usage_to_employees(self):
        _, used = self.tenant_usage()
        data = content("storage")
        with mock.patch.dict(os.environ, {"TENANT_MAX_UPLOAD_BYTES": str(used + len(data) - 1)}):
            files_before = self.stored_files()
            employee = self.upload("finance", "big.md", data).json()["error"]
            self.assertEqual(employee["code"], "tenant_storage_limit_reached")
            self.assertIsNone(re.search(r"\d", employee["message"]), employee["message"])
            admin = self.upload("admin", "big.md", data).json()["error"]
            self.assertEqual(admin["code"], "tenant_storage_limit_reached")
            self.assertIn("MB used", admin["message"])
            self.assertEqual(self.stored_files(), files_before)
        with mock.patch.dict(os.environ, {"TENANT_MAX_UPLOAD_BYTES": str(used + len(data))}):
            self.assertEqual(self.upload("finance", "big.md", data).status_code, 201)

    def test_duplicate_check_runs_before_limits_only_for_readable_documents(self):
        # At the document limit, a readable duplicate still reports the duplicate; an
        # unreadable one reports the limit, exactly like any other new file.
        count, _ = self.tenant_usage()
        with mock.patch.dict(os.environ, {"TENANT_MAX_DOCUMENTS": str(count)}):
            readable = self.upload("finance", "finance.md", self.data["finance.md"]).json()["error"]["code"]
            hidden = self.upload("finance", "orphan.md", self.data["orphan.md"]).json()["error"]
            new = self.upload("finance", "new.md", content("new")).json()["error"]
        self.assertEqual(readable, "duplicate_document")
        self.assertEqual(hidden, new)


if __name__ == "__main__":
    unittest.main()
