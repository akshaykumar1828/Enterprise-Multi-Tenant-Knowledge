# Operations

This page covers deployment, backups, security hardening, tests and day-to-day commands. The step-by-step
administrator procedures are in [deploy/DEPLOYMENT.md](../deploy/DEPLOYMENT.md),
[deploy/PRODUCTION_RUNBOOK.md](../deploy/PRODUCTION_RUNBOOK.md) and [deploy/BACKUP.md](../deploy/BACKUP.md).

> No secret values appear in this repository's docs. Real values live only in the production `.env` (never
> committed) and the PostgreSQL password file of the account that runs backups.

## Production deployment

Everything runs on a single Windows machine.

| Service | Listens on | Started by |
|---|---|---|
| Caddy (TLS, static frontend, `/api` proxy) | 80/443, or 8080 locally; admin API on `localhost:2999` | `\EnterpriseRAG\RAG Caddy` (`deploy/start_caddy_task.ps1`) |
| FastAPI, one uvicorn worker | `127.0.0.1:8000` only | `\EnterpriseRAG\RAG API` (`deploy/start_api_task.ps1`) |
| PostgreSQL 18 + pgvector | `127.0.0.1` / `::1` only | Windows service |
| Daily backup | – | `\EnterpriseRAG\RAG Backup` (`deploy/run_backup_task.ps1`), daily at 02:30 |

**Caddy's admin port.** On this machine Windows refuses port 2019, so Caddy is started with
`CADDY_ADMIN=localhost:2999` (set in `deploy/caddy.env`, which isn't committed).

**Production `.env`.** The full template is `deploy/env.production.example`; these are the settings that matter most
(names only):
- `APP_ENV=production`
- the API's own database account, `APP_DB_USER` and `APP_DB_PASSWORD` (the restricted `rag_app` role, never the
  owner)
- the database location, `PGHOST`, `PGPORT` and `PGDATABASE`
- `JWT_SECRET_KEY` and `ACCESS_TOKEN_EXPIRE_MINUTES`
- `GEMINI_API_KEY`
- `UPLOAD_DIR`
- `HF_HUB_OFFLINE=1`
- `REGISTRATION_MODE`
- the `RATE_LIMIT_*` settings
- the `TENANT_MAX_*` and `UPLOAD_MAX_*` limits
- the `DB_POOL_*` settings and `DB_STATEMENT_TIMEOUT_MS`
- the `LOG_*` settings

The production `.env` deliberately contains **no** database-owner credentials.

**Startup checks in production.** The API refuses to start if:
- the JWT secret is short, low-variety or looks like a placeholder;
- `APP_DB_USER` is missing;
- `UPLOAD_DIR` isn't writable;
- the pool or timeout settings are invalid;
- the database role is a superuser, can create roles or databases, or owns any application table.

Access changes need **no restart**: every request reloads the caller's scope from the database. A change to code or
`.env` needs the API task restarted:
`Stop-ScheduledTask` / `Start-ScheduledTask -TaskPath "\EnterpriseRAG\" -TaskName "RAG API"`.

## Backup and restore

| What | How |
|---|---|
| Create | `python -m src.rag.backup create --out C:\rag-backups\<name>.dump`. This is a custom-format `pg_dump` of every table. It prints the SHA-256, the tables in the archive, and the result of a check that the dump contains no secrets. |
| Verify | `python -m src.rag.backup verify --dump <file>`. This restores into a scratch database `<db>_restore_verify_<hex>`, compares row counts, password hashes, documents, chunk ids and text, embeddings, full-text vectors, sequences, indexes, constraints and extensions, then drops the scratch database. |
| Scheduled | `\EnterpriseRAG\RAG Backup` runs daily at 02:30 into `C:\rag-backups`. It keeps the newest 14 files that match `<db>-YYYYMMDD-HHMMSS.dump` and logs to `C:\rag-logs\backup.log`. |
| Restore | See [deploy/BACKUP.md §4](../deploy/BACKUP.md). Restore the dump, restore the uploaded files and the folder corpus, then re-grant `rag_app` with `python -m src.rag.db_roles grant --role rag_app`. |

The backup and verify commands need the **database owner's** connection: `PGUSER`, plus the password from the
PostgreSQL password file. They aren't run with the production `.env`. Back up the files as well as the database:
`UPLOAD_DIR` and `data/documents/`.

### Restore points

