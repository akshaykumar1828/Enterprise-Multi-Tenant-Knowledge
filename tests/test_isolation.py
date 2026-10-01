"""The test run is isolated from the live database and the production configuration.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_isolation -v
"""

import os
import unittest

from tests import isolated_db  # noqa: I001
from tests.helpers import make_user  # (starts the isolated database)

import psycopg

import src.api.main as api_main
from src.rag.db import connect, connect_app


class TestIsolationTests(unittest.TestCase):
    def test_tests_use_a_throwaway_copy_not_the_live_database(self):
        live = os.environ["RAG_TEST_LIVE_DATABASE"]
        current = os.environ["PGDATABASE"]
        self.assertNotEqual(current, live)
        self.assertRegex(current, rf"^{live}_testrun_[0-9a-f]{{8}}$")
        with connect() as conn:
            self.assertEqual(conn.execute("SELECT current_database()").fetchone()[0], current)
        with connect_app() as conn:
            user, database = conn.execute("SELECT current_user, current_database()").fetchone()
        self.assertEqual(database, current)
        self.assertRegex(user, r"^rag_app_testrun_[0-9a-f]{8}$")  # a throwaway API role, not rag_app

    def test_the_copy_starts_from_the_folder_corpus_only(self):
        with connect() as conn:
            other = conn.execute(
                "SELECT count(*) FROM documents d JOIN tenants t ON t.id = d.tenant_id "
                "WHERE t.slug = 'default' AND d.origin <> 'folder'").fetchone()[0]
            folder = conn.execute("SELECT count(*) FROM documents WHERE origin = 'folder'").fetchone()[0]
        self.assertEqual(other, 0)  # no uploads from the live database
        self.assertGreater(folder, 0)

    def test_production_settings_are_never_loaded(self):
        self.assertNotEqual(os.environ.get("APP_ENV", "").strip().lower(), "production")
        self.assertFalse(api_main.load_dotenv())  # the API's own .env loading is disabled during tests
        with self.assertRaises(RuntimeError):
            isolated_db._check_db(os.environ["RAG_TEST_LIVE_DATABASE"], os.environ["RAG_TEST_LIVE_DATABASE"])
        for name in ("postgres", "template1"):
            with self.assertRaises(RuntimeError):
                isolated_db._check_db(name, os.environ["RAG_TEST_LIVE_DATABASE"])

    def test_writes_land_in_the_copy(self):
        with connect() as conn:
            tenant = conn.execute("SELECT id FROM tenants WHERE slug = 'default'").fetchone()[0]
            user, _ = make_user(conn, tenant, "isolation")
            try:
                self.assertEqual(conn.execute("SELECT count(*) FROM users WHERE id = %s", (user.id,)).fetchone()[0], 1)
            finally:
                conn.execute("DELETE FROM users WHERE id = %s", (user.id,))
        live = os.environ["RAG_TEST_LIVE_DATABASE"]
        with psycopg.connect(dbname=live) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM users WHERE email = %s", (user.email,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
