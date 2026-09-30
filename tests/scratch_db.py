"""Throwaway PostgreSQL databases for destructive tests (never the application database).

Names are <PGDATABASE>_privtest_<8 hex>; every CREATE and DROP checks the name
against that pattern and against the application and system databases first.
"""

import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg import sql

PATTERN = re.compile(r"^[a-z0-9_]{1,40}_privtest_[0-9a-f]{8}$")
PROTECTED = {"postgres", "template0", "template1"}


def app_database() -> str:
    return os.environ["PGDATABASE"]


def check_name(name: str) -> str:
    if name == app_database() or name in PROTECTED or not PATTERN.fullmatch(name):
        raise ValueError(f"refusing to use {name!r} as a scratch database")
    return name


def can_create_databases() -> bool:
    with psycopg.connect() as conn:
        return bool(conn.execute("SELECT rolsuper OR rolcreatedb FROM pg_roles WHERE rolname = current_user").fetchone()[0])


@contextmanager
def scratch_database() -> Iterator[str]:
    """Create a new empty database, yield its name, drop it afterwards (connections are terminated)."""
    base = re.sub(r"[^a-z0-9_]", "_", app_database().lower())[:40]
    name = check_name(f"{base}_privtest_{secrets.token_hex(4)}")
    with psycopg.connect(dbname="postgres", autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(name)))
    try:
        yield name
    finally:
        check_name(name)
        with psycopg.connect(dbname="postgres", autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
