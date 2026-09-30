# Production deployment (Windows, single machine)

```
Internet ──443/80──► Caddy (deploy/Caddyfile)            admin API: 127.0.0.1 only (CADDY_ADMIN)
                       ├─ /           → frontend/dist (static files)
                       └─ /api/*      → 127.0.0.1:8000  FastAPI, 1 uvicorn worker (deploy/run_api.ps1)
                                            └─ localhost:5432  PostgreSQL, as least-privilege role rag_app
```

| Port | Listener | Must be reachable from | Inbound firewall |
|---|---|---|---|
| 80, 443 | Caddy | the internet (only once public) | allow, for caddy.exe only (step 3) |
| 8000 | uvicorn (FastAPI) | Caddy on the same machine | none (binds 127.0.0.1 only) |
| 5432 | PostgreSQL | the API and operator tools on the same machine | none; block rule as a safeguard |
| 2019 (or `CADDY_ADMIN`) | Caddy admin API | nothing remote | none (binds localhost only) |

Only Caddy is reachable from outside. FastAPI listens on 127.0.0.1 only; PostgreSQL
must listen on localhost only (step 2). Development (`APP_ENV` unset) is unchanged.

Run every command from the project root. Steps marked **(admin)** need an
elevated PowerShell ("Run as administrator").

## 1. Create the API's database role

Uses the owner credentials (`PGUSER`/`PGPASSWORD`) from `.env`, applies any pending
schema migration, creates role `rag_app` with a random password, grants it row
access only, and writes `APP_DB_USER`/`APP_DB_PASSWORD` into `.env` (never printed):

```powershell
.venv\Scripts\python.exe -m src.rag.db_roles create-app-role --role rag_app
```

Re-run later with `grant` instead of `create-app-role` if a migration adds tables
(default privileges already cover tables created by the owner). Neither command
ever puts a password in source code or committed files.

Once `APP_DB_USER` is set, the API (also in development) connects as `rag_app`;
ingestion, migrations, user management and backups keep using the owner.

**What `rag_app` may do — and nothing else:**

| Object | Privileges |
|---|---|
| role attributes | `LOGIN` only: not superuser, no `CREATEROLE`, `CREATEDB`, `REPLICATION`, `BYPASSRLS`; member of no role; owns nothing |
| database `enterprise_rag` | `CONNECT` (no `CREATE`, no `TEMPORARY`); `PUBLIC` has no privileges on this database |
| schema `public` | `USAGE` (no `CREATE`) |
| the 8 application tables | `SELECT`, `INSERT`, `UPDATE`, `DELETE` (no `TRUNCATE`, `REFERENCES`, `TRIGGER`) |
| their sequences | `USAGE`, `SELECT` (no `setval`) |

It cannot create, alter or drop tables, indexes, schemas, functions, extensions or
roles, cannot grant itself or others anything, cannot read or write server files or
run server programs (`COPY … PROGRAM`, `pg_read_file`, `lo_import`), and cannot read
other databases. `tests/test_db_privileges.py` proves all of this — and that every API
feature works as this role — in throwaway databases, never in `enterprise_rag`. In
production the API also refuses to start as a superuser or as the tables' owner.

## 2. PostgreSQL: listen on localhost only (admin)

Check the current state (owner connection; prints no secrets):

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -h localhost -U postgres -d postgres -c "SHOW listen_addresses;" -c "SELECT line_number, type, database, user_name, address, auth_method FROM pg_hba_file_rules;"
Get-NetTCPConnection -LocalPort 5432 -State Listen | Select-Object LocalAddress   # goal: only 127.0.0.1 and ::1
```

The default Windows installation listens on **all** interfaces (`listen_addresses = '*'`,
i.e. `0.0.0.0` and `::`). Its `pg_hba.conf` accepts only local and loopback
(`127.0.0.1/32`, `::1/128`) connections with `scram-sha-256`, so remote logins are
refused — but the port is still open to the network. Close it (`pg_hba` already makes
this safe: no remote client can log in today):

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -h localhost -U postgres -d postgres -c "ALTER SYSTEM SET listen_addresses = 'localhost';"
Restart-Service postgresql-x64-18
Get-NetTCPConnection -LocalPort 5432 -State Listen | Select-Object LocalAddress   # expect only 127.0.0.1 and ::1
```

