"""Authorization schema tests (Migration v9): roles, departments, visibility.

Schema only: nothing here exercises access filtering (not implemented yet).
Two throwaway tenants get users, departments and bare document rows (no
chunks); every cross-tenant link must be rejected by the database itself.
Everything is deleted again afterwards. No models, no Gemini calls.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_authz_schema -v
"""

import secrets
import unittest

from tests.helpers import make_user  # noqa: I001  (sets the test JWT secret first)

import psycopg

from src.rag.db import connect, ensure_schema
from src.rag.tenants import DEFAULT_TENANT, get_or_create_tenant, get_tenant


class AuthorizationSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = connect()
        ensure_schema(cls.conn)
        suffix = secrets.token_hex(4)
        cls.tenant, cls.user, cls.department, cls.document = {}, {}, {}, {}
        for key in ("a", "b"):
            tenant = get_or_create_tenant(cls.conn, f"test-authz-{key}-{suffix}", f"Authz tenant {key.upper()}")
            cls.tenant[key] = tenant.id
            cls.user[key] = make_user(cls.conn, tenant.id, f"authz-{key}")[0].id
            cls.department[key] = cls.add_department(tenant.id, "finance")
            cls.document[key] = cls.add_document(tenant.id, f"authz-{key}.md")

    @classmethod
    def tearDownClass(cls):
        ids = list(cls.tenant.values())
        cls.conn.execute("DELETE FROM documents WHERE tenant_id = ANY(%s)", (ids,))
        # Users, departments and every link table row cascade from the tenant.
        cls.conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", (ids,))
        cls.conn.close()

    @classmethod
    def add_department(cls, tenant_id: int, slug: str) -> int:
        return cls.conn.execute(
            "INSERT INTO departments (tenant_id, slug, name) VALUES (%s, %s, %s) RETURNING id",
            (tenant_id, slug, slug.title())).fetchone()[0]

    @classmethod
    def add_document(cls, tenant_id: int, path: str) -> int:
        return cls.conn.execute(
            """INSERT INTO documents (tenant_id, relative_path, source, source_type, path, content_hash,
                                      chunk_size, chunk_overlap, embedding_model, embedding_input_version)
               VALUES (%s, %s, %s, 'local', %s, repeat('0', 64), 500, 100, 'test-model', 'test')
               RETURNING id""", (tenant_id, path, path, path)).fetchone()[0]

    def rejected(self, error, statement: str, params: tuple):
        """The statement must fail with `error`; nothing persists either way."""
        with self.assertRaises(error):
            with self.conn.transaction(force_rollback=True):
                self.conn.execute(statement, params)

    def accepted(self, statement: str, params: tuple):
        with self.conn.transaction(force_rollback=True):
            self.conn.execute(statement, params)

    # --- defaults and allowed values ---------------------------------------------------
    def test_new_users_are_employees_and_new_documents_are_company_wide(self):
        role = self.conn.execute("SELECT role FROM users WHERE id = %s", (self.user["a"],)).fetchone()[0]
        self.assertEqual(role, "employee")
        visibility, uploaded_by = self.conn.execute(
            "SELECT visibility, uploaded_by FROM documents WHERE id = %s", (self.document["a"],)).fetchone()
        self.assertEqual((visibility, uploaded_by), ("company", None))

    def test_only_known_roles_and_visibilities_are_allowed(self):
        for role in ("admin", "employee"):
            self.accepted("UPDATE users SET role = %s WHERE id = %s", (role, self.user["a"]))
        for bad in ("owner", "Admin", ""):
            with self.subTest(role=bad):
                self.rejected(psycopg.errors.CheckViolation, "UPDATE users SET role = %s WHERE id = %s", (bad, self.user["a"]))
        for visibility in ("company", "departments"):
            self.accepted("UPDATE documents SET visibility = %s WHERE id = %s", (visibility, self.document["a"]))
        for bad in ("public", "private", ""):
            with self.subTest(visibility=bad):
                self.rejected(psycopg.errors.CheckViolation,
                              "UPDATE documents SET visibility = %s WHERE id = %s", (bad, self.document["a"]))
        self.rejected(psycopg.errors.NotNullViolation, "UPDATE users SET role = NULL WHERE id = %s", (self.user["a"],))
        self.rejected(psycopg.errors.NotNullViolation,
                      "UPDATE documents SET visibility = NULL WHERE id = %s", (self.document["a"],))

    def test_department_slugs_are_unique_per_tenant_only(self):
        self.rejected(psycopg.errors.UniqueViolation,
                      "INSERT INTO departments (tenant_id, slug, name) VALUES (%s, 'finance', 'Again')", (self.tenant["a"],))
        # Both tenants already hold a 'finance' department (setUpClass).
        self.assertNotEqual(self.department["a"], self.department["b"])

    # --- same-tenant links work, duplicates do not --------------------------------------
    def test_same_tenant_links_are_accepted_once(self):
        a = self.tenant["a"]
        membership = "INSERT INTO user_departments (tenant_id, user_id, department_id) VALUES (%s, %s, %s)"
        assignment = "INSERT INTO document_departments (tenant_id, document_id, department_id) VALUES (%s, %s, %s)"
        with self.conn.transaction(force_rollback=True):
            self.conn.execute(membership, (a, self.user["a"], self.department["a"]))
            self.conn.execute(assignment, (a, self.document["a"], self.department["a"]))
            self.conn.execute("UPDATE documents SET uploaded_by = %s WHERE id = %s", (self.user["a"], self.document["a"]))
            with self.assertRaises(psycopg.errors.UniqueViolation):
                with self.conn.transaction():
                    self.conn.execute(membership, (a, self.user["a"], self.department["a"]))
            with self.assertRaises(psycopg.errors.UniqueViolation):
                with self.conn.transaction():
                    self.conn.execute(assignment, (a, self.document["a"], self.department["a"]))

    # --- no link can cross a tenant boundary ---------------------------------------------
    def test_membership_cannot_cross_tenants(self):
        statement = "INSERT INTO user_departments (tenant_id, user_id, department_id) VALUES (%s, %s, %s)"
        a, b = self.tenant["a"], self.tenant["b"]
        for label, params in {
            "user A + department B, labelled A": (a, self.user["a"], self.department["b"]),
            "user A + department B, labelled B": (b, self.user["a"], self.department["b"]),
            "user B + department A, labelled A": (a, self.user["b"], self.department["a"]),
            "user A + department A, labelled B": (b, self.user["a"], self.department["a"]),
        }.items():
            with self.subTest(label):
                self.rejected(psycopg.errors.ForeignKeyViolation, statement, params)

    def test_document_assignment_cannot_cross_tenants(self):
        statement = "INSERT INTO document_departments (tenant_id, document_id, department_id) VALUES (%s, %s, %s)"
        a, b = self.tenant["a"], self.tenant["b"]
        for label, params in {
            "document A + department B, labelled A": (a, self.document["a"], self.department["b"]),
            "document A + department B, labelled B": (b, self.document["a"], self.department["b"]),
            "document B + department A, labelled A": (a, self.document["b"], self.department["a"]),
            "document A + department A, labelled B": (b, self.document["a"], self.department["a"]),
        }.items():
            with self.subTest(label):
                self.rejected(psycopg.errors.ForeignKeyViolation, statement, params)

    def test_uploader_must_belong_to_the_documents_tenant(self):
        self.rejected(psycopg.errors.ForeignKeyViolation,
                      "UPDATE documents SET uploaded_by = %s WHERE id = %s", (self.user["b"], self.document["a"]))

    def test_link_rows_cannot_be_moved_to_another_tenant(self):
        a, b = self.tenant["a"], self.tenant["b"]
        with self.conn.transaction(force_rollback=True):
            self.conn.execute("INSERT INTO user_departments (tenant_id, user_id, department_id) VALUES (%s, %s, %s)",
                              (a, self.user["a"], self.department["a"]))
            self.conn.execute("INSERT INTO document_departments (tenant_id, document_id, department_id) VALUES (%s, %s, %s)",
                              (a, self.document["a"], self.department["a"]))
            for table in ("user_departments", "document_departments"):
                with self.subTest(table=table), self.assertRaises(psycopg.errors.ForeignKeyViolation):
                    with self.conn.transaction():
                        self.conn.execute(f"UPDATE {table} SET tenant_id = %s WHERE tenant_id = %s", (b, a))

    # --- deletions ------------------------------------------------------------------------
    def test_deleting_a_user_keeps_their_documents_and_drops_memberships(self):
        a = self.tenant["a"]
        with self.conn.transaction(force_rollback=True):
            uploader = make_user(self.conn, a, "authz-uploader")[0].id
            self.conn.execute("UPDATE documents SET uploaded_by = %s WHERE id = %s", (uploader, self.document["a"]))
            self.conn.execute("INSERT INTO user_departments (tenant_id, user_id, department_id) VALUES (%s, %s, %s)",
                              (a, uploader, self.department["a"]))
            self.conn.execute("DELETE FROM users WHERE id = %s", (uploader,))
            row = self.conn.execute("SELECT tenant_id, uploaded_by FROM documents WHERE id = %s", (self.document["a"],)).fetchone()
            self.assertEqual(row, (a, None))
            self.assertEqual(self.conn.execute(
                "SELECT count(*) FROM user_departments WHERE user_id = %s", (uploader,)).fetchone()[0], 0)

    def test_deleting_a_department_removes_its_assignments(self):
        a = self.tenant["a"]
        with self.conn.transaction(force_rollback=True):
            self.conn.execute("INSERT INTO document_departments (tenant_id, document_id, department_id) VALUES (%s, %s, %s)",
                              (a, self.document["a"], self.department["a"]))
            self.conn.execute("DELETE FROM departments WHERE id = %s", (self.department["a"],))
            self.assertEqual(self.conn.execute(
                "SELECT count(*) FROM document_departments WHERE document_id = %s", (self.document["a"],)).fetchone()[0], 0)
            self.assertEqual(self.conn.execute(
                "SELECT count(*) FROM documents WHERE id = %s", (self.document["a"],)).fetchone()[0], 1)

    # --- migration of existing data ---------------------------------------------------------
    def test_rerunning_the_migration_changes_no_roles(self):
        before = self.conn.execute("SELECT id, role FROM users ORDER BY id").fetchall()
        ensure_schema(self.conn)
        self.assertEqual(self.conn.execute("SELECT id, role FROM users ORDER BY id").fetchall(), before)
        # Users created after v9 stay employees, even as the first user of a tenant.
        self.assertEqual(dict(before)[self.user["b"]], "employee")

    def test_existing_corpus_is_company_wide(self):
        default = get_tenant(self.conn, DEFAULT_TENANT).id
        rows = self.conn.execute(
            "SELECT visibility, count(*) FROM documents WHERE tenant_id = %s AND origin = 'folder' GROUP BY 1",
            (default,)).fetchall()
        self.assertEqual([r[0] for r in rows], ["company"])


if __name__ == "__main__":
    unittest.main()
