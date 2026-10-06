"""A signed-in user changes their own password: POST /api/v1/auth/change-password.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_password_change -v
"""

import unittest

from tests.helpers import bearer, login, make_user, new_password  # noqa: I001  (sets the test JWT secret first)

from fastapi.testclient import TestClient

from src.api.main import app
from src.rag.db import connect
from src.rag.tenants import DEFAULT_TENANT, get_tenant

CHANGE = "/api/v1/auth/change-password"


class PasswordChangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        cls.conn = connect()
        tenant = get_tenant(cls.conn, DEFAULT_TENANT).id
        (cls.alice, cls.alice_password), (cls.bob, cls.bob_password) = (
            make_user(cls.conn, tenant, "pw-alice"), make_user(cls.conn, tenant, "pw-bob"))

    @classmethod
    def tearDownClass(cls):
        cls.conn.execute("DELETE FROM users WHERE id = ANY(%s)", ([cls.alice.id, cls.bob.id],))
        cls.conn.close()
        cls.client_context.__exit__(None, None, None)

    def login_status(self, email: str, password: str) -> int:
        return self.client.post("/api/v1/auth/login", json={"email": email, "password": password}).status_code

    def test_change_own_password(self):
        token = login(self.client, self.alice.email, self.alice_password)
        new = new_password()
        response = self.client.post(CHANGE, headers=bearer(token),
                                    json={"current_password": self.alice_password, "new_password": new})
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(self.login_status(self.alice.email, self.alice_password), 401)  # old one no longer works
        self.assertEqual(self.login_status(self.alice.email, new), 200)
        self.assertEqual(self.login_status(self.bob.email, self.bob_password), 200)  # nobody else is affected
        type(self).alice_password = new

    def test_wrong_current_password_is_refused_without_ending_the_session(self):
        token = login(self.client, self.alice.email, self.alice_password)
        response = self.client.post(CHANGE, headers=bearer(token),
                                    json={"current_password": "not-the-password", "new_password": new_password()})
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, "wrong_current_password"))
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=bearer(token)).status_code, 200)
        self.assertEqual(self.login_status(self.alice.email, self.alice_password), 200)

    def test_invalid_requests(self):
        token = login(self.client, self.bob.email, self.bob_password)
        cases = {
            "too short": ({"current_password": self.bob_password, "new_password": "short"}, 422),
            "unchanged": ({"current_password": self.bob_password, "new_password": self.bob_password}, 422),
            "extra field (no changing someone else's)": (
                {"current_password": self.bob_password, "new_password": new_password(), "user_id": self.alice.id}, 422),
        }
        for name, (body, status) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.client.post(CHANGE, headers=bearer(token), json=body).status_code, status)
        self.assertEqual(self.client.post(CHANGE, json={"current_password": "x", "new_password": new_password()}).status_code,
                         401)
        self.assertEqual(self.login_status(self.bob.email, self.bob_password), 200)  # nothing changed


if __name__ == "__main__":
    unittest.main()
