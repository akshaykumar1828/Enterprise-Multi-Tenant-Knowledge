"""Least-privilege database role for the API.

The API connects as an "app" role that can only read and write rows in the
application tables. Schema changes (ensure_schema) and operator tools keep
using the owner role from PGUSER/PGPASSWORD.

The app role cannot create, alter, drop or truncate tables, is not a
superuser, and owns nothing.

Run as the owner (credentials from .env), once per database:
    .venv\\Scripts\\python.exe -m src.rag.db_roles create-app-role --role rag_app
This generates a random password, creates or updates the role, grants
privileges, and writes APP_DB_USER / APP_DB_PASSWORD into .env without
printing the password. Re-apply grants after adding tables with:
    .venv\\Scripts\\python.exe -m src.rag.db_roles grant --role rag_app
"""

import argparse
import re
import secrets
import sys
from pathlib import Path

import psycopg
from psycopg import sql

APP_TABLES = ("tenants", "users", "documents", "document_chunks", "rate_limits",
              "departments", "user_departments", "document_departments")
ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def validate_role_name(role: str) -> str:
    if not ROLE_PATTERN.fullmatch(role):
        raise ValueError("role name must be lowercase letters, digits and underscores")
    return role


def create_or_update_app_role(conn: psycopg.Connection, role: str, password: str) -> None:
    """Create the login role (or reset its password) with no special attributes."""
    validate_role_name(role)
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    # Utility statements cannot take bind parameters; the password is quoted
    # client-side as a literal (and the server stores only its SCRAM hash).
    statement = "ALTER ROLE {} WITH LOGIN PASSWORD {}" if exists else "CREATE ROLE {} WITH LOGIN PASSWORD {}"
    conn.execute(sql.SQL(statement + " NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")
                 .format(sql.Identifier(role), sql.Literal(password)))


def grant_app_privileges(conn: psycopg.Connection, role: str) -> None:
    """Row-level access to the application tables only; no DDL of any kind."""
    validate_role_name(role)
    who = sql.Identifier(role)
    database = sql.Identifier(conn.info.dbname)
    tables = sql.SQL(", ").join(sql.Identifier(t) for t in APP_TABLES)
    statements = [
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, who),
        sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(who),
        sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(who),
        sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {} TO {}").format(tables, who),
        # bigserial primary keys need their sequences.
        sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(who),
        # Tables the owner adds in later migrations get the same row-level access.
        sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}").format(who),
        sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {}").format(who),
    ]
    with conn.transaction():
        for statement in statements:
            conn.execute(statement)


def drop_app_role(conn: psycopg.Connection, role: str) -> None:
    """Remove the role and every privilege granted to it (it owns nothing)."""
    validate_role_name(role)
    with conn.transaction():
        conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def write_env_values(env_file: Path, values: dict[str, str]) -> None:
    """Set KEY=value lines in an env file, replacing existing keys, without printing values."""
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    remaining = dict(values)
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"
    lines.extend(f"{key}={value}" for key, value in remaining.items())
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    from dotenv import load_dotenv

    from .db import connect, ensure_schema

    parser = argparse.ArgumentParser(description="Manage the API's least-privilege database role (run as the owner).")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-app-role", help="create/update the role, grant privileges, write .env")
    create.add_argument("--role", default="rag_app")
    create.add_argument("--env-file", type=Path, default=Path(".env"))
    grant = commands.add_parser("grant", help="re-apply privileges (e.g. after a migration added tables)")
    grant.add_argument("--role", default="rag_app")
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    try:
        with connect() as conn:
            if conn.execute("SELECT current_user = %s", (args.role,)).fetchone()[0]:
                print("Error: run this as the owner role, not as the app role.", file=sys.stderr)
                return 1
            ensure_schema(conn)  # the tables must exist before privileges are granted
            if args.command == "create-app-role":
                password = secrets.token_urlsafe(32)
                create_or_update_app_role(conn, args.role, password)
                grant_app_privileges(conn, args.role)
                write_env_values(args.env_file, {"APP_DB_USER": args.role, "APP_DB_PASSWORD": password})
                print(f"Role {args.role!r} ready; APP_DB_USER and APP_DB_PASSWORD written to {args.env_file} (not shown).")
            else:
                grant_app_privileges(conn, args.role)
                print(f"Privileges re-applied for role {args.role!r}.")
    except (psycopg.Error, ValueError) as error:
        print(f"Error: {error.__class__.__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
