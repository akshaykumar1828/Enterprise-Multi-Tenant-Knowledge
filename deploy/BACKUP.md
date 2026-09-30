# Backup and restore

Run every command from the project root, as the database **owner** (`PGUSER`/`PGPASSWORD`
from `.env` or the shell session), never as `rag_app`. Nothing below prints a password.

## What a complete backup consists of

| Item | Where it lives | In the database dump? | How to back it up |
|---|---|---|---|
| Tenants, users (incl. Argon2 password hashes), roles, departments, memberships, documents, document access, chunks, embeddings, full-text vectors, rate-limit counters | PostgreSQL database `PGDATABASE` (e.g. `enterprise_rag`) | **yes** | `src.rag.backup create` (below) |
| Uploaded files | `UPLOAD_DIR` (default `data\uploads\<tenant id>\<uuid>.<ext>`) | **no** — the database stores only each file's absolute path | copy the folder (below) |
| Folder-ingested source documents | `data\documents\` (or your `--documents-dir`) | **no** — only their text, chunks and embeddings | copy the folder / keep in version control |
| Secrets: `JWT_SECRET_KEY`, `GEMINI_API_KEY`, `PGPASSWORD`, `APP_DB_PASSWORD` | `.env` (never committed) | **no** — `verify` checks that none of their values occur in the dump | password manager / secure store, separately |
| PostgreSQL roles (`rag_app`) and their passwords | the PostgreSQL cluster | **no** (`pg_dump` covers one database) | recreated after a restore (step 4 below) |
| Embedding/reranker models | Hugging Face cache (`%USERPROFILE%\.cache\huggingface`) | no | re-downloadable; copy the cache if the server must stay offline (`HF_HUB_OFFLINE=1`) |
| `frontend\dist`, Caddy certificates | project / Caddy data folder | no | rebuilt / re-issued automatically |

Retrieval needs only the database: every chunk's text and embedding is stored there. The
files are needed to delete an uploaded document cleanly (its file is removed from
`UPLOAD_DIR`) and to re-ingest documents (e.g. after an embedding-model change).

**The dump contains password hashes and all document text: treat it as confidential.**
Store backups outside the repository (`*.dump` is git-ignored anyway), on another disk or
machine, with restricted access; encrypt copies that leave the server.

## 1. Back up the database

```powershell
New-Item -ItemType Directory -Force D:\rag-backups | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
.venv\Scripts\python.exe -m src.rag.backup create --out "D:\rag-backups\enterprise_rag-$stamp.dump"
```

This is exactly `pg_dump --format=custom --no-password --dbname=%PGDATABASE% --file=<out>`
(read-only for the database; the password comes from `PGPASSWORD`), followed by
`pg_restore --list` to check that the archive is readable and contains every
application table. It prints the file size and SHA-256. It refuses to overwrite an
existing file.

## 2. Back up the files

```powershell
# Uploaded files (use your UPLOAD_DIR if it is set) and the folder corpus:
robocopy data\uploads   "D:\rag-backups\uploads-$stamp"   /E /COPY:DAT /R:1 /W:1
robocopy data\documents "D:\rag-backups\documents-$stamp" /E /COPY:DAT /R:1 /W:1
```

Take the database and file backups together (ideally while no uploads are running) so
they describe the same moment. A file without a database row is harmless; a row whose
file is missing still answers questions, but deleting that upload leaves nothing to remove.

## 3. Verify a backup (safe; do this regularly)

```powershell
.venv\Scripts\python.exe -m src.rag.backup verify --dump "D:\rag-backups\enterprise_rag-$stamp.dump"
```

It creates a **new, separate** database `<PGDATABASE>_restore_verify_<8 hex>`, restores the
dump into it, compares it with the live database (row counts of all tables, ids,
checksums of chunk text, embeddings and full-text vectors, users/roles/password hashes,
departments, memberships, document access, content hashes, timestamps, sequences,
indexes, constraints, extensions), checks the dump for the secrets in `.env`, and drops
the scratch database. It never writes to the application database: every CREATE, restore
and DROP checks that the target is a `_restore_verify_` scratch name and not the
application or a system database. Run it right after `create` (the comparison is exact
only while the live database is unchanged). `--keep` leaves the scratch database for
inspection; drop it afterwards with the same tool or `DROP DATABASE`.

The automated test `tests/test_backup_restore.py` does the same with a throwaway company
and also runs the API against the restored copy (logins, `/auth/me`, authorized
retrieval, document list, Admin API).

## 4. Recovery (new or rebuilt server)

Prerequisites: PostgreSQL 18 with the **pgvector** extension installed (the dump contains
`CREATE EXTENSION vector`, 0.8.x), the project checked out, `.venv` and the frontend built
as in DEPLOYMENT.md, and the models cached (or internet access once).

1. **Secrets.** Restore `.env` from your secure store (`PGHOST`, `PGPORT`, `PGDATABASE`,
   `PGUSER`, `PGPASSWORD`, `JWT_SECRET_KEY`, `GEMINI_API_KEY`, `APP_ENV`, …). If the old
   `JWT_SECRET_KEY` is lost, generate a new one; users simply log in again.
2. **Stop the API** (and Caddy) so nothing writes during the restore.
3. **Create an empty database** — only on a server where it does not exist yet. Never
   restore over a database that is in use; to replace one, restore under a new name first,
   verify it, then switch `PGDATABASE`.
   ```powershell
   & "C:\Program Files\PostgreSQL\18\bin\createdb.exe" --no-password --owner=postgres --template=template0 --encoding=UTF8 enterprise_rag
   ```
4. **Restore** (as the owner; objects become owned by the restoring role, privileges are
   re-granted in the next step):
   ```powershell
   & "C:\Program Files\PostgreSQL\18\bin\pg_restore.exe" --no-password --exit-on-error --single-transaction --no-owner --no-privileges --dbname=enterprise_rag "D:\rag-backups\enterprise_rag-<stamp>.dump"
   ```
5. **Recreate the API's database role** (new random password written to `.env`, row
   access granted; also applies any pending schema migration):
   ```powershell
   .venv\Scripts\python.exe -m src.rag.db_roles create-app-role --role rag_app
   ```
6. **Restore the files** to the **same paths** as before (the database stores absolute
   paths), e.g.:
   ```powershell
   robocopy "D:\rag-backups\uploads-<stamp>"   data\uploads   /E /COPY:DAT
   robocopy "D:\rag-backups\documents-<stamp>" data\documents /E /COPY:DAT
   ```
7. **Check**, then start the API and Caddy (DEPLOYMENT.md step 6):
   ```powershell
   .venv\Scripts\python.exe -m src.rag.backup verify --dump "D:\rag-backups\enterprise_rag-<stamp>.dump"   # compares the restored live DB with the dump
   Invoke-RestMethod http://127.0.0.1:8000/api/v1/health
   ```
   Log in as an admin and ask a known question. Users keep their passwords (the hashes
   are in the dump).
