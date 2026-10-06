"""Tenants: look them up by slug, create them for ingestion, and rename them.

Until authentication exists, callers choose the tenant explicitly (CLI
--tenant, API request field). Everything stored before multi-tenancy belongs
to the "default" tenant, which the schema migration creates.
"""

import re
from dataclasses import dataclass

import psycopg

DEFAULT_TENANT = "default"
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class TenantNotFound(LookupError):
    pass


@dataclass(frozen=True)
class Tenant:
    id: int
    slug: str
    name: str


def validate_slug(slug: str) -> str:
    if not SLUG_PATTERN.fullmatch(slug):
        raise ValueError(f"Invalid tenant slug {slug!r}: use lowercase letters, digits and '-' (max 63)")
    return slug


def get_tenant(conn: psycopg.Connection, slug: str) -> Tenant:
    row = conn.execute("SELECT id, slug, name FROM tenants WHERE slug = %s", (slug,)).fetchone()
    if row is None:
        raise TenantNotFound(f"Unknown tenant: {slug!r}")
    return Tenant(*row)


def get_or_create_tenant(conn: psycopg.Connection, slug: str, name: str | None = None) -> Tenant:
    validate_slug(slug)
    conn.execute(
        "INSERT INTO tenants (slug, name) VALUES (%s, %s) ON CONFLICT (slug) DO NOTHING",
        (slug, name or slug),
    )
    return get_tenant(conn, slug)

def rename_tenant(conn: psycopg.Connection, slug: str, name: str) -> Tenant:
    """Change a company's display name (shown in the website). The slug, and everything keyed on it, stays."""
    name = name.strip()
    if not 1 <= len(name) <= 100:
        raise ValueError("The company name must be 1-100 characters.")
    if conn.execute("UPDATE tenants SET name = %s WHERE slug = %s", (name, slug)).rowcount != 1:
        raise TenantNotFound(f"Unknown tenant: {slug!r}")
    return get_tenant(conn, slug)


def main() -> int:
    """Operator command (owner connection, like the users command):
        .venv\\Scripts\\python.exe -m src.rag.tenants rename --name "Redwood Inference"
    """
    import argparse
    import sys

    from dotenv import load_dotenv

    from .db import connect

    parser = argparse.ArgumentParser(prog="python -m src.rag.tenants", description="Manage the company (tenant).")
    commands = parser.add_subparsers(dest="command", required=True)
    rename = commands.add_parser("rename", help="change the company's display name")
    rename.add_argument("--tenant", default=DEFAULT_TENANT, help="tenant slug (default: %(default)s)")
    rename.add_argument("--name", required=True)
    args = parser.parse_args()
    load_dotenv()
    try:
        with connect() as conn:
            tenant = rename_tenant(conn, args.tenant, args.name)
    except (ValueError, TenantNotFound) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Renamed tenant {tenant.slug!r} to {tenant.name!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
