# Production runbook (administrator steps)

The remaining production steps change the machine (PostgreSQL configuration, Windows
Firewall, startup tasks, public exposure). Nothing here is done automatically: run each
step yourself, in order, from an **elevated** PowerShell ("Run as administrator") in the
project root, verify it, and keep the rollback at hand. No command prints a secret.

Common variables for the commands below (set them once per PowerShell window):

```powershell
Set-Location "C:\path\to\Enterprise Multi-Tenant Knowledge"        # the project root
$psql = "C:\Program Files\PostgreSQL\18\bin\psql.exe"
$env:PGHOST = "localhost"; $env:PGUSER = "postgres"                # owner, for psql and operator tools
# psql asks for the owner password each time (or set $env:PGPASSWORD in this window only).
```

## Order

| # | Step | Needs admin | Reversible |
|---|---|---|---|
| 0 | Backup the database (and verify it) | no | — |
| 1 | Firewall: block rules; review the Python 3.14 allow rules | yes | yes |
| 2 | PostgreSQL: listen on localhost only (restart) | yes | yes |
| 3 | Optional: `pg_hba` — `rag_app` may reach only `enterprise_rag` | yes | yes |
| 4 | Production `.env` | no | yes |
| 5 | Caddy installed, `caddy.env`, startup tasks (PostgreSQL → API → Caddy) | yes | yes |
| 6 | HTTPS / public domain (last) | yes | yes |
| 7 | Scheduled daily database backups | yes (registration) | yes |

## 0. Backup first

```powershell
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
.venv\Scripts\python.exe -m src.rag.backup create --out "D:\rag-backups\enterprise_rag-$stamp.dump"
.venv\Scripts\python.exe -m src.rag.backup verify --dump "D:\rag-backups\enterprise_rag-$stamp.dump"   # RESULT: OK
```

## 1. Windows Firewall (admin)

Windows already blocks unsolicited inbound traffic (`netsh advfirewall show allprofiles
firewallpolicy` → `BlockInbound,AllowOutbound`). These rules add explicit safeguards and
are grouped as "Enterprise RAG" so they can be listed and removed together.

```powershell
New-NetFirewallRule -Group "Enterprise RAG" -DisplayName "Enterprise RAG - block PostgreSQL inbound (5432)" `
    -Direction Inbound -Protocol TCP -LocalPort 5432 -Action Block -Profile Any
New-NetFirewallRule -Group "Enterprise RAG" -DisplayName "Enterprise RAG - block API inbound (8000)" `
    -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Block -Profile Any
```

The Caddy allow rule for 80/443 is created only when going public (step 6).

**Existing Python 3.14 rules.** Two inbound rules named `Python` allow
`C:\python314\python.exe` to receive **any** TCP and UDP traffic on Public networks.
Windows created them when that interpreter first listened on a port and the "allow
access" prompt was accepted. The API does not use that interpreter (it runs from `.venv`,
based on Python 3.12), but any server started with Python 3.14 would be reachable from
the network. Review, then disable (reversible):

```powershell
$pythonRules = @(
    "TCP Query User{E7E107C3-204F-4C0F-8A08-F5512281811E}C:\python314\python.exe",
    "UDP Query User{5F2E7E4D-486E-44D6-8F68-9913D176A3D6}C:\python314\python.exe")
Get-NetFirewallRule -Name $pythonRules | Format-Table Name, DisplayName, Enabled, Action, Profile
Disable-NetFirewallRule -Name $pythonRules
```

Rollback: `Enable-NetFirewallRule -Name $pythonRules`. To delete them instead:
`Remove-NetFirewallRule -Name $pythonRules` (Windows asks again the next time Python 3.14
listens on a port).

**Verify:**

```powershell
Get-NetFirewallRule -Group "Enterprise RAG" | Format-Table DisplayName, Enabled, Action, Profile
Get-NetFirewallRule -Name $pythonRules | Format-Table DisplayName, Enabled
# From ANOTHER machine on the network (replace the address with this machine's):
Test-NetConnection 192.168.1.50 -Port 5432     # TcpTestSucceeded : False
Test-NetConnection 192.168.1.50 -Port 8000     # TcpTestSucceeded : False
```

