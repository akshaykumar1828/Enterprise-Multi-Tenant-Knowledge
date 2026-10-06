"""Documents API authorization: listing and deleting follow the caller's AccessScope.

Tenant A holds bare document rows (no chunks needed here):
    company.md       folder, 'company'                        -> everyone in A
    finance.md       upload, 'departments' -> Finance         -> Finance members, admins
    engineering.md   upload, 'departments' -> Engineering     -> Engineering members, admins
    orphan.md        upload, 'departments', no departments    -> admins only
    eng-folder.md    folder, 'departments' -> Engineering     -> Engineering members, admins
Tenant B has its own company document. A document the caller may not read must
look exactly like one that does not exist: absent from items, total and
pagination, and a 404 identical to an unknown id on delete. No Gemini calls.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_documents_access -v
"""

import os
import secrets
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.helpers import bearer, login, make_user  # noqa: I001  (sets the test JWT secret first)

from fastapi.testclient import TestClient

from src.api.main import app
from src.rag.db import connect
from src.rag.tenants import get_or_create_tenant

DOCS = "/api/v1/documents"
SUFFIX = secrets.token_hex(4)
EVERYTHING_IN_A = {"company.md", "finance.md", "engineering.md", "orphan.md", "eng-folder.md"}
UNKNOWN_ID = 2**62


class DocumentAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = tempfile.mkdtemp(prefix="rag-docs-access-test-")
        cls.upload_dir = Path(cls.sandbox) / "uploads"
        os.environ["UPLOAD_DIR"] = str(cls.upload_dir)
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        cls.conn = connect()

        cls.tenant = {key: get_or_create_tenant(cls.conn, f"test-docs-access-{key}-{SUFFIX}", f"Docs access {key}").id
                      for key in ("a", "b")}
        a = cls.tenant["a"]
        cls.dept = {name: cls.conn.execute(
            "INSERT INTO departments (tenant_id, slug, name) VALUES (%s, %s, %s) RETURNING id",
            (a, name, name.title())).fetchone()[0] for name in ("finance", "engineering")}

        cls.docs = {}
        for name, origin, departments in (
            ("company.md", "folder", None),
            ("finance.md", "upload", ["finance"]),
            ("engineering.md", "upload", ["engineering"]),
            ("orphan.md", "upload", []),
            ("eng-folder.md", "folder", ["engineering"]),
        ):
            cls.docs[name] = cls.add_document(a, name, origin, departments)
        cls.b_company = cls.add_document(cls.tenant["b"], "b-company.md", "folder", None)

        cls.tokens = {}
        for name, tenant_key, role, departments in (
            ("admin", "a", "admin", []),
            ("finance", "a", "employee", ["finance"]),
            ("engineering", "a", "employee", ["engineering"]),
            ("both", "a", "employee", ["finance", "engineering"]),
            ("nobody", "a", "employee", []),
            ("b_admin", "b", "admin", []),
        ):
            user, password = make_user(cls.conn, cls.tenant[tenant_key], f"docs-access-{name}")
            cls.conn.execute("UPDATE users SET role = %s WHERE id = %s", (role, user.id))
            for department in departments:
                cls.conn.execute("INSERT INTO user_departments (tenant_id, user_id, department_id) VALUES (%s, %s, %s)",
                                 (a, user.id, cls.dept[department]))
            cls.tokens[name] = login(cls.client, user.email, password)

    @classmethod
    def tearDownClass(cls):
        ids = list(cls.tenant.values())
        cls.conn.execute("DELETE FROM documents WHERE tenant_id = ANY(%s)", (ids,))
        cls.conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", (ids,))  # users, departments, links cascade
        cls.conn.close()
        cls.client_context.__exit__(None, None, None)
        shutil.rmtree(cls.sandbox, ignore_errors=True)
        os.environ.pop("UPLOAD_DIR", None)

    @classmethod
    def add_document(cls, tenant_id: int, name: str, origin: str, departments: list[str] | None) -> int:
        """A bare document row; uploads get a real file inside the upload root."""
        path = cls.upload_dir / str(tenant_id) / f"{secrets.token_hex(8)}.md"
        if origin == "upload":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {name}\n", encoding="utf-8")
        relative_path = f"uploads/{secrets.token_hex(4)}/{name}" if origin == "upload" else name
        document_id = cls.conn.execute(
            """INSERT INTO documents (tenant_id, relative_path, source, source_type, path, content_hash, chunk_size,
                                      chunk_overlap, embedding_model, embedding_input_version, origin, visibility)
               VALUES (%s, %s, %s, %s, %s, %s, 500, 100, 'test-model', 'test', %s, %s) RETURNING id""",
            (tenant_id, relative_path, name, "upload" if origin == "upload" else "local", str(path),
             secrets.token_hex(32), origin, "company" if departments is None else "departments")).fetchone()[0]
        for department in departments or []:
            cls.conn.execute("INSERT INTO document_departments (tenant_id, document_id, department_id) VALUES (%s, %s, %s)",
                             (tenant_id, document_id, cls.dept[department]))
        return document_id

    def listing(self, user: str, **params) -> dict:
        response = self.client.get(DOCS, headers=bearer(self.tokens[user]), params={"limit": 200, **params})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def visible(self, user: str) -> set[str]:
        body = self.listing(user)
        names = [item["filename"] for item in body["items"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(body["total"], len(names), "total must count only readable documents")
        return set(names)

    def exists(self, document_id: int) -> bool:
        return self.conn.execute("SELECT count(*) FROM documents WHERE id = %s", (document_id,)).fetchone()[0] == 1

    # --- listing ------------------------------------------------------------------------------
    def test_cross_department_documents_are_hidden(self):
        self.assertTrue(self.visible("finance").isdisjoint({"engineering.md", "eng-folder.md"}))
        self.assertNotIn("finance.md", self.visible("engineering"))

    def test_own_department_documents_are_visible(self):
        self.assertIn("finance.md", self.visible("finance"))
        self.assertTrue({"engineering.md", "eng-folder.md"} <= self.visible("engineering"))

    def test_company_documents_are_visible_to_every_employee(self):
        for user in ("finance", "engineering", "both", "nobody"):
            with self.subTest(user=user):
                self.assertIn("company.md", self.visible(user))

    def test_exact_visible_sets(self):
        expected = {
            "admin": EVERYTHING_IN_A,
            "finance": {"company.md", "finance.md"},
            "engineering": {"company.md", "engineering.md", "eng-folder.md"},
            "both": {"company.md", "finance.md", "engineering.md", "eng-folder.md"},  # multi-department
            "nobody": {"company.md"},
            "b_admin": {"b-company.md"},
        }
        for user, names in expected.items():
            with self.subTest(user=user):
                self.assertEqual(self.visible(user), names)

    def test_restricted_document_without_departments_is_admin_only(self):
        for user in ("finance", "engineering", "both", "nobody"):
            with self.subTest(user=user):
                self.assertNotIn("orphan.md", self.visible(user))
        self.assertIn("orphan.md", self.visible("admin"))

    def test_tenant_isolation(self):
        self.assertNotIn("b-company.md", self.visible("admin"))
        self.assertTrue(self.visible("b_admin").isdisjoint(EVERYTHING_IN_A))

    def test_pagination_never_reveals_hidden_documents(self):
        for user in ("finance", "nobody", "admin"):
            with self.subTest(user=user):
                expected = self.visible(user)
                seen, offset = [], 0
                while True:
                    page = self.listing(user, limit=1, offset=offset)
                    self.assertEqual(page["total"], len(expected))  # the same total on every page
                    if not page["items"]:
                        break
                    seen += [item["filename"] for item in page["items"]]
                    offset += 1
                self.assertEqual(set(seen), expected)
                self.assertEqual(len(seen), len(expected))
                self.assertEqual(self.listing(user, limit=5, offset=len(expected))["items"], [])

    def test_listing_shape_is_unchanged(self):
        body = self.listing("admin")
        self.assertEqual(set(body), {"total", "limit", "offset", "items"})
        self.assertEqual((body["limit"], body["offset"]), (200, 0))
        item = next(i for i in body["items"] if i["filename"] == "finance.md")
        # The original fields, plus the additive (nullable) description.
        self.assertEqual(set(item), {"id", "filename", "source_type", "origin", "chunk_count", "ingested_at", "deletable",
                                     "description"})
        self.assertEqual((item["id"], item["origin"], item["deletable"]), (self.docs["finance.md"], "upload", True))
        self.assertEqual(self.client.get(DOCS).status_code, 401)

    def test_descriptions_come_only_from_readable_documents(self):
        secrets_by_doc = {"finance.md": f"FINANCE-{SUFFIX}", "orphan.md": f"ORPHAN-{SUFFIX}",
                          "company.md": f"COMPANY-{SUFFIX}"}
        zero = "[" + ",".join(["0"] * 384) + "]"
        a = self.tenant["a"]
        inserted = []
        for name, secret in secrets_by_doc.items():
            text = f"{name}\nThis passage describes the {name} document and carries the marker {secret} for this test."
            inserted.append(self.conn.execute(
                """INSERT INTO document_chunks (tenant_id, document_id, chunk_id, chunk_index, text, start_char, end_char,
                                               embedding) VALUES (%s, %s, %s, 0, %s, 0, %s, %s::vector) RETURNING id""",
                (a, self.docs[name], f"{name}#0-{SUFFIX}", text, len(text), zero)).fetchone()[0])
        try:
            for user, readable in (("finance", {"finance.md", "company.md"}), ("engineering", {"company.md"}),
                                   ("nobody", {"company.md"}), ("admin", set(secrets_by_doc))):
                with self.subTest(user=user):
                    raw = self.client.get(DOCS, headers=bearer(self.tokens[user]), params={"limit": 200}).text
                    for name, secret in secrets_by_doc.items():
                        # A description only ever appears with its own (readable) document.
                        self.assertEqual(secret in raw, name in readable, (user, name))
                    items = {i["filename"]: i for i in self.listing(user)["items"]}
                    for name in readable:
                        self.assertIn(secrets_by_doc[name], items[name]["description"])
                        self.assertTrue(items[name]["description"].startswith("This passage describes"))
            self.assertIsNone(next(i for i in self.listing("admin")["items"] if i["filename"] == "engineering.md")["description"])
        finally:
            self.conn.execute("DELETE FROM document_chunks WHERE id = ANY(%s)", (inserted,))

    # --- delete: an unreadable document behaves exactly like a missing one -------------------------
    def test_deleting_an_unreadable_document_looks_like_not_found(self):
        missing = self.client.delete(f"{DOCS}/{UNKNOWN_ID}", headers=bearer(self.tokens["finance"]))
        self.assertEqual(missing.status_code, 404)
        for name in ("engineering.md", "orphan.md", "eng-folder.md"):  # uploads and a folder document
            with self.subTest(document=name):
                response = self.client.delete(f"{DOCS}/{self.docs[name]}", headers=bearer(self.tokens["finance"]))
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["error"], missing.json()["error"])
                self.assertTrue(self.exists(self.docs[name]))
        other_tenant = self.client.delete(f"{DOCS}/{self.b_company}", headers=bearer(self.tokens["admin"]))
        self.assertEqual((other_tenant.status_code, other_tenant.json()["error"]), (404, missing.json()["error"]))
        self.assertTrue(self.exists(self.b_company))

    def test_readable_documents_keep_the_existing_delete_behavior(self):
        # A readable folder document: still "managed by ingestion".
        response = self.client.delete(f"{DOCS}/{self.docs['company.md']}", headers=bearer(self.tokens["nobody"]))
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (409, "document_managed_by_ingestion"))
        # A readable upload can be deleted (department member / admin for an orphan), file included.
        for user, name in (("engineering", "engineering.md"), ("admin", "orphan.md")):
            with self.subTest(user=user, document=name):
                document_id = self.docs[name]
                stored = Path(self.conn.execute("SELECT path FROM documents WHERE id = %s", (document_id,)).fetchone()[0])
                self.assertTrue(stored.exists())
                response = self.client.delete(f"{DOCS}/{document_id}", headers=bearer(self.tokens[user]))
                self.assertEqual(response.status_code, 204, response.text)
                self.assertFalse(self.exists(document_id))
                self.assertFalse(stored.exists())
                # Put it back for the other tests (fresh id).
                departments = {"engineering.md": ["engineering"], "orphan.md": []}[name]
                self.__class__.docs[name] = self.add_document(self.tenant["a"], name, "upload", departments)


if __name__ == "__main__":
    unittest.main()
