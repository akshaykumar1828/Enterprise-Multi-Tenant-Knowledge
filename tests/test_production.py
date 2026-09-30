"""Production configuration tests: least-privilege database role and APP_ENV=production.

A temporary app role is created with the same functions the setup command uses
(src.rag.db_roles), exercised, and dropped again. No Gemini calls.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_production -v
"""

import os
import secrets
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from tests.helpers import bearer, new_password, unique_email  # noqa: I001  (test JWT secret first)

import psycopg
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.settings import (
    ProductionConfigError,
    UnsafeDatabaseRole,
    check_production_database,
    verify_least_privilege,
)
from src.rag.db import app_connection_kwargs, connect, connect_app
from src.rag.db_roles import create_or_update_app_role, drop_app_role, grant_app_privileges, write_env_values
from src.rag.tenants import DEFAULT_TENANT, get_tenant


def default_counts(conn) -> tuple[int, int]:
    tenant_id = get_tenant(conn, DEFAULT_TENANT).id
    return conn.execute(
        "SELECT (SELECT count(*) FROM documents WHERE tenant_id = %s), (SELECT count(*) FROM document_chunks WHERE tenant_id = %s)",
        (tenant_id, tenant_id)).fetchone()


class AppRoleTests(unittest.TestCase):
    """The least-privilege role: what it can and cannot do."""

    @classmethod
    def setUpClass(cls):
        cls.owner = connect()
        cls.role = f"rag_app_test_{secrets.token_hex(4)}"
        cls.password = secrets.token_urlsafe(24)
        create_or_update_app_role(cls.owner, cls.role, cls.password)
        grant_app_privileges(cls.owner, cls.role)
        cls.env = {"APP_DB_USER": cls.role, "APP_DB_PASSWORD": cls.password}

    @classmethod
    def tearDownClass(cls):
        drop_app_role(cls.owner, cls.role)
        cls.owner.close()

    @contextmanager
    def app_conn(self):
        """A direct connection as the temporary app role - fail closed.

        Connects while the role's credentials are in the environment (never via a
        shared pool), and refuses to hand the connection out unless it really is
        that role: the privilege probes below must never run as the owner.
        """
        with mock.patch.dict(os.environ, self.env):
            conn = psycopg.connect(**app_connection_kwargs())
        with conn:
            actual = conn.execute("SELECT current_user").fetchone()[0]
            if actual != self.role:
                raise RuntimeError(f"refusing to run privilege probes as {actual!r} instead of {self.role!r}")
            yield conn

    def test_connect_app_uses_the_app_role_and_is_least_privilege(self):
        with self.app_conn() as conn:
            self.assertEqual(conn.execute("SELECT current_user").fetchone()[0], self.role)
            verify_least_privilege(conn)  # does not raise
            self.assertFalse(conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0])

    # Writes and every forbidden action (DDL, escalation, server files/programs) are
    # exercised in a scratch database only: tests/test_db_privileges.py. Here the
    # role is only inspected (read-only) in the application database.
    def test_app_role_can_read_rows_and_holds_row_privileges(self):
        with self.app_conn() as conn:
            self.assertGreater(conn.execute("SELECT count(*) FROM documents").fetchone()[0], 0)
            for table in ("tenants", "users", "documents", "document_chunks", "rate_limits"):
                with self.subTest(table=table):
                    self.assertEqual(conn.execute(
                        "SELECT has_table_privilege(%(t)s, 'INSERT'), has_table_privilege(%(t)s, 'UPDATE'),"
                        "       has_table_privilege(%(t)s, 'DELETE'), has_table_privilege(%(t)s, 'TRUNCATE')",
                        {"t": table}).fetchone(), (True, True, True, False))

    def test_app_role_has_row_access_to_the_authorization_tables(self):
        with self.app_conn() as conn:
            for table in ("departments", "user_departments", "document_departments"):
                with self.subTest(table=table):
                    self.assertEqual(conn.execute(
                        "SELECT has_table_privilege(%s, 'SELECT'), has_table_privilege(%s, 'INSERT'),"
                        "       has_table_privilege(%s, 'UPDATE'), has_table_privilege(%s, 'DELETE'),"
                        "       has_table_privilege(%s, 'TRUNCATE')", (table,) * 5).fetchone(),
                        (True, True, True, True, False))

    def test_owner_and_superuser_are_rejected_for_production(self):
        with connect() as owner:  # the development identity (superuser, owns the tables)
            with self.assertRaises(UnsafeDatabaseRole):
                verify_least_privilege(owner)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ["APP_DB_USER"] = ""  # "not set"; empty, so load_dotenv(.env) cannot fill it back in
            with self.assertRaises(UnsafeDatabaseRole):
                check_production_database(connect_app)

    def test_production_startup_refuses_without_the_app_role(self):
        # Configuration is validated before anything connects, so the missing
        # APP_DB_USER is reported as a configuration error.
        with mock.patch.dict(os.environ, {"APP_ENV": "production"}):
            os.environ["APP_DB_USER"] = ""  # "not set"; empty, so load_dotenv(.env) cannot fill it back in
            with self.assertRaises(ProductionConfigError) as caught:
                with TestClient(app):
                    pass
        self.assertIn("APP_DB_USER", str(caught.exception))

    def test_write_env_values_replaces_and_appends_without_touching_other_lines(self):
        with tempfile.TemporaryDirectory() as folder:
            env_file = Path(folder) / ".env"
            env_file.write_text("GEMINI_API_KEY=keep-me\nAPP_DB_USER=old\n", encoding="utf-8")
            write_env_values(env_file, {"APP_DB_USER": "rag_app", "APP_DB_PASSWORD": "s3cret"})
            self.assertEqual(env_file.read_text(encoding="utf-8").splitlines(),
                             ["GEMINI_API_KEY=keep-me", "APP_DB_USER=rag_app", "APP_DB_PASSWORD=s3cret"])


