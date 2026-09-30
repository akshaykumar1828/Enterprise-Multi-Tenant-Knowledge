"""PostgreSQL backup and restore verification (operator tool, run as the owner).

    .venv\\Scripts\\python.exe -m src.rag.backup create --out D:\\rag-backups\\enterprise_rag.dump
    .venv\\Scripts\\python.exe -m src.rag.backup verify --dump D:\\rag-backups\\enterprise_rag.dump

`create` runs pg_dump (custom format) against the application database (PGDATABASE)
and checks the archive can be read back. It only reads from the database.

`verify` restores a dump into a NEW, separate scratch database named
<PGDATABASE>_restore_verify_<8 hex>, compares it with the live database (row
counts, ids, checksums of embeddings, text, full-text vectors, authorization
rows, timestamps, content and password hashes, sequences, indexes,
constraints), checks the dump contains none of the secrets from the
environment, and drops the scratch database again. It never writes to the
application database: every CREATE / restore / DROP checks the target name
first (scratch pattern, never the application or a system database).

Connection settings and the password come from the standard PG* environment
variables (.env); nothing here prints them. See deploy/BACKUP.md.
"""

import argparse
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass
from glob import glob
from pathlib import Path

import psycopg
from psycopg import sql

from .db_roles import APP_TABLES

MAINTENANCE_DB = "postgres"
PROTECTED_DATABASES = {"postgres", "template0", "template1"}
SCRATCH_SUFFIX = "_restore_verify_"
SCRATCH_PATTERN = re.compile(r"^[a-z0-9_]{1,40}_restore_verify_[0-9a-f]{8}$")
# Settings whose values must never appear in a dump.
SECRET_SETTINGS = ("PGPASSWORD", "APP_DB_PASSWORD", "JWT_SECRET_KEY", "GEMINI_API_KEY")


class BackupError(RuntimeError):
    pass


# --- tools and names ---------------------------------------------------------------

def pg_tool(name: str) -> str:
    """Path of a PostgreSQL client program: PG_BIN_DIR, then PATH, then the standard Windows install."""
    executable = f"{name}.exe" if os.name == "nt" else name
    if os.environ.get("PG_BIN_DIR"):
        candidate = Path(os.environ["PG_BIN_DIR"]) / executable
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    installed = sorted(glob(rf"C:\Program Files\PostgreSQL\*\bin\{executable}"))
    if installed:
        return installed[-1]
    raise BackupError(f"{name} not found: set PG_BIN_DIR to PostgreSQL's bin folder")


def app_database() -> str:
    name = os.environ.get("PGDATABASE", "")
    if not name:
        raise BackupError("PGDATABASE is not set")
    return name


def new_scratch_name(app_db: str) -> str:
    base = re.sub(r"[^a-z0-9_]", "_", app_db.lower())[:40]
    return f"{base}{SCRATCH_SUFFIX}{secrets.token_hex(4)}"


def check_scratch_name(name: str, app_db: str) -> str:
    """Refuse anything that is not clearly a scratch database created by this tool."""
    if name == app_db or name in PROTECTED_DATABASES or not SCRATCH_PATTERN.fullmatch(name):
        raise BackupError(f"refusing to use {name!r} as a scratch database")
    return name