These restore points were taken for the authorization rollout on 2026-10-01. Both verified with `RESULT: OK` and no
secrets found in the dump. Their names don't match the retention pattern, so the scheduled job never deletes them.

| File (`C:\rag-backups\`) | State captured | SHA-256 |
|---|---|---|
| `enterprise_rag-pre-authz-rollout-20261001-181157.dump` (53.0 MB) | before any authorization change: 0 departments, all documents company-wide | `b172b4db2dadf134d5d220544445c588b7a6657195fc6a36230ad0efda8676d7` |
| `enterprise_rag-pre-authz-access-20261001-181756.dump` (53.0 MB) | the 8 departments created, no document access applied yet | `cd3923987567c249b39ca8a2da2a00162d83c91fbb5c88eef5d7b2c2be25215f` |

An older restore point, from before the demo-data cleanup, is `enterprise_rag-pre-demo-cleanup-20261001-153901.dump`.

## Security hardening

- **Network:**
  - Only Caddy is reachable from outside. FastAPI binds `127.0.0.1:8000`, PostgreSQL listens on loopback only, and
    Caddy's admin API is loopback only.
  - The firewall rules are listed in the production runbook (steps 1 and 6).
- **Database:**
  - The API connects as `rag_app`. It has CONNECT, schema USAGE, SELECT/INSERT/UPDATE/DELETE on the 8 application
    tables, and USAGE/SELECT on their sequences. It is not an owner and has no DDL.
  - PUBLIC privileges on the database are revoked.
  - A statement timeout and pool limits are configured.
- **HTTP:**
  - Caddy sends HSTS, a strict Content-Security-Policy, `X-Frame-Options: DENY`, `nosniff`, a Referrer-Policy, a
    Permissions-Policy and COOP, and strips the `Server` header.
  - In production, `/docs`, `/redoc` and `/openapi.json` return 404, and `/health` returns only an overall status.
- **Authentication and authorization:**
  - Passwords are hashed with Argon2id; tokens are HS256 JWTs with the algorithm pinned and no claims trusted beyond
    the user id.
  - Role and departments are read from the database on every request.
  - The access filter runs in SQL before ranking. Admin routes are tenant-scoped.
  - Public registration never creates an admin.
- **Abuse limits** (production):
  - Rate limits on login, registration, queries, Gemini answers and uploads.
  - Upload limits: 10 MB, 300 PDF pages, 1,000,000 text characters, 2,000 chunks.
  - Per-tenant quotas on document count and stored bytes.
  - A request-body cap at both Caddy and the API.
- **Logging:**
  - JSON lines carrying a request id.
  - Paths are logged without query strings.
  - Errors return a generic message to the client and the full traceback to the log only.
- **Secrets:**
  - `.env`, `.env.*`, `deploy/caddy.env`, `*.dump`, `backups/`, `/logs/`, `data/uploads/` (tenant uploads) and
    `data/processed/*` are git-ignored. The benchmark folder corpus in `data/documents/` is tracked, and it is
    synthetic data under the MIT licence.
  - The backup tool scans each dump for secrets.
  - Tests sign tokens with a random key and never read the production `.env`.
- **Models:** with `HF_HUB_OFFLINE=1` the models load from the local cache, so the Hugging Face Hub isn't contacted.

## Tests

**Strategy.** Backend tests never touch production data:
- `tests/isolated_db.py` dumps the live database read-only and restores it into a throwaway `<db>_testrun_<hex>` copy.
- In the copy it keeps only the default tenant's folder corpus, removes users, departments, links, uploads and other
  tenants, and resets every document's visibility to `company`. That last step is the change committed in `1992fbf`;
  it keeps the starting point independent of production's access state.
- Each run gets a throwaway database role, and both the copy and the role are dropped at exit.
- Settings come from `RAG_TEST_ENV_FILE`, `.env.test` or `.env.development`. A file containing `APP_ENV=production`
  is always skipped.

**Coverage includes:**
- authentication;
- the access filter (including SQL filtering before `LIMIT`);
- the Admin API;
- document listing, uploads and deletes under authorization;
- upload limits;
- tenancy isolation;
- the database privileges of `rag_app`;
- production startup checks;
- backup and restore, and scheduled backups;
- the Windows startup tasks;
- end-to-end authorization, where a reranker spy and an LLM recorder show exactly which chunks reach each stage.

| Suite | Command | Result (2026-10-01) |
|---|---|---|
| Backend (218 tests) | `.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"` | **OK.** Before the rollout: 218 passed in 304 s. After the rollout: 218 passed in 323 s. |
| Frontend (4 files, 39 tests) | `cd frontend; npm test` | **39 passed** |
| Production authorization smoke test (rollout) | in-process production app; temporary Engineering and Finance employees plus the admin; no Gemini calls | Document totals were correct (admin 1,224, Engineering 715, Finance 72). Every allowed document was retrieved and every disallowed one was not. Sources, citations and LLM context were authorized in all 15 queries. The one harness false positive is explained below. |

**The false positive.** It came from matching chunks by text: the heading `## Escalation paths` appears in both an
Engineering document and a document Finance may read. The harness now traces each reranker candidate and LLM source
through `chunk_id` → `document_id` → source path. A focused rerun of the Finance company-query check passed: all 50
reranker candidates, and all 10 chunks sent to the LLM, came from documents Finance may read.

## Operational commands

Run these from the project root in PowerShell. `python` means `.venv\Scripts\python.exe` (Python 3.12).

| Purpose | Command |
|---|---|
| Ingest or refresh the folder corpus (default tenant) | `python -m src.rag.ingest` |
| Ingest another tenant's folder | `python -m src.rag.ingest --tenant <slug> --documents-dir <folder> --create-tenant` |
| Ask from the CLI without Gemini (operator scope) | `python -m src.rag "<question>" --retrieve-only` |
| Create a user | `python -m src.rag.users create --tenant default --email <email>` |
| Reset a password | `python -m src.rag.users set-password --email <email>` |
| Make a user admin or employee | `python -m src.rag.users set-role --email <email> --role admin` |
| Create the app role / re-grant privileges | `python -m src.rag.db_roles create-app-role --role rag_app` / `... grant --role rag_app` |
| Backup / verify (owner connection) | `python -m src.rag.backup create --out <file>` / `python -m src.rag.backup verify --dump <file>` |
| Scheduled backup by hand | `powershell -ExecutionPolicy Bypass -File deploy\run_backup_task.ps1 -BackupDir C:\rag-backups -LogDir C:\rag-logs` |
| Task status | `Get-ScheduledTaskInfo -TaskPath "\EnterpriseRAG\" -TaskName "RAG API"` (also `RAG Caddy`, `RAG Backup`) |
| Health | `Invoke-RestMethod https://<site>/api/v1/health` |
| Backend tests | `python -m unittest discover -s tests -p "test_*.py"` |
| Frontend build / tests | `cd frontend; npm run build` / `npm test` |

To check the access state read-only (owner connection, `psql`):

```sql
SELECT visibility, count(*) FROM documents GROUP BY 1;                       -- company 33, departments 1191
SELECT dp.name, count(*) FROM document_departments dd
  JOIN departments dp ON dp.id = dd.department_id GROUP BY 1 ORDER BY 1;     -- 1,373 links in total
SELECT d.id FROM documents d
 WHERE d.visibility = 'departments'
   AND NOT EXISTS (SELECT 1 FROM document_departments dd WHERE dd.document_id = d.id);  -- 25 admin-only (9 + 16 pending)
```

Make authorization changes through the Admin API or the admin panel, never with direct SQL.

## Git checkpoint

| Commit | Summary |
|---|---|
| `1992fbf` | Checkpoint: complete production authorization rollout (test isolation resets document visibility) |
| `b26baad` | Checkpoint: isolate backend tests from production database |
| `bb3056d` | Checkpoint: update Gemini model integration |
| `e643e49` | Checkpoint: harden Caddy startup reliability |
| `03d6c8b` | Checkpoint: fix production startup tasks |
| `49beeec` | Checkpoint: add scheduled database backups |

## Open items

- **Pending review:** 16 documents are held admin-only until a human decides; see
  [AUTHORIZATION.md](AUTHORIZATION.md#pending-review-16).
- **Memberships:** none exist yet. Assign employees to departments as they are onboarded.
- **Gemini:** the model is `gemini-3.8-flash` (free tier, about 20 answers a day). An end-to-end generated answer
  hasn't been confirmed since the model change, because earlier attempts returned 503 (high demand). Retrieval-only
  queries are unaffected.
- **User deletion:** there is no delete-user endpoint yet.
- **Going public:** HTTPS on a public domain and opening the firewall for 80/443 are runbook step 6. Do them only
  when the site goes public.