`ALTER SYSTEM` writes `postgresql.auto.conf` in the data folder; undo with
`ALTER SYSTEM RESET listen_addresses;` and a restart. Stop the API before the restart
and start it again afterwards (step 6).

Keep `pg_hba.conf` (`C:\Program Files\PostgreSQL\18\data\pg_hba.conf`, edit as admin)
limited to the loopback `host` lines with `scram-sha-256`. Optionally, confine `rag_app`
to its own database — PostgreSQL lets every role *connect* to other databases (such as
`postgres`) by default, although `rag_app` can read nothing there. Put these lines
**above** the general `host all all …` lines, then reload:

```
host    enterprise_rag    rag_app    127.0.0.1/32    scram-sha-256
host    enterprise_rag    rag_app    ::1/128         scram-sha-256
host    all               rag_app    all             reject
```

`psql -h localhost -U postgres -d postgres -c "SELECT pg_reload_conf();"` (no restart
needed), then check `SELECT line_number, database, user_name, error FROM pg_hba_file_rules;`
shows no errors.

## 3. Windows Firewall (admin)

Windows blocks unsolicited inbound traffic by default (check:
`netsh advfirewall show allprofiles firewallpolicy` → `BlockInbound,AllowOutbound` for
every profile). Required inbound access:

- **80 and 443 → Caddy only**, and only when the site goes public (certificate
  challenges, HTTP→HTTPS redirect, HTTPS).
- **Nothing else.** uvicorn (8000) and Caddy's admin API listen on loopback only;
  PostgreSQL gets an explicit block rule as a safeguard in case a later change makes it
  listen on the network again.

```powershell
# Safeguards: nothing outside may reach PostgreSQL or the API directly.
New-NetFirewallRule -DisplayName "Block PostgreSQL inbound (5432)" -Direction Inbound -Protocol TCP -LocalPort 5432 -Action Block
New-NetFirewallRule -DisplayName "Block RAG API inbound (8000)"    -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Block

# Only when going public: let Caddy (this program only) receive HTTP and HTTPS.
New-NetFirewallRule -DisplayName "Caddy HTTP/HTTPS inbound" -Direction Inbound -Protocol TCP -LocalPort 80,443 `
    -Program "C:\Program Files\Caddy\caddy.exe" -Action Allow        # adjust to where caddy.exe is installed
```

Loopback traffic (Caddy → API → PostgreSQL) is not affected by these rules. Review
existing "allow" rules for interpreters too (`Get-NetFirewallRule -Direction Inbound
-Action Allow -Enabled True`): an allow rule for a Python executable on any port lets
*any* server started with that interpreter be reached from the network.

## 4. Production `.env`

Create it from `deploy/env.production.example` with fresh values; do not reuse the
development `.env`. Keep it readable only by the account that runs the API.

| Variable | Required | Notes |
|---|---|---|
| `APP_ENV` | yes | `production` |
| `APP_DB_USER` / `APP_DB_PASSWORD` | yes | written by step 1 (`rag_app`) |
| `PGHOST` / `PGPORT` / `PGDATABASE` | yes | `localhost` / `5432` / `enterprise_rag` |
| `JWT_SECRET_KEY` | yes | a **new** secret: `.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `HF_HUB_OFFLINE` | recommended | `1` (models are already cached) |
| `UPLOAD_DIR` | recommended | absolute path, writable by the API account, included in backups |
| `GEMINI_API_KEY` | optional | without it, AI answers are off (sources still work) |
| `PGUSER` / `PGPASSWORD` | no | owner credentials; set them in the operator's shell session for migrations, ingestion, user management and backups instead of storing them here |