def _run(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run a PostgreSQL client; the password is taken from PGPASSWORD in the environment, never argv."""
    result = subprocess.run(args, capture_output=True, text=True, env=env or os.environ.copy())
    if result.returncode != 0:
        raise BackupError(f"{Path(args[0]).stem} failed (exit {result.returncode}): {result.stderr.strip()[:1000]}")
    return result


# --- backup ---------------------------------------------------------------------------

@dataclass
class BackupInfo:
    path: Path
    size_bytes: int
    sha256: str
    tables_with_data: list[str]


def archive_tables(dump: Path) -> list[str]:
    """Tables whose data the archive contains (from pg_restore --list)."""
    listing = _run([pg_tool("pg_restore"), "--list", str(dump)]).stdout
    return sorted({line.split()[-2] for line in listing.splitlines() if " TABLE DATA " in line})


def create_backup(out: Path) -> BackupInfo:
    """pg_dump -Fc of the application database, then read the archive back to check it."""
    out = Path(out)
    if out.exists():
        raise BackupError(f"{out} already exists; choose a new file name")
    out.parent.mkdir(parents=True, exist_ok=True)
    _run([pg_tool("pg_dump"), "--format=custom", "--no-password", f"--dbname={app_database()}", f"--file={out}"])
    tables = archive_tables(out)
    missing = sorted(set(APP_TABLES) - set(tables))
    if missing:
        raise BackupError(f"archive is missing table data for {missing}")
    digest = hashlib.sha256()
    with out.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return BackupInfo(out, out.stat().st_size, digest.hexdigest(), tables)


def secrets_in_dump(dump: Path) -> list[str]:
    """Names of environment secrets whose values occur anywhere in the dump's SQL (values never printed)."""
    values = {name: os.environ[name].encode() for name in SECRET_SETTINGS
              if len(os.environ.get(name, "")) >= 8}
    if not values:
        return []
    longest = max(map(len, values.values()))
    found, tail = set(), b""
    with subprocess.Popen([pg_tool("pg_restore"), "--file=-", str(dump)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        assert process.stdout is not None
        for block in iter(lambda: process.stdout.read(1 << 20), b""):
            window = tail + block
            found |= {name for name, value in values.items() if value in window}
            tail = window[-longest:]
    if process.returncode != 0:
        raise BackupError("pg_restore could not render the dump for the secret check")
    return sorted(found)


# --- scratch database ---------------------------------------------------------------------

def _maintenance_connection() -> psycopg.Connection:
    return psycopg.connect(dbname=MAINTENANCE_DB, autocommit=True)


def create_scratch_database(name: str) -> None:
    check_scratch_name(name, app_database())
    with _maintenance_connection() as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            raise BackupError(f"{name!r} already exists")
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(name)))


def restore_into(dump: Path, name: str) -> None:
    """pg_restore into the scratch database only: explicit --dbname, and PGDATABASE overridden too."""
    check_scratch_name(name, app_database())
    env = {**os.environ, "PGDATABASE": name}
    _run([pg_tool("pg_restore"), "--no-password", "--exit-on-error", "--single-transaction",
          "--no-owner", "--no-privileges", f"--dbname={name}", str(dump)], env=env)


def drop_scratch_database(name: str) -> None:
    check_scratch_name(name, app_database())
    with _maintenance_connection() as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is None:
            return
        conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


# --- comparison -------------------------------------------------------------------------------

FINGERPRINT_QUERIES = {
    # Row counts of every application table.
    **{f"count {table}": f"SELECT count(*) FROM {table}" for table in APP_TABLES},
    # Identity and content.
    "tenants": "SELECT md5(coalesce(string_agg(id || ':' || slug || ':' || name || ':' || created_at, ',' ORDER BY id), '')) FROM tenants",
    "users": "SELECT md5(coalesce(string_agg(id || ':' || tenant_id || ':' || email || ':' || coalesce(display_name, '') "
             "|| ':' || role || ':' || created_at, ',' ORDER BY id), '')) FROM users",
    "password hashes": "SELECT md5(coalesce(string_agg(id || ':' || password_hash, ',' ORDER BY id), '')) FROM users",
    "departments": "SELECT md5(coalesce(string_agg(id || ':' || tenant_id || ':' || slug || ':' || name || ':' || created_at, ',' ORDER BY id), '')) FROM departments",
    "user_departments": "SELECT md5(coalesce(string_agg(tenant_id || ':' || user_id || ':' || department_id, ',' ORDER BY user_id, department_id), '')) FROM user_departments",
    "document_departments": "SELECT md5(coalesce(string_agg(tenant_id || ':' || document_id || ':' || department_id, ',' ORDER BY document_id, department_id), '')) FROM document_departments",
    "documents": "SELECT md5(coalesce(string_agg(concat_ws(':', id, tenant_id, relative_path, source, source_type, doc_id, path, "
                 "content_hash, chunk_size, chunk_overlap, embedding_model, embedding_input_version, origin, size_bytes, "
                 "visibility, uploaded_by, ingested_at), ',' ORDER BY id), '')) FROM documents",
    "chunk ids": "SELECT md5(coalesce(string_agg(concat_ws(':', id, tenant_id, document_id, chunk_id, chunk_index, page_number, "
                 "section, start_char, end_char), ',' ORDER BY id), '')) FROM document_chunks",
    "chunk text": "SELECT md5(coalesce(string_agg(text, chr(10) ORDER BY id), '')) FROM document_chunks",
    "embeddings": "SELECT md5(coalesce(string_agg(embedding::text, chr(10) ORDER BY id), '')) FROM document_chunks",
    "full-text vectors": "SELECT md5(coalesce(string_agg(coalesce(lexical_vector::text, ''), chr(10) ORDER BY id), '')) FROM document_chunks",
    # Structure.
    "sequences": "SELECT string_agg(sequencename || '=' || coalesce(last_value::text, 'null'), ',' ORDER BY sequencename) FROM pg_sequences WHERE schemaname = 'public'",
    "indexes": "SELECT md5(string_agg(indexname || ':' || indexdef, ',' ORDER BY indexname)) FROM pg_indexes WHERE schemaname = 'public'",
    "constraints": "SELECT md5(string_agg(conrelid::regclass || ':' || conname || ':' || pg_get_constraintdef(oid), ',' ORDER BY conrelid::regclass::text, conname)) "
                   "FROM pg_constraint WHERE connamespace = 'public'::regnamespace",
    "extensions": "SELECT string_agg(extname || ' ' || extversion, ',' ORDER BY extname) FROM pg_extension",
}


def fingerprint(conn: psycopg.Connection) -> dict[str, object]:
    return {name: conn.execute(query).fetchone()[0] for name, query in FINGERPRINT_QUERIES.items()}


def compare(source: dict, restored: dict) -> list[str]:
    """Names of every check whose value differs."""
    return [name for name in source if source[name] != restored.get(name)]


@dataclass
class VerifyReport:
    scratch_database: str
    source: dict
    restored: dict
    mismatches: list[str]
    secrets_found: list[str]

    @property
    def ok(self) -> bool:
        return not self.mismatches and not self.secrets_found


def verify_dump(dump: Path, keep: bool = False) -> VerifyReport:
    """Restore `dump` into a new scratch database, compare with the live database, drop the scratch copy.

    The comparison is exact only if the live database has not changed since the dump was taken.
    """
    app_db = app_database()
    scratch = check_scratch_name(new_scratch_name(app_db), app_db)
    create_scratch_database(scratch)
    try:
        restore_into(Path(dump), scratch)
        with psycopg.connect(dbname=app_db) as source_conn, psycopg.connect(dbname=scratch) as restored_conn:
            if restored_conn.execute("SELECT current_database()").fetchone()[0] != scratch:
                raise BackupError("connected to the wrong database")
            source, restored = fingerprint(source_conn), fingerprint(restored_conn)
        return VerifyReport(scratch, source, restored, compare(source, restored), secrets_in_dump(Path(dump)))
    finally:
        if not keep:
            drop_scratch_database(scratch)


# --- command line -----------------------------------------------------------------------------

def main() -> int:
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description="Back up the RAG database and verify a backup by restoring it into a scratch database.")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="pg_dump (custom format) of PGDATABASE")
    create.add_argument("--out", type=Path, required=True, help="new .dump file (keep it outside the repository)")
    verify = commands.add_parser("verify", help="restore into a new scratch database, compare, drop it")
    verify.add_argument("--dump", type=Path, required=True)
    verify.add_argument("--keep", action="store_true", help="keep the scratch database for inspection")
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    try:
        if args.command == "create":
            info = create_backup(args.out)
            print(f"Backup written: {info.path} ({info.size_bytes / 1e6:.1f} MB)")
            print(f"SHA-256: {info.sha256}")
            print(f"Table data in archive: {', '.join(info.tables_with_data)}")
            return 0
        report = verify_dump(args.dump, keep=args.keep)
        print(f"Restored into scratch database {report.scratch_database}"
              f" ({'kept' if args.keep else 'dropped afterwards'})")
        for name in FINGERPRINT_QUERIES:
            status = "same" if name not in report.mismatches else "DIFFERENT"
            shown = report.restored.get(name) if name.startswith("count") else ""
            print(f"  {name:22} {status:9} {shown}")
        print("Secrets in dump:", ", ".join(report.secrets_found) or "none")
        print("RESULT:", "OK" if report.ok else "FAILED")
        return 0 if report.ok else 1
    except (BackupError, psycopg.Error) as error:
        print(f"Error: {error.__class__.__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
