"""Tenants: look them up by slug, and create them for ingestion.

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