**Rollback:** `Remove-NetFirewallRule -Group "Enterprise RAG"`

## 2. PostgreSQL: listen on localhost only (admin)

Today `listen_addresses = '*'` (from `postgresql.conf`, line 60), so PostgreSQL listens
on every interface. `pg_hba.conf` accepts only loopback connections, so this change
cannot lock out anything that works today. `ALTER SYSTEM` writes the setting to
`postgresql.auto.conf` (currently empty), which overrides `postgresql.conf`.

Stop the API first if it is running (`Stop-ScheduledTask -TaskPath "\EnterpriseRAG\" -TaskName "RAG API"`
once step 5 is done; otherwise stop `run_api.ps1`).

```powershell
& $psql -d postgres -c "ALTER SYSTEM SET listen_addresses = 'localhost';"
Restart-Service postgresql-x64-18
```

**Verify:**

```powershell
& $psql -d postgres -c "SHOW listen_addresses;"                 # localhost
Get-NetTCPConnection -LocalPort 5432 -State Listen | Select-Object LocalAddress   # only 127.0.0.1 and ::1
$lan = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias "Wi-Fi").IPAddress   # adjust the interface name
(Test-NetConnection $lan -Port 5432 -WarningAction SilentlyContinue).TcpTestSucceeded   # False
.venv\Scripts\python.exe deploy\check_db_access.py              # API connection: OK as rag_app to enterprise_rag
```

**Rollback:**

```powershell
& $psql -d postgres -c "ALTER SYSTEM RESET listen_addresses;"
Restart-Service postgresql-x64-18
```

## 3. Optional: confine `rag_app` to `enterprise_rag` (admin)

PostgreSQL lets every role *log in* to every database unless `pg_hba.conf` says
otherwise; `rag_app` can read nothing in `postgres` or `youtube_trending_db`, but it
can connect. These three rules — placed **before** the existing rules, because the first
matching line wins — allow `rag_app` only into `enterprise_rag` from loopback and
reject it everywhere else. Other roles are not affected. The API connects over IPv6
loopback (`::1`), so both loopback lines are needed.

```powershell
$hba = "C:\Program Files\PostgreSQL\18\data\pg_hba.conf"
Copy-Item -LiteralPath $hba -Destination "$hba.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
$lines = @(Get-Content -LiteralPath $hba)
if ($lines -match '^\s*host\s+\S+\s+rag_app\s') { "rag_app rules already present - nothing changed" } else {
    $first = ($lines | Select-String -Pattern '^\s*(local|host|hostssl|hostnossl|hostgssenc|hostnogssenc)\s' | Select-Object -First 1).LineNumber - 1
    $rules = @(
        "# Enterprise RAG: the API role may only reach its own database, from loopback.",
        "host    enterprise_rag    rag_app    127.0.0.1/32    scram-sha-256",
        "host    enterprise_rag    rag_app    ::1/128         scram-sha-256",
        "host    all               rag_app    all             reject",
        "")
    $updated = @($lines[0..($first - 1)]) + $rules + @($lines[$first..($lines.Count - 1)])
    [IO.File]::WriteAllLines($hba, [string[]]$updated, (New-Object Text.UTF8Encoding $false))
    "rules inserted before line $($first + 1)"
}
& $psql -d postgres -c "SELECT pg_reload_conf();"
```

A reload with an invalid `pg_hba.conf` keeps the previous rules active, so always check:

**Verify:**

```powershell
& $psql -d postgres -c "SELECT line_number, database, user_name, address, auth_method, error FROM pg_hba_file_rules ORDER BY rule_number;"
# the three rag_app lines come first; the error column is empty on every line
.venv\Scripts\python.exe deploy\check_db_access.py
# API connection: OK as rag_app to enterprise_rag
# other database 'postgres': rejected by pg_hba (expected with the optional rules)
```