class ProductionApiTests(unittest.TestCase):
    """The whole API in production mode, connected as the least-privilege role."""

    @classmethod
    def setUpClass(cls):
        cls.owner = connect()
        cls.before = default_counts(cls.owner)
        cls.role = f"rag_app_test_{secrets.token_hex(4)}"
        password = secrets.token_urlsafe(24)
        create_or_update_app_role(cls.owner, cls.role, password)
        grant_app_privileges(cls.owner, cls.role)
        cls.sandbox = tempfile.mkdtemp(prefix="rag-prod-test-")
        # Production defaults apply (rate limits on), except registration, which is
        # closed by default in production and opened here to create a test tenant.
        cls.owner.execute("DELETE FROM rate_limits WHERE bucket = 'register:testclient' OR bucket LIKE 'login:testclient:%'")
        cls.env = mock.patch.dict(os.environ, {
            "APP_ENV": "production", "APP_DB_USER": cls.role, "APP_DB_PASSWORD": password,
            "REGISTRATION_MODE": "open", "UPLOAD_DIR": str(Path(cls.sandbox) / "uploads")})
        cls.env.start()
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()  # production startup checks run here
        cls.pipeline = app.state.pipeline
        cls.pipeline.generate = lambda client, question, sources: "unused"  # never a real Gemini call

    @classmethod
    def tearDownClass(cls):
        from src.rag.llm import generate_answer

        cls.pipeline.generate = generate_answer
        cls.client_context.__exit__(None, None, None)
        cls.env.stop()
        tenants = [r[0] for r in cls.owner.execute("SELECT id FROM tenants WHERE slug LIKE 'prod-check-%'")]
        # Patterns are passed as parameters: a literal '%' next to a placeholder would be misread.
        cls.owner.execute(
            "DELETE FROM rate_limits WHERE bucket = %s OR bucket LIKE %s OR bucket = ANY(%s)",
            ("register:testclient", "login:testclient:%",
             [f"{kind}:tenant:{t}" for t in tenants for kind in ("query", "answer", "upload")]))
        cls.owner.execute("DELETE FROM documents WHERE tenant_id = ANY(%s)", (tenants,))
        cls.owner.execute("DELETE FROM tenants WHERE id = ANY(%s)", (tenants,))
        drop_app_role(cls.owner, cls.role)
        assert default_counts(cls.owner) == cls.before, "default tenant changed"
        cls.owner.close()
        shutil.rmtree(cls.sandbox, ignore_errors=True)

    def test_api_docs_are_not_served(self):
        for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_health_reveals_only_a_status(self):
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"status"})

    def test_full_user_flow_works_with_the_least_privilege_role(self):
        email, password = unique_email("prod-check"), new_password()
        response = self.client.post("/api/v1/auth/register", json={
            "organization_name": "Prod Check", "email": email, "password": password})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertTrue(response.json()["tenant"]["slug"].startswith("prod-check-"))
        token = self.client.post("/api/v1/auth/login", json={"email": email, "password": password}).json()["access_token"]
        auth = bearer(token)

        secret = secrets.token_hex(4).upper()
        upload = self.client.post("/api/v1/documents", headers=auth, files={
            "file": ("vault.md", f"# Vault\n\nThe Lyra vault code is {secret}.\n".encode(), "text/markdown")})
        self.assertEqual(upload.status_code, 201, upload.text)

        sources = self.client.post("/api/v1/query", headers=auth, json={
            "question": "What is the Lyra vault code?", "retrieve_only": True}).json()["sources"]
        self.assertIn(secret, sources[0]["text"])
        self.assertEqual(self.client.get("/api/v1/documents", headers=auth).json()["total"], 1)
        self.assertEqual(self.client.delete(f"/api/v1/documents/{upload.json()['id']}", headers=auth).status_code, 204)

        # The requests really ran as the app role.
        with connect_app() as conn:
            self.assertEqual(conn.execute("SELECT current_user").fetchone()[0], self.role)


class DevelopmentModeTests(unittest.TestCase):
    """Without APP_ENV/APP_DB_USER nothing changes (development)."""

    def test_development_defaults(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("APP_ENV", None)
            os.environ["APP_DB_USER"] = ""  # "not set"; empty, so load_dotenv(.env) cannot fill it back in
            with connect_app() as app_conn, connect() as owner_conn:
                self.assertEqual(app_conn.execute("SELECT current_user").fetchone(),
                                 owner_conn.execute("SELECT current_user").fetchone())
            with TestClient(app) as client:
                self.assertEqual(client.get("/docs").status_code, 200)
                self.assertIn("database", client.get("/api/v1/health").json())


if __name__ == "__main__":
    unittest.main()
