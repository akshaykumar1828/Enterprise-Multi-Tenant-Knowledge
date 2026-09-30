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

## 5. Scheduled backups (daily)

A Windows scheduled task, `\EnterpriseRAG\RAG Backup`, runs the same backup as step 1 every
day and keeps the most recent ones:

| Setting | Default | Change with |
|---|---|---|
| Schedule | daily at **02:30**; if the machine was off then, as soon as it is on again | `-At HH:mm` |
| Backup folder | `C:\rag-backups` (must be outside the project folder; copy it to another disk or machine regularly) | `-BackupDir` |
| Retention | the newest **14** backups (two weeks of daily backups) | `-Keep N` |
| Log | `C:\rag-logs\backup.log` (one line per step: `OK …` / `FAILED …`) | `-LogDir` |
| Account | your Windows account, limited rights, no stored Windows password (S4U) | `-UserId`, `-LogonType Password` |

Each run (`deploy\run_backup_task.ps1` → `python -m src.rag.backup_schedule`):

1. writes `<database>-YYYYMMDD-HHMMSS.dump` (custom format, read-only `pg_dump`); an
   existing file is never overwritten;
2. checks the archive with `pg_restore --list` (it must contain every application table).
   Only then is the backup counted; an unreadable archive is renamed to `….dump.invalid`
   (kept for inspection, never counted, never deleted automatically) and the run fails;
3. applies retention, only after a successful backup: among the files **directly in the
   backup folder** whose names match `<database>-YYYYMMDD-HHMMSS.dump` exactly, the newest
   `-Keep` stay and older ones are deleted. The backup just written is never deleted.
   Nothing else is ever touched (other files, `.invalid` files, other databases' dumps,
   subfolders). The log reports `retained=… removed=…`;
4. exits with 0 (success), 1 (backup failed; nothing deleted) or 2 (backup made, retention
   failed), which Task Scheduler shows as the task's *Last Run Result*.

A full restore is **not** part of the daily run (it takes a scratch database and minutes);
run the restore check yourself once a week and after every change to the setup (step 3):
`.venv\Scripts\python.exe -m src.rag.backup verify --dump <newest dump>`.

### Owner credentials without a password in the task

`pg_dump` needs the database owner. Its password is kept in PostgreSQL's own password file,
readable only by your account; the task passes only the file's path (`-PgPassFile`) and the
owner's role name (`-DbUser`, default `postgres`). Create the file once (no admin needed;
the password is typed into a hidden prompt and never shown):

```powershell
$pgpass = Join-Path $env:APPDATA "postgresql\pgpass.conf"
New-Item -ItemType Directory -Force (Split-Path $pgpass) | Out-Null
$pw = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR((Read-Host "PostgreSQL owner password" -AsSecureString)))
$pw = $pw -replace '\\', '\\' -replace ':', '\:'           # pgpass escaping for \ and :
Set-Content -LiteralPath $pgpass -Value "localhost:5432:enterprise_rag:postgres:$pw" -Encoding ascii
Remove-Variable pw
icacls $pgpass /inheritance:r /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):(R,W)"   # only you can read it
```

Check it works — in a **new** PowerShell window without `PGPASSWORD` set, one real run:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\run_backup_task.ps1 -BackupDir C:\rag-backups -LogDir C:\rag-logs `
    -PgPassFile "$env:APPDATA\postgresql\pgpass.conf"
Get-Content C:\rag-logs\backup.log -Tail 2          # OK backup … / OK retention …
```

If you later change the owner's password, update this file too.

### Register, check, remove (admin)

```powershell
powershell -ExecutionPolicy Bypass -File deploy\register_backup_task.ps1 -WhatIf   # dry run: must end with "Checks: OK"
powershell -ExecutionPolicy Bypass -File deploy\register_backup_task.ps1           # registers \EnterpriseRAG\RAG Backup
Start-ScheduledTask -TaskPath "\EnterpriseRAG\" -TaskName "RAG Backup"             # one run now, to check
```

Verify:

```powershell
Get-ScheduledTaskInfo -TaskPath "\EnterpriseRAG\" -TaskName "RAG Backup" | Select-Object LastRunTime, LastTaskResult, NextRunTime
# LastTaskResult 0 = success; 1 = backup failed; 2 = retention failed; 267009 = still running
Get-Content C:\rag-logs\backup.log -Tail 4
Get-ChildItem C:\rag-backups -Filter "enterprise_rag-*.dump" | Sort-Object Name | Select-Object -Last 3 Name, Length, LastWriteTime
```

Rollback (the task only; existing backups stay where they are):

```powershell
powershell -ExecutionPolicy Bypass -File deploy\register_backup_task.ps1 -Unregister
```

### Troubleshooting

| `backup.log` / symptom | Cause | Fix |
|---|---|---|
| `password file not found` | `-PgPassFile` path wrong or file missing | create it (above); re-register with the right `-PgPassFile` |
| `pg_dump failed … no password supplied` or `password authentication failed` | password file missing its line, wrong password, or a `:`/`\` not escaped | recreate the file (above) |
| `pg_dump failed … connection refused` / `could not connect` | PostgreSQL not running | `Get-Service postgresql-x64-18`; start it |
| `archive rejected, kept for inspection as ….invalid` | the dump could not be read back (disk full, interrupted) | check free space on the backup disk; delete the `.invalid` file once understood |
| `a file with this name already exists` | two runs in the same second | none needed; the next run gets a new name |
| `FAILED retention` (exit 2) | a backup file could not be deleted (in use, permissions) | the new backup is fine; fix permissions on the backup folder |
| no new line in `backup.log`, `LastTaskResult` not 0 | task could not start (account/logon type) | re-register with `-LogonType Password` |

### Recovery from a scheduled backup

Scheduled dumps are ordinary custom-format dumps, exactly like step 1: restore the newest
one (or the newest from before a problem) with the recovery sequence in step 4, after
checking it with `python -m src.rag.backup verify --dump <file>`. Files and secrets are not
in the dump; back up `UPLOAD_DIR`, `data\documents` and `.env` as described above.