**Rollback:** copy the `.bak-…` file back over `pg_hba.conf`, then
`& $psql -d postgres -c "SELECT pg_reload_conf();"`.

## 4. Production `.env` (no admin needed)

The API reads `<project root>\.env`. Keep the development file, then create the
production one from the template. Secrets are generated straight into the file and
never displayed.

```powershell
Copy-Item .env .env.development                  # git-ignored (.env.*); your development settings
Copy-Item deploy\env.production.example .env     # then edit: GEMINI_API_KEY (optional), UPLOAD_DIR

# A fresh JWT secret, written without being shown:
$secret = & .venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(64))"
(Get-Content .env) -replace '^JWT_SECRET_KEY=.*$', "JWT_SECRET_KEY=$secret" | Set-Content .env -Encoding ascii
Remove-Variable secret

# The upload folder (absolute path from UPLOAD_DIR):
New-Item -ItemType Directory -Force "C:\rag-data\uploads" | Out-Null

# A new rag_app password, written into .env as APP_DB_PASSWORD (needs the owner credentials
# in this window only; the production .env does not store them):
$env:PGUSER = "postgres"
$env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR((Read-Host "PostgreSQL owner password" -AsSecureString)))
.venv\Scripts\python.exe -m src.rag.db_roles create-app-role --role rag_app
Remove-Item Env:PGPASSWORD
```

`create-app-role` also resets the password the development `.env.development` holds for
`rag_app`; if you need development again, re-run it with the development file in place.

**Verify:**

```powershell
Select-String -Path .env -Pattern '^(APP_ENV|APP_DB_USER|HF_HUB_OFFLINE)=' | ForEach-Object Line   # production, rag_app, 1
(Select-String -Path .env -Pattern '^JWT_SECRET_KEY=(.{43,})$').Count            # 1 (a secret is set; not shown)
(Select-String -Path .env -Pattern '^APP_DB_PASSWORD=(.+)$').Count               # 1
.venv\Scripts\python.exe deploy\check_db_access.py                               # OK as rag_app
powershell -ExecutionPolicy Bypass -File deploy\run_api.ps1                      # in a second window
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health                            # status: ok  (then Ctrl+C the API)
```

In production the API refuses to start with a weak `JWT_SECRET_KEY`, without
`APP_DB_USER`, or as a superuser/table owner; the error names the problem. Note: the
backend test suite needs the development settings (owner credentials) — run tests with
`.env.development` in place, not on the production configuration.

**Rollback:** `Copy-Item .env.development .env`

## 5. Caddy and startup tasks (admin)

**Install Caddy** to `C:\Program Files\Caddy\caddy.exe` (e.g. download the Windows amd64
binary from caddyserver.com, or `winget install CaddyServer.Caddy` and note its path),
then check: `& "C:\Program Files\Caddy\caddy.exe" version`.

**Build the frontend:** `npm --prefix frontend ci; npm --prefix frontend run build`.

**Caddy settings:** `Copy-Item deploy\caddy.env.example deploy\caddy.env` and edit it.
Before going public, test without a certificate and without opening anything:
`SITE_ADDRESS=http://localhost:8080` (the firewall has no allow rule for 8080), and set
`FRONTEND_DIST` to the absolute path of `frontend\dist`, `CADDY_LOG_DIR=C:\rag-logs`,
`CADDY_ADMIN=localhost:2999` (on this machine Windows blocks Caddy's default admin port 2019).

```powershell
& "C:\Program Files\Caddy\caddy.exe" validate --config deploy\Caddyfile   # "Valid configuration"
```

**Startup tasks.** PostgreSQL is a Windows service (`postgresql-x64-18`, Automatic):
check with `Get-Service postgresql-x64-18 | Select-Object Status, StartType` and, if
needed, `Set-Service postgresql-x64-18 -StartupType Automatic`. The API and Caddy become
two scheduled tasks under `\EnterpriseRAG\`, started at boot, running as your account
(not SYSTEM, limited rights, no stored password), each waiting for its dependency:
`RAG API` waits until PostgreSQL accepts connections, `RAG Caddy` until
`http://127.0.0.1:8000/api/v1/health` answers HTTP 200 with `ok` or `degraded` (degraded =
no Gemini key: sources still work, so the site is served). Logs go to `C:\rag-logs` (`api-*.log`, `caddy-*.log`, plus Caddy's
`access.log`).

