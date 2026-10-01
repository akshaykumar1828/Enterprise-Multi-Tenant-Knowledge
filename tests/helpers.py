"""Shared test helpers: an isolated JWT secret, an isolated database, and throwaway users.

Every test module imports this first, so for the whole test run:
- tokens are signed with a random key, never the real JWT_SECRET_KEY;
- settings come from the test settings file, never the production .env, and the API
  does not read the project .env at startup either;
- all database work happens in a throwaway copy of the database (tests/isolated_db.py),
  which is dropped when the run ends. The live database is only read once (pg_dump).
"""

import os
import secrets

# Tests sign tokens with their own random key, never the real one.
os.environ["JWT_SECRET_KEY"] = secrets.token_urlsafe(64)

from tests.isolated_db import load_test_settings, start_isolated_database  # noqa: E402

load_test_settings()
start_isolated_database()

import src.api.main as _api_main  # noqa: E402

# The API loads <project>\.env at startup; during tests that file may be the production
# configuration, so the test settings above are the only ones used.
_api_main.load_dotenv = lambda *args, **kwargs: False

from src.rag.users import User, create_user  # noqa: E402


def new_password() -> str:
    return secrets.token_urlsafe(18)


def unique_email(label: str) -> str:
    return f"{label}-{secrets.token_hex(4)}@example.test"


def make_user(conn, tenant_id: int, label: str = "user") -> tuple[User, str]:
    password = new_password()
    return create_user(conn, tenant_id, unique_email(label), password), password


def login(client, email: str, password: str) -> str:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
