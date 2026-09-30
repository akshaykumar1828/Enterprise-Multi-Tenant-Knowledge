"""Read-only check of the API's database access (run from the project root, no admin needed).

    .venv\\Scripts\\python.exe deploy\\check_db_access.py

Connects exactly as the API does (APP_DB_USER / APP_DB_PASSWORD from .env, host/port
from PG*) and reports: which role and database it gets, that the role is not
privileged, and whether that role can also log in to other databases (it should be
rejected once the optional pg_hba rules from PRODUCTION_RUNBOOK.md are in place).
Writes nothing, prints no secrets. Exit code 1 if the API's own connection fails or
the role is privileged.
"""

import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.rag.db import app_connection_kwargs, app_role_configured  # noqa: E402

OTHER_DATABASES = ("postgres",)

if not app_role_configured():
    print("APP_DB_USER is not set in .env: the API would connect as the owner (development).")
    sys.exit(1)

kwargs = app_connection_kwargs()
try:
    with psycopg.connect(**kwargs) as conn:
        user, database, server = conn.execute("SELECT current_user, current_database(), inet_server_addr()").fetchone()
        superuser, createrole, createdb = conn.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = current_user").fetchone()
        print(f"API connection:  OK as {user} to {database} (server address {server or 'local socket'})")
        print(f"role privileges: superuser={superuser} createrole={createrole} createdb={createdb}")
        if superuser or createrole or createdb:
            print("PROBLEM: the API role is privileged")
            sys.exit(1)
except psycopg.OperationalError as error:
    print(f"API connection:  FAILED ({str(error).strip().splitlines()[-1][:120]})")
    sys.exit(1)

for other in OTHER_DATABASES:
    try:
        with psycopg.connect(**{**kwargs, "dbname": other}):
            print(f"other database {other!r}: login allowed (the role can read nothing there; "
                  "the optional pg_hba rules would reject it)")
    except psycopg.OperationalError as error:
        reason = str(error).strip().splitlines()[-1]
        verdict = "rejected by pg_hba (expected with the optional rules)" if "pg_hba.conf rejects" in reason else reason[:120]
        print(f"other database {other!r}: {verdict}")