```powershell
powershell -ExecutionPolicy Bypass -File deploy\register_startup_tasks.ps1 -WhatIf   # dry run: shows the tasks, "Checks: OK"
powershell -ExecutionPolicy Bypass -File deploy\register_startup_tasks.ps1           # registers both tasks
Start-ScheduledTask -TaskPath "\EnterpriseRAG\" -TaskName "RAG API"
Start-ScheduledTask -TaskPath "\EnterpriseRAG\" -TaskName "RAG Caddy"                  # waits for the API
```

If registration fails because S4U logons are not allowed on this machine, add
`-LogonType Password` (asks for your Windows password; Task Scheduler stores it).

**Verify** (and again after a reboot):

```powershell
Get-ScheduledTask -TaskPath "\EnterpriseRAG\" | Format-Table TaskName, State
Get-ScheduledTaskInfo -TaskPath "\EnterpriseRAG\" -TaskName "RAG API" | Select-Object LastRunTime, LastTaskResult
Get-Content (Get-ChildItem C:\rag-logs\*.task.log | Sort-Object LastWriteTime | Select-Object -Last 2).FullName
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health                  # status: ok
Invoke-WebRequest http://localhost:8080/ -UseBasicParsing | Select-Object StatusCode   # 200 (the app, through Caddy)
Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 5432,8000,2999,8080,80,443 |
    Select-Object LocalAddress, LocalPort, @{n='Process';e={(Get-Process -Id $_.OwningProcess).ProcessName}}
# 5432 and 8000 on loopback only; 2999 on 127.0.0.1; Caddy on 8080 (all interfaces, but no firewall allow rule)
nvidia-smi   # the API's python.exe should appear (models on the GPU); otherwise it runs on the CPU (slower)
```

**Rollback:**

```powershell
powershell -ExecutionPolicy Bypass -File deploy\register_startup_tasks.ps1 -Unregister
Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 8000,8080,80,443,2999 |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }     # stops what the tasks started
```

## 6. HTTPS on a public domain (admin; last)

Checklist, in order:

1. **Domain.** You control a domain or subdomain, e.g. `knowledge.example.com`.
2. **Stable address.** Give this machine a fixed LAN address (router DHCP reservation).
3. **Internet reachability.** Your ISP gives the router a public IPv4 address (not
   carrier-grade NAT: the router's WAN address must equal what
   `Invoke-RestMethod https://api.ipify.org` reports) and does not block inbound 80/443.
4. **DNS.** An `A` record for the name points to that public address (an `AAAA` record
   only if IPv6 reaches this machine). Check: `Resolve-DnsName knowledge.example.com`.
5. **Router.** Forward TCP 80 and 443 (and UDP 443 for HTTP/3, optional) to this
   machine's LAN address. Forward nothing else — never 5432 or 8000.
6. **Power.** On AC power, the machine must not sleep:
   `powercfg /change standby-timeout-ac 0`.
7. **Firewall allow rule for Caddy only:**
   ```powershell
   New-NetFirewallRule -Group "Enterprise RAG" -DisplayName "Enterprise RAG - Caddy HTTP/HTTPS inbound" `
       -Direction Inbound -Protocol TCP -LocalPort 80,443 -Program "C:\Program Files\Caddy\caddy.exe" -Action Allow -Profile Any
   New-NetFirewallRule -Group "Enterprise RAG" -DisplayName "Enterprise RAG - Caddy HTTP/3 inbound" `
       -Direction Inbound -Protocol UDP -LocalPort 443 -Program "C:\Program Files\Caddy\caddy.exe" -Action Allow -Profile Any
   ```
