# Production deployment (Windows, single machine)

```
Internet ──443/80──► Caddy (deploy/Caddyfile)
                       ├─ /           → frontend/dist (static files)
                       └─ /api/*      → 127.0.0.1:8000  FastAPI, 1 uvicorn worker (deploy/run_api.ps1)
                                            └─ localhost:5432  PostgreSQL, as least-privilege role rag_app
```

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
(default privileges already cover tables created by the owner).

Once `APP_DB_USER` is set, the API (also in development) connects as `rag_app`;
ingestion, migrations and user management keep using the owner.

## 2. PostgreSQL: listen on localhost only (admin)

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -h localhost -U postgres -d postgres -c "ALTER SYSTEM SET listen_addresses = 'localhost';"
Restart-Service postgresql-x64-18
Get-NetTCPConnection -LocalPort 5432 -State Listen | Select-Object LocalAddress   # expect only 127.0.0.1 and ::1
```

Then open `C:\Program Files\PostgreSQL\18\data\pg_hba.conf` in an editor (admin) and
make sure the only `host` lines are the loopback ones, with `scram-sha-256`:

```
host    all    all    127.0.0.1/32    scram-sha-256
host    all    all    ::1/128         scram-sha-256
```

If you changed it: `psql -h localhost -U postgres -d postgres -c "SELECT pg_reload_conf();"`

## 3. Windows Firewall (admin)

```powershell
# Belt and braces: nothing outside may reach PostgreSQL or the API directly.
New-NetFirewallRule -DisplayName "Block PostgreSQL inbound (5432)" -Direction Inbound -Protocol TCP -LocalPort 5432 -Action Block
New-NetFirewallRule -DisplayName "Block RAG API inbound (8000)"    -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Block

# Only when going public: let Caddy receive HTTP (certificate challenges, redirect) and HTTPS.
New-NetFirewallRule -DisplayName "Caddy HTTP/HTTPS inbound" -Direction Inbound -Protocol TCP -LocalPort 80,443 -Action Allow
```

Loopback traffic (Caddy → API → PostgreSQL) is not affected by these rules.

## 4. Production `.env`

Create it from `deploy/env.production.example` with fresh values; do not reuse the
development `.env`:

- `APP_ENV=production`
- a **new** `JWT_SECRET_KEY`: `.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(64))"`
- `APP_DB_USER` / `APP_DB_PASSWORD` from step 1
- `HF_HUB_OFFLINE=1` (models are already cached)
- keep the owner password out of this file if you can; set `PGUSER`/`PGPASSWORD` in
  the shell session when running migrations or ingestion

In production the API **refuses to start** if it would connect as a superuser or as
the owner of the tables.

## 5. Build the frontend

```powershell
npm --prefix frontend ci
npm --prefix frontend run build      # → frontend/dist
```

## 6. Start the API and Caddy

```powershell
powershell -ExecutionPolicy Bypass -File deploy\run_api.ps1
```

Caddy (install once, e.g. `winget install CaddyServer.Caddy`, or download `caddy.exe` from caddyserver.com):

```powershell
$env:SITE_ADDRESS  = "knowledge.example.com"                       # your domain; DNS must point here
$env:FRONTEND_DIST = (Resolve-Path frontend\dist).Path
$env:CADDY_LOG_DIR = "C:\rag-logs"
caddy validate --config deploy\Caddyfile
caddy run --config deploy\Caddyfile
```

For a public domain, ports 80 and 443 must reach this machine (router port forward)
so Caddy can obtain the Let's Encrypt certificate. To keep both processes running
after logout, register them as services or scheduled tasks ("At startup").

## 7. Check

```powershell
Invoke-RestMethod https://knowledge.example.com/api/v1/health          # → status: ok (nothing else in production)
(Invoke-WebRequest https://knowledge.example.com/).Headers["Content-Security-Policy"]
Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 5432,8000 | Select-Object LocalAddress, LocalPort   # loopback only
```
