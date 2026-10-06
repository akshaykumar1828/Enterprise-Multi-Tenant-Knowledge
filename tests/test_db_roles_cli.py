"""`python -m src.rag.db_roles ensure-app-role`, as the Docker migrate service runs it.

Runs against a scratch database with a throwaway role name: roles are cluster-wide, so
the real rag_app role (and its password) is never touched.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_db_roles_cli -v
"""

import contextlib
import io
import os
import secrets
import unittest
from unittest import mock

import tests.helpers  # noqa: F401,I001  (test database settings, never the production .env)
from tests.scratch_db import can_create_databases, check_name, scratch_database

import psycopg
from psycopg import sql

from src.rag import db_roles


@unittest.skipUnless(can_create_databases(), "the database role cannot create databases")
class EnsureAppRoleTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.db = check_name(self.stack.enter_context(scratch_database()))
        self.role = f"rag_app_clitest_{secrets.token_hex(4)}"

    def tearDown(self):
        self.stack.close()
        with psycopg.connect(dbname="postgres", autocommit=True) as conn:
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(self.role)))

    def run_cli(self, password: str) -> int:
        env = {"PGDATABASE": self.db, "APP_DB_PASSWORD": password}
        argv = ["db_roles", "ensure-app-role", "--role", self.role]
        with mock.patch.dict(os.environ, env), mock.patch("dotenv.load_dotenv", return_value=False), \
                mock.patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return db_roles.main()

    def role_can_read(self, password: str) -> bool:
        try:
            with psycopg.connect(dbname=self.db, user=self.role, password=password) as conn:
                conn.execute("SELECT count(*) FROM tenants").fetchone()
            return True
        except psycopg.OperationalError:
            return False

    def test_creates_the_schema_and_role_and_updates_the_password_on_rerun(self):
        first = secrets.token_urlsafe(24)
        self.assertEqual(self.run_cli(first), 0)
        self.assertTrue(self.role_can_read(first))

        second = secrets.token_urlsafe(24)
        self.assertEqual(self.run_cli(second), 0)  # safe to run on every start
        self.assertTrue(self.role_can_read(second))
        self.assertFalse(self.role_can_read(first))

    def test_refuses_a_missing_or_short_password(self):
        self.assertEqual(self.run_cli(""), 1)
        self.assertEqual(self.run_cli("short-15-chars!"), 1)
        with psycopg.connect(dbname="postgres") as conn:
            exists = conn.execute("SELECT count(*) FROM pg_roles WHERE rolname = %s", (self.role,)).fetchone()[0]
        self.assertEqual(exists, 0)


if __name__ == "__main__":
    unittest.main()