8. **SITE_ADDRESS.** In `deploy\caddy.env`: `SITE_ADDRESS=knowledge.example.com` (no
   `http://`), then restart Caddy:
   ```powershell
   Stop-ScheduledTask -TaskPath "\EnterpriseRAG\" -TaskName "RAG Caddy"
   Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 80,443,8080 | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
   Start-ScheduledTask -TaskPath "\EnterpriseRAG\" -TaskName "RAG Caddy"
   ```
9. **Certificate.** Caddy obtains it from Let's Encrypt on its own within a minute. Watch
   `C:\rag-logs\caddy-*.err.log` for `certificate obtained successfully`; errors there
   usually mean DNS or port forwarding is not in place yet.
10. **External verification** — from a phone on mobile data or another network:
    ```powershell
    Invoke-RestMethod https://knowledge.example.com/api/v1/health          # {"status":"ok"} and nothing else
    (Invoke-WebRequest https://knowledge.example.com/ -UseBasicParsing).Headers["Strict-Transport-Security"]
    curl.exe -sI http://knowledge.example.com/ | Select-Object -First 3    # HTTP/1.1 308 Permanent Redirect, Location: https://…
    Test-NetConnection knowledge.example.com -Port 5432                     # False
    Test-NetConnection knowledge.example.com -Port 8000                     # False
    ```
    Then log in in a browser, ask a question, and optionally check the TLS setup at
    https://www.ssllabs.com/ssltest/.

**Rollback (take the site offline):** remove the two allow rules
(`Get-NetFirewallRule -Group "Enterprise RAG" | Where-Object Action -eq Allow | Remove-NetFirewallRule`),
remove the router forwards, and set `SITE_ADDRESS` back to `http://localhost:8080` and
restart Caddy.

## 7. Scheduled daily database backups (admin for registration)

Details, troubleshooting and recovery: [BACKUP.md](BACKUP.md), section 5. Defaults: daily at
02:30 (or as soon as the machine is on again), newest 14 backups kept in `C:\rag-backups`
(outside the project; copy it to another disk or machine regularly), log in
`C:\rag-logs\backup.log`. Only files named `enterprise_rag-YYYYMMDD-HHMMSS.dump` directly in
that folder are ever deleted, and only after a new backup passed its archive check.

1. **Owner password file** (no admin; hidden prompt; readable only by you) — the commands in
   BACKUP.md, "Owner credentials without a password in the task".
2. **One manual run** in a new window without `PGPASSWORD` set:
   ```powershell
   powershell -ExecutionPolicy Bypass -File deploy\run_backup_task.ps1 -BackupDir C:\rag-backups -LogDir C:\rag-logs -PgPassFile "$env:APPDATA\postgresql\pgpass.conf"
   Get-Content C:\rag-logs\backup.log -Tail 2                   # OK backup … / OK retention …
   ```
3. **Register** (admin):
   ```powershell
   powershell -ExecutionPolicy Bypass -File deploy\register_backup_task.ps1 -WhatIf   # "Checks: OK"
   powershell -ExecutionPolicy Bypass -File deploy\register_backup_task.ps1
   Start-ScheduledTask -TaskPath "\EnterpriseRAG\" -TaskName "RAG Backup"
   ```

**Verify:**

```powershell
Get-ScheduledTaskInfo -TaskPath "\EnterpriseRAG\" -TaskName "RAG Backup" | Select-Object LastRunTime, LastTaskResult, NextRunTime   # LastTaskResult 0
Get-Content C:\rag-logs\backup.log -Tail 2
.venv\Scripts\python.exe -m src.rag.backup verify --dump (Get-ChildItem C:\rag-backups -Filter "enterprise_rag-*.dump" | Sort-Object Name | Select-Object -Last 1).FullName   # weekly: RESULT: OK
```

**Rollback:** `powershell -ExecutionPolicy Bypass -File deploy\register_backup_task.ps1 -Unregister`
(existing backups are kept).
