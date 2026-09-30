"""Shared test helpers: an isolated JWT secret and throwaway users."""

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

# Tests sign tokens with their own random key, never the real one from .env.
# (load_dotenv does not override variables that are already set.)
os.environ["JWT_SECRET_KEY"] = secrets.token_urlsafe(64)
# Database settings for every test module, whichever runs first.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

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