Limits, rate limits, pool and logging settings have safe defaults (see the example file).
In production the API **refuses to start** with a weak/placeholder `JWT_SECRET_KEY`,
without `APP_DB_USER`, or if it would connect as a superuser or as the tables' owner.

Caddy reads its own variables from the environment it is started in:
`SITE_ADDRESS` (public hostname), `FRONTEND_DIST` (absolute path to `frontend\dist`),
`CADDY_LOG_DIR` (access logs), and `CADDY_ADMIN` (default `localhost:2019`; on
machines where Windows reserves port 2019 — Caddy then fails with "listen tcp
127.0.0.1:2019 … forbidden by its access permissions" — use e.g. `localhost:2999`).

## 5. Build the frontend

```powershell
npm --prefix frontend ci
npm --prefix frontend run build      # → frontend/dist
```

## 6. Start the services (in this order)

1. **PostgreSQL** — Windows service `postgresql-x64-18` (startup type Automatic).
2. **API** — waits for nothing but the database; loads the models, then listens:
   ```powershell
   powershell -ExecutionPolicy Bypass -File deploy\run_api.ps1
   ```
   `run_api.ps1` runs uvicorn with `--host 127.0.0.1 --port 8000 --workers 1
   --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-server-header --no-access-log`.
   Wait until `Invoke-RestMethod http://127.0.0.1:8000/api/v1/health` returns `ok`.
3. **Caddy** (install once, e.g. `winget install CaddyServer.Caddy`, or download `caddy.exe` from caddyserver.com):
   ```powershell
   $env:SITE_ADDRESS  = "knowledge.example.com"                       # your domain; DNS must point here
   $env:FRONTEND_DIST = (Resolve-Path frontend\dist).Path
   $env:CADDY_LOG_DIR = "C:\rag-logs"
   # $env:CADDY_ADMIN = "localhost:2999"                              # only if port 2019 is blocked
   caddy validate --config deploy\Caddyfile
   caddy run --config deploy\Caddyfile
   ```

Stop in the reverse order (Caddy, API, then PostgreSQL if needed). For a public
domain, ports 80 and 443 must reach this machine (router port forward) so Caddy can
obtain the Let's Encrypt certificate. To keep the API and Caddy running after logout,
register them as services or scheduled tasks ("At startup"), in the order above.

## 7. Check

```powershell
Invoke-RestMethod https://knowledge.example.com/api/v1/health          # → status: ok (nothing else in production)
(Invoke-WebRequest https://knowledge.example.com/).Headers["Content-Security-Policy"]

# Listeners: 8000 and the Caddy admin port on 127.0.0.1 only; 5432 on 127.0.0.1/::1 only (after step 2).
Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 5432,8000,2019,2999,80,443 |
    Select-Object LocalAddress, LocalPort, @{n='Process';e={(Get-Process -Id $_.OwningProcess).ProcessName}}

# From ANOTHER machine on the network (replace the address): only 80/443 may connect.
Test-NetConnection 192.168.1.50 -Port 443      # TcpTestSucceeded : True   (when public)
Test-NetConnection 192.168.1.50 -Port 8000     # False
Test-NetConnection 192.168.1.50 -Port 5432     # False

# The API's database sessions use rag_app only (owner connection):
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -h localhost -U postgres -d enterprise_rag -c "SELECT usename, count(*) FROM pg_stat_activity WHERE datname = 'enterprise_rag' AND pid <> pg_backend_pid() GROUP BY 1;"
```

The API writes one JSON line per request (request id, method, path without query
string, status, duration, client IP); uvicorn's own access log stays off. Caddy writes
its JSON access log to `CADDY_LOG_DIR\access.log` (authorization headers are not logged).

## 8. Backups

See [BACKUP.md](BACKUP.md): database dump and restore verification
(`python -m src.rag.backup`), uploaded files, secrets, and the recovery sequence.
