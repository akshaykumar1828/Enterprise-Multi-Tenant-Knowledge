"""Users: password hashing, storage, and credential checks.

Passwords are hashed with Argon2id (argon2-cffi defaults); only the hash is
stored. Every user belongs to exactly one tenant, and an email identifies one
user across the whole system.

Server-side command for creating users in an existing tenant (e.g. the
"default" tenant, which self-registration cannot join):
    .venv\\Scripts\\python.exe -m src.rag.users create --tenant default --email dev@example.com
    .venv\\Scripts\\python.exe -m src.rag.users set-password --email dev@example.com
"""

import argparse
import getpass
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256

_hasher = PasswordHasher()
# Verified against when an email is unknown, so a failed login takes about the
# same time whether or not the account exists.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


class EmailAlreadyRegistered(Exception):
    pass


class InvalidCredentials(Exception):
    pass


@dataclass(frozen=True)
class User:
    id: int
    email: str
    display_name: str | None
    tenant_id: int
    tenant_slug: str
    tenant_name: str


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("invalid email address")
    return email


def validate_password(password: str) -> str:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(f"password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters")
    return password


def hash_password(password: str) -> str:
    return _hasher.hash(validate_password(password))


_USER_SELECT = """
    SELECT u.id, u.email, u.display_name, u.tenant_id, t.slug, t.name, u.password_hash
    FROM users u JOIN tenants t ON t.id = u.tenant_id
"""


def _fetch(conn: psycopg.Connection, where: str, value) -> tuple[User, str] | None:
    row = conn.execute(f"{_USER_SELECT} WHERE {where}", (value,)).fetchone()
    return (User(*row[:6]), row[6]) if row else None


def get_user(conn: psycopg.Connection, user_id: int) -> User | None:
    found = _fetch(conn, "u.id = %s", user_id)
    return found[0] if found else None


def create_user(
    conn: psycopg.Connection, tenant_id: int, email: str, password: str, display_name: str | None = None
) -> User:
    email = normalize_email(email)
    password_hash = hash_password(password)
    try:
        with conn.transaction():
            user_id = conn.execute(
                "INSERT INTO users (tenant_id, email, display_name, password_hash) VALUES (%s, %s, %s, %s) RETURNING id",
                (tenant_id, email, display_name, password_hash),
            ).fetchone()[0]
    except psycopg.errors.UniqueViolation as error:
        raise EmailAlreadyRegistered(email) from error
    return get_user(conn, user_id)


def register_organization(
    conn: psycopg.Connection, organization_name: str, email: str, password: str, display_name: str | None = None
) -> User:
    """Self-service sign-up: create a new tenant and its first user together."""
    base = re.sub(r"[^a-z0-9]+", "-", organization_name.lower()).strip("-")[:40] or "org"
    slug = f"{base}-{secrets.token_hex(3)}"
    with conn.transaction():
        tenant_id = conn.execute(
            "INSERT INTO tenants (slug, name) VALUES (%s, %s) RETURNING id", (slug, organization_name.strip())
        ).fetchone()[0]
        # Inside the same transaction: if the email is taken, the tenant is rolled back too.
        return create_user(conn, tenant_id, email, password, display_name)


def authenticate(conn: psycopg.Connection, email: str, password: str) -> User:
    """Return the user for valid credentials; raise InvalidCredentials otherwise (same error either way)."""
    try:
        found = _fetch(conn, "lower(u.email) = %s", normalize_email(email))
    except ValueError:
        found = None
    if found is None:
        try:
            _hasher.verify(_DUMMY_HASH, password)
        except VerificationError:
            pass
        raise InvalidCredentials()

    user, password_hash = found
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        raise InvalidCredentials() from None
    if _hasher.check_needs_rehash(password_hash):
        conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (_hasher.hash(password), user.id))
    return user


def set_password(conn: psycopg.Connection, email: str, password: str) -> None:
    updated = conn.execute(
        "UPDATE users SET password_hash = %s WHERE lower(email) = %s",
        (hash_password(password), normalize_email(email)),
    ).rowcount
    if not updated:
        raise LookupError(f"no user with email {email!r}")


# --- server-side command ----------------------------------------------------

def _read_password(generate: bool) -> str:
    if generate:
        password = secrets.token_urlsafe(18)
        print(f"Generated password (shown once, store it safely): {password}")
        return password
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat password: "):
        raise ValueError("passwords do not match")
    return password


def main() -> int:
    from dotenv import load_dotenv

    from .db import connect, ensure_schema
    from .tenants import TenantNotFound, get_tenant

    parser = argparse.ArgumentParser(description="Manage users (server-side).")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="create a user in an existing tenant")
    create.add_argument("--tenant", required=True)
    create.add_argument("--email", required=True)
    create.add_argument("--display-name")
    create.add_argument("--generate-password", action="store_true", help="generate a random password and print it once")
    reset = commands.add_parser("set-password", help="set a user's password")
    reset.add_argument("--email", required=True)
    reset.add_argument("--generate-password", action="store_true")
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    with connect() as conn:
        ensure_schema(conn)
        try:
            if args.command == "create":
                tenant = get_tenant(conn, args.tenant)
                user = create_user(conn, tenant.id, args.email, _read_password(args.generate_password), args.display_name)
                print(f"Created user {user.email} (id {user.id}) in tenant {user.tenant_slug}")
            else:
                set_password(conn, args.email, _read_password(args.generate_password))
                print(f"Password updated for {args.email}")
        except (TenantNotFound, EmailAlreadyRegistered, LookupError, ValueError) as error:
            print(f"Error: {error.__class__.__name__}: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
