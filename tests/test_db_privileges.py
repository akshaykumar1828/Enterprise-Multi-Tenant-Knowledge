"""Least privilege of the API's database role, proven in throwaway databases only.

A scratch application database (real schema, via ensure_schema), an unrelated
scratch database, and a scratch role granted exactly like rag_app
(src.rag.db_roles). Every forbidden action is attempted there, as that role; then
the real API runs in production mode against the scratch database as that role
and performs every legitimate operation (login, /auth/me, upload, retrieval,
listing, admin API, delete). The application database is never used for these
probes. Everything is dropped afterwards. No Gemini calls.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_db_privileges -v
"""

import os
import secrets
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from tests.helpers import bearer, login  # noqa: I001  (sets the test JWT secret first)
from tests.scratch_db import can_create_databases, check_name, scratch_database

import psycopg
from fastapi.testclient import TestClient
from psycopg import sql

from src.api.main import app
from src.api.settings import verify_least_privilege
from src.rag.db import ensure_schema
from src.rag.db_roles import APP_TABLES, create_or_update_app_role, grant_app_privileges
from src.rag.tenants import get_or_create_tenant
from src.rag.users import create_user

InsufficientPrivilege = psycopg.errors.InsufficientPrivilege


@unittest.skipUnless(can_create_databases(), "the database role cannot create databases")
class AppRolePrivilegeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.db = cls.stack.enter_context(scratch_database())
        cls.other_db = cls.stack.enter_context(scratch_database())
        cls.role = f"rag_app_privtest_{secrets.token_hex(4)}"
        cls.password = secrets.token_urlsafe(24)
        cls.ungranted = f"rag_nogrant_privtest_{secrets.token_hex(4)}"
        cls.ungranted_password = secrets.token_urlsafe(24)

        # The application schema in the scratch database, and the role granted like rag_app.
        cls.owner = psycopg.connect(dbname=cls.db, autocommit=True)
        assert cls.owner.execute("SELECT current_database()").fetchone()[0] == check_name(cls.db)
        ensure_schema(cls.owner)
        create_or_update_app_role(cls.owner, cls.role, cls.password)
        grant_app_privileges(cls.owner, cls.role)
        # A login role with no grants at all (PUBLIC defaults only).
        create_or_update_app_role(cls.owner, cls.ungranted, cls.ungranted_password)
        # An unrelated database with data the role must not read.
        with psycopg.connect(dbname=cls.other_db, autocommit=True) as other:
            other.execute("CREATE TABLE unrelated_secrets (secret text)")
            other.execute("INSERT INTO unrelated_secrets VALUES ('not for the API')")

    @classmethod
    def tearDownClass(cls):
        cls.owner.close()
        cls.stack.close()  # drops both scratch databases (and with them every grant to the roles)
        with psycopg.connect(dbname="postgres", autocommit=True) as conn:
            for role in (cls.role, cls.ungranted):
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))

    def role_conn(self, dbname: str | None = None, role: str | None = None, password: str | None = None):
        """A connection as the scratch role to a scratch database (explicit user/password/database, never the env)."""
        dbname = check_name(dbname or self.db)
        conn = psycopg.connect(dbname=dbname, user=role or self.role, password=password or self.password, autocommit=True)
        assert conn.execute("SELECT current_user, current_database()").fetchone() == (role or self.role, dbname)
        return conn

    # --- what the role is -------------------------------------------------------------------------
    def test_role_attributes_and_ownership(self):
        with self.role_conn() as conn:
            attributes = conn.execute("SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, rolinherit, "
                                      "rolcanlogin FROM pg_roles WHERE rolname = current_user").fetchone()
            self.assertEqual(attributes[:5], (False, False, False, False, False))
            self.assertTrue(attributes[6])
            self.assertEqual(conn.execute("SELECT count(*) FROM pg_class WHERE relowner = "
                                          "(SELECT oid FROM pg_roles WHERE rolname = current_user)").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM pg_auth_members WHERE member = "
                                          "(SELECT oid FROM pg_roles WHERE rolname = current_user)").fetchone()[0], 0)
            verify_least_privilege(conn)  # the production startup check passes

    def test_exact_table_sequence_and_database_privileges(self):
        with self.role_conn() as conn:
            for table in APP_TABLES:
                with self.subTest(table=table):
                    granted = conn.execute(
                        "SELECT has_table_privilege(%(t)s, 'SELECT'), has_table_privilege(%(t)s, 'INSERT'),"
                        "       has_table_privilege(%(t)s, 'UPDATE'), has_table_privilege(%(t)s, 'DELETE'),"
                        "       has_table_privilege(%(t)s, 'TRUNCATE'), has_table_privilege(%(t)s, 'REFERENCES'),"
                        "       has_table_privilege(%(t)s, 'TRIGGER')", {"t": table}).fetchone()
                    self.assertEqual(granted, (True, True, True, True, False, False, False))
            for (sequence,) in conn.execute("SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'").fetchall():
                with self.subTest(sequence=sequence):
                    self.assertEqual(conn.execute("SELECT has_sequence_privilege(%(s)s, 'USAGE'), has_sequence_privilege(%(s)s, 'SELECT'),"
                                                  " has_sequence_privilege(%(s)s, 'UPDATE')", {"s": sequence}).fetchone(),
                                     (True, True, False))
            self.assertEqual(conn.execute("SELECT has_database_privilege(current_database(), 'CONNECT'),"
                                          " has_database_privilege(current_database(), 'CREATE'),"
                                          " has_database_privilege(current_database(), 'TEMPORARY'),"
                                          " has_schema_privilege('public', 'USAGE'), has_schema_privilege('public', 'CREATE')").fetchone(),
                             (True, False, False, True, False))

    # --- what the role cannot do (attempted in the scratch database only) ---------------------------------
    def test_role_cannot_change_schema_escalate_or_reach_the_server(self):
        forbidden = {
            "create table": "CREATE TABLE privtest_table (id int)",
            "create temporary table": "CREATE TEMPORARY TABLE privtest_temp (id int)",
            "drop table": "DROP TABLE documents",
            "alter table": "ALTER TABLE documents ADD COLUMN evil text",
            "truncate": "TRUNCATE document_chunks",
            "create index": "CREATE INDEX privtest_idx ON documents (source)",
            "create schema": "CREATE SCHEMA privtest_schema",
            "alter schema": "ALTER SCHEMA public RENAME TO privtest_renamed",
            "create function": "CREATE FUNCTION public.privtest() RETURNS int LANGUAGE sql AS 'SELECT 1'",
            "create extension": "CREATE EXTENSION IF NOT EXISTS pg_stat_statements",
            "create role": "CREATE ROLE privtest_escalation",
            "grant itself createrole": f"ALTER ROLE {self.role} CREATEROLE",
            "become the owner": "SET ROLE postgres",
            "change a sequence": "SELECT setval('documents_id_seq', 1)",
            "read server files": "SELECT pg_read_file('postgresql.conf')",
            "list server directories": "SELECT pg_ls_dir('.')",
            "run server programs": "COPY tenants FROM PROGRAM 'whoami'",
            "write server files": "COPY tenants TO 'C:/privtest.csv'",
            "import server files": "SELECT lo_import('postgresql.conf')",
        }
        with self.role_conn() as conn:
            for name, statement in forbidden.items():
                with self.subTest(action=name):
                    with self.assertRaises(InsufficientPrivilege):
                        with conn.transaction(force_rollback=True):  # nothing could persist anyway
                            conn.execute(statement)
            # GRANT on objects it does not own only draws a WARNING; nothing may be gained or given away.
            conn.execute("GRANT ALL ON documents TO CURRENT_USER")
            conn.execute("GRANT SELECT ON documents TO PUBLIC")
            self.assertEqual(conn.execute("SELECT has_table_privilege('documents', 'TRUNCATE'),"
                                          " has_table_privilege('documents', 'TRIGGER')").fetchone(), (False, False))
        entries = self.owner.execute("SELECT relacl::text FROM pg_class WHERE relname = 'documents'").fetchone()[0].strip("{}").split(",")
        self.assertFalse([e for e in entries if e.startswith("=")], "a PUBLIC privilege was granted")
        # And the schema is exactly as before: no table, index, schema or function was created.
        self.assertEqual(self.owner.execute("SELECT count(*) FROM pg_class WHERE relname LIKE 'privtest%%'").fetchone()[0], 0)
        self.assertEqual(self.owner.execute("SELECT count(*) FROM pg_roles WHERE rolname = 'privtest_escalation'").fetchone()[0], 0)

    def test_role_cannot_read_unrelated_databases(self):
        with self.role_conn(self.other_db) as conn:  # PUBLIC may connect to an unrelated database by default...
            for statement in ("SELECT secret FROM unrelated_secrets", "CREATE TABLE privtest_other (id int)"):
                with self.subTest(statement=statement), self.assertRaises(InsufficientPrivilege):
                    with conn.transaction(force_rollback=True):
                        conn.execute(statement)  # ...but it can neither read nor create anything there

    def test_the_application_database_admits_only_granted_roles(self):
        # PUBLIC has no CONNECT on the application database (grant_app_privileges revokes it).
        with self.assertRaises(psycopg.OperationalError) as caught:
            self.role_conn(self.db, role=self.ungranted, password=self.ungranted_password)
        self.assertIn("permission denied", str(caught.exception).lower())

    # --- everything the application legitimately does, as this role -----------------------------------------
    def test_the_api_works_in_production_as_the_role(self):
        tenant = get_or_create_tenant(self.owner, f"privtest-{secrets.token_hex(3)}", "Privilege Co").id
        admin_password, employee_password = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
        admin = create_user(self.owner, tenant, f"admin-{secrets.token_hex(3)}@example.test", admin_password)
        employee = create_user(self.owner, tenant, f"employee-{secrets.token_hex(3)}@example.test", employee_password)
        self.owner.execute("UPDATE users SET role = 'admin' WHERE id = %s", (admin.id,))
        uploads = tempfile.mkdtemp(prefix="rag-privtest-")
        env = {"APP_ENV": "production", "PGDATABASE": self.db, "APP_DB_USER": self.role, "APP_DB_PASSWORD": self.password,
               "UPLOAD_DIR": str(Path(uploads) / "uploads")}
        try:
            with mock.patch.dict(os.environ, env), TestClient(app) as client:  # production startup checks run here
                sessions = self.owner.execute("SELECT DISTINCT usename FROM pg_stat_activity WHERE datname = %s "
                                              "AND usename <> current_user", (self.db,)).fetchall()
                self.assertEqual(sessions, [(self.role,)])  # the API is connected as the role, nothing else

                tokens = {"admin": login(client, admin.email, admin_password),
                          "employee": login(client, employee.email, employee_password)}
                me = client.get("/api/v1/auth/me", headers=bearer(tokens["employee"])).json()
                self.assertEqual((me["role"], me["departments"]), ("employee", []))

                code = secrets.token_hex(4).upper()
                uploaded = client.post("/api/v1/documents", headers=bearer(tokens["employee"]), files={
                    "file": ("wharf.md", f"# Wharf\n\nThe Wharf gate code is {code}.\n".encode(), "text/markdown")})
                self.assertEqual(uploaded.status_code, 201, uploaded.text)
                document_id = uploaded.json()["id"]

                def visible(user: str) -> bool:
                    sources = client.post("/api/v1/query", headers=bearer(tokens[user]), json={
                        "question": "What is the Wharf gate code?", "retrieve_only": True}).json().get("sources", [])
                    listed = {d["id"] for d in client.get("/api/v1/documents", headers=bearer(tokens[user])).json()["items"]}
                    return any(code in s["text"] for s in sources) and document_id in listed

                self.assertTrue(visible("employee"))
                self.assertEqual(client.get("/api/v1/admin/users", headers=bearer(tokens["employee"])).status_code, 403)
                department = client.post("/api/v1/admin/departments", headers=bearer(tokens["admin"]),
                                         json={"slug": "ops", "name": "Operations"}).json()["id"]
                self.assertEqual(client.put(f"/api/v1/admin/documents/{document_id}/access", headers=bearer(tokens["admin"]),
                                            json={"visibility": "departments", "department_ids": [department]}).status_code, 200)
                self.assertFalse(visible("employee"))
                self.assertEqual(client.put(f"/api/v1/admin/users/{employee.id}/departments/{department}",
                                            headers=bearer(tokens["admin"])).status_code, 200)
                self.assertTrue(visible("employee"))
                self.assertEqual(client.put(f"/api/v1/admin/users/{employee.id}/role", headers=bearer(tokens["admin"]),
                                            json={"role": "employee"}).status_code, 200)
                self.assertEqual(client.delete(f"/api/v1/documents/{document_id}",
                                               headers=bearer(tokens["employee"])).status_code, 204)
                self.assertEqual(client.get("/api/v1/admin/users", headers=bearer(tokens["admin"])).json()["total"], 2)
        finally:
            shutil.rmtree(uploads, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
