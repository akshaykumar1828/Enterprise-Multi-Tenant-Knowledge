"""PostgreSQL + pgvector connection and schema setup.

Connection settings come from the standard libpq environment variables
(PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD), loaded from .env.
Nothing here reads or prints the password itself.

Two identities:
  connect()      the owner role (PGUSER): schema migrations, ingestion, operator tools.
                 Direct connections, unchanged.
  connect_app()  API requests: the least-privilege role (APP_DB_USER / APP_DB_PASSWORD),
                 or the PG* identity if APP_DB_USER is not set (development).
                 Borrowed from a connection pool while the API is running
                 (open_app_pool / close_app_pool from the FastAPI lifespan); outside
                 the API it opens a short-lived connection with the same settings.

Every API connection has a connect timeout and a per-statement timeout:
  DB_POOL_MIN_SIZE (1)  DB_POOL_MAX_SIZE (10)  DB_POOL_TIMEOUT seconds to wait for a
  free connection (5)  DB_CONNECT_TIMEOUT seconds (5)  DB_STATEMENT_TIMEOUT_MS (15000)
"""

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

SCHEMA_FILE = Path(__file__).with_name("schema.sql")
log = logging.getLogger("rag.db")

_pool: ConnectionPool | None = None


def connect() -> psycopg.Connection:
    # autocommit: each write batch is wrapped in an explicit conn.transaction().
    return psycopg.connect(autocommit=True)


def app_role_configured() -> bool:
    return bool(os.environ.get("APP_DB_USER"))


def _positive_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, default))
    if value < 1:
        raise ValueError(f"{name} must be a positive number")
    return value


def pool_settings() -> dict:
    """Validated pool/timeout settings (raises ValueError on bad values)."""
    settings = {
        "min_size": _positive_int("DB_POOL_MIN_SIZE", 1),
        "max_size": _positive_int("DB_POOL_MAX_SIZE", 10),
        "timeout": _positive_int("DB_POOL_TIMEOUT", 5),
        "connect_timeout": _positive_int("DB_CONNECT_TIMEOUT", 5),
        "statement_timeout_ms": _positive_int("DB_STATEMENT_TIMEOUT_MS", 15000),
    }
    if settings["min_size"] > settings["max_size"]:
        raise ValueError("DB_POOL_MIN_SIZE must not exceed DB_POOL_MAX_SIZE")
    return settings


def app_connection_kwargs() -> dict:
    """psycopg.connect() arguments for API connections (host/port/database come from PG*)."""
    settings = pool_settings()
    kwargs = {
        "autocommit": True,
        "connect_timeout": settings["connect_timeout"],
        # Server-side limit per statement, so one slow query cannot hold a connection forever.
        "options": f"-c statement_timeout={settings['statement_timeout_ms']}",
    }
    if app_role_configured():
        kwargs["user"] = os.environ["APP_DB_USER"]
        kwargs["password"] = os.environ.get("APP_DB_PASSWORD", "")
    return kwargs


def open_app_pool() -> ConnectionPool:
    """Open the API connection pool (called once at API startup)."""
    global _pool
    close_app_pool()
    settings = pool_settings()
    pool = ConnectionPool(
        conninfo="",
        kwargs=app_connection_kwargs(),
        min_size=settings["min_size"],
        max_size=settings["max_size"],
        timeout=settings["timeout"],
        check=ConnectionPool.check_connection,  # discard broken connections before handing them out
        name="rag-api",
        open=False,
    )
    pool.open(wait=True, timeout=settings["connect_timeout"] + settings["timeout"])
    _pool = pool
    log.info("database pool opened (min %d, max %d)", settings["min_size"], settings["max_size"])
    return pool


def close_app_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
        log.info("database pool closed")


def app_pool() -> ConnectionPool | None:
    return _pool


@contextmanager
def connect_app() -> Iterator[psycopg.Connection]:
    """An API connection: from the pool while the API runs, otherwise a short-lived one."""
    if _pool is not None:
        with _pool.connection() as conn:
            yield conn
        return
    with psycopg.connect(**app_connection_kwargs()) as conn:
        yield conn


def ensure_schema(conn: psycopg.Connection) -> None:
    """Create or migrate the schema; all-or-nothing, safe to run on every start."""
    with conn.transaction():
        conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
    # Must run after the extension exists, so psycopg knows the vector type.
    register_vector(conn)
