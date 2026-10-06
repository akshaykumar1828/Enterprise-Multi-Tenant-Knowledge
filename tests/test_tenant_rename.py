"""Renaming the company (display name only). Runs on a throwaway tenant in the test database.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_tenant_rename -v
"""

import secrets
import unittest

from tests.helpers import make_user  # noqa: I001  (sets up the isolated test database first)

from fastapi.testclient import TestClient

from src.api.main import app
from src.rag.db import connect
from src.rag.tenants import TenantNotFound, get_or_create_tenant, get_tenant, rename_tenant
from tests.helpers import bearer, login


class TenantRenameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = connect()
        cls.slug = f"test-rename-{secrets.token_hex(4)}"
        cls.tenant = get_or_create_tenant(cls.conn, cls.slug, "Old Name")

    @classmethod
    def tearDownClass(cls):
        cls.conn.execute("DELETE FROM tenants WHERE id = %s", (cls.tenant.id,))
        cls.conn.close()

    def test_rename_changes_only_the_display_name(self):
        renamed = rename_tenant(self.conn, self.slug, "  Redwood Inference  ")
        self.assertEqual((renamed.id, renamed.slug, renamed.name), (self.tenant.id, self.slug, "Redwood Inference"))
        user, password = make_user(self.conn, self.tenant.id, "rename")
        with TestClient(app) as client:
            me = client.get("/api/v1/auth/me", headers=bearer(login(client, user.email, password))).json()
        self.assertEqual(me["tenant"], {"slug": self.slug, "name": "Redwood Inference"})  # what the sidebar shows

    def test_invalid_names_and_unknown_tenants_are_refused(self):
        for name in ("", "   ", "x" * 101):
            with self.subTest(name=name[:10]), self.assertRaises(ValueError):
                rename_tenant(self.conn, self.slug, name)
        with self.assertRaises(TenantNotFound):
            rename_tenant(self.conn, "no-such-tenant", "Anything")
        self.assertEqual(get_tenant(self.conn, self.slug).id, self.tenant.id)


if __name__ == "__main__":
    unittest.main()
