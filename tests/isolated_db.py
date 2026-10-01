"""Test isolation: test settings, and a throwaway copy of the database for each test run.

Imported (via tests/helpers.py) before anything else in every test module:

1. Settings come from a test settings file, never from the production .env:
   RAG_TEST_ENV_FILE if set, else .env.test, else .env.development, else .env - but
   any file with APP_ENV=production is skipped. It must give the database owner's
   connection (PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD).

2. The live database PGDATABASE is dumped (read-only pg_dump) and restored into a new
   database <PGDATABASE>_testrun_<8 hex>. In that copy everything except the default
   tenant's folder-ingested corpus is deleted (users, departments, memberships,
   uploads, other tenants, rate-limit counters), so tests start from a known state
   whatever the live database contains. A throwaway API role is granted on the copy,
   and PGDATABASE / APP_DB_USER / APP_DB_PASSWORD / UPLOAD_DIR point the whole test
   run at it. At exit the copy and the role are dropped.

The live database is only ever read (pg_dump). Every CREATE/DROP checks the target
name against the _testrun_ pattern and refuses the live and system databases.
Copies left by a run that crashed are removed at the start of the next run.
"""

import atexit
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg import sql

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROTECTED = {"postgres", "template0", "template1"}
ROLE_PATTERN = re.compile(r"^rag_app_testrun_[0-9a-f]{8}$")

_state: dict = {}


# --- settings -------------------------------------------------------------------------------

def _is_production(values: dict) -> bool:
    return (values.get("APP_ENV") or "").strip().lower() == "production"


def load_test_settings() -> Path:
    """Put the test settings into os.environ (existing variables win). Never the production .env."""
    explicit = os.environ.get("RAG_TEST_ENV_FILE")
    candidates = [Path(explicit)] if explicit else [
        PROJECT_ROOT / ".env.test", PROJECT_ROOT / ".env.development", PROJECT_ROOT / ".env"]
    for path in candidates:
        if not path.is_file():
            continue
        values = dotenv_values(path)
        if _is_production(values):
            if explicit:
                raise RuntimeError(f"RAG_TEST_ENV_FILE points to a production settings file: {path.name}")
            continue
        for key, value in values.items():
            if value is not None and key not in os.environ:
                os.environ[key] = value
        return path
    raise RuntimeError(
        "No test settings found. Create .env.test (or .env.development) in the project root with the "
        "database owner's PGHOST, PGPORT, PGDATABASE, PGUSER and PGPASSWORD; the production .env is never used.")


# --- names ----------------------------------------------------------------------------------

def _base(live: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", live.lower())[:40]


def _db_pattern(live: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(_base(live))}_testrun_[0-9a-f]{{8}}$")


def _check_db(name: str, live: str) -> str:
    if name == live or name in PROTECTED or not _db_pattern(live).fullmatch(name):
        raise RuntimeError(f"refusing to use {name!r} as a test database")
    return name


def _pg_tool(name: str) -> str:
    from src.rag.backup import pg_tool

    return pg_tool(name)


def _maintenance():
    return psycopg.connect(dbname="postgres", autocommit=True)


def _drop(name: str, live: str) -> None:
    _check_db(name, live)
    with _maintenance() as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def _drop_role(role: str) -> None:
    if not ROLE_PATTERN.fullmatch(role):
        raise RuntimeError(f"refusing to drop role {role!r}")
    with _maintenance() as conn:
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _remove_stale(live: str) -> None:
    """Drop test databases/roles left by a crashed run (databases nobody is connected to)."""
    pattern = _db_pattern(live)
    with _maintenance() as conn:
        stale = [name for (name,) in conn.execute(
            "SELECT d.datname FROM pg_database d WHERE NOT EXISTS "
            "(SELECT 1 FROM pg_stat_activity a WHERE a.datname = d.datname)") if pattern.fullmatch(name)]
        roles = [r for (r,) in conn.execute("SELECT rolname FROM pg_roles") if ROLE_PATTERN.fullmatch(r)]
    for name in stale:
        _drop(name, live)
    for role in roles:
        try:
            _drop_role(role)
        except psycopg.Error:
            pass  # still has privileges in a running test database


# --- the test database ------------------------------------------------------------------------

def start_isolated_database() -> str:
    """Create the test copy once per process and point the environment at it. Returns its name."""
    if _state:
        return _state["name"]
    from src.rag.db_roles import create_or_update_app_role, grant_app_privileges

    live = os.environ["PGDATABASE"]
    if "_testrun_" in live:
        raise RuntimeError("PGDATABASE already names a test database")
    _remove_stale(live)
    suffix = secrets.token_hex(4)
    name = _check_db(f"{_base(live)}_testrun_{suffix}", live)
    role, password = f"rag_app_testrun_{suffix}", secrets.token_urlsafe(24)
    work = Path(tempfile.mkdtemp(prefix="rag-testrun-"))
    _state.update(name=name, role=role, live=live, work=work)
    atexit.register(stop_isolated_database)

    dump = work / "live.dump"
    subprocess.run([_pg_tool("pg_dump"), "--format=custom", "--no-password", f"--dbname={live}", f"--file={dump}"],
                   check=True, capture_output=True)
    with _maintenance() as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(name)))
    subprocess.run([_pg_tool("pg_restore"), "--no-password", "--exit-on-error", "--single-transaction",
                    "--no-owner", "--no-privileges", f"--dbname={name}", str(dump)],
                   check=True, capture_output=True, env={**os.environ, "PGDATABASE": name})
    dump.unlink()

    with psycopg.connect(dbname=name, autocommit=True) as conn:
        if conn.execute("SELECT current_database()").fetchone()[0] != name:
            raise RuntimeError("connected to the wrong database")
        with conn.transaction():
            # Only the default tenant's folder corpus stays: a known, data-independent start.
            conn.execute("DELETE FROM document_departments")
            conn.execute("DELETE FROM user_departments")
            conn.execute("DELETE FROM departments")
            conn.execute("DELETE FROM documents WHERE NOT (origin = 'folder' AND tenant_id = "
                         "(SELECT id FROM tenants WHERE slug = 'default'))")  # chunks cascade
            conn.execute("DELETE FROM users")
            conn.execute("DELETE FROM rate_limits")
            conn.execute("DELETE FROM tenants WHERE slug <> 'default'")
        create_or_update_app_role(conn, role, password)
        grant_app_privileges(conn, role)

    os.environ.update({
        "PGDATABASE": name,
        "APP_DB_USER": role,
        "APP_DB_PASSWORD": password,
        "UPLOAD_DIR": str(work / "uploads"),
        "RAG_TEST_LIVE_DATABASE": live,
    })
    return name


def stop_isolated_database() -> None:
    if not _state:
        return
    try:
        _drop(_state["name"], _state["live"])
        _drop_role(_state["role"])
    finally:
        shutil.rmtree(_state["work"], ignore_errors=True)
        _state.clear()
