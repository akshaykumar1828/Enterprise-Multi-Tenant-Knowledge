# Production API process (Windows PowerShell). Run from anywhere:
#   powershell -ExecutionPolicy Bypass -File deploy\run_api.ps1
#
# - Loopback only: Caddy on the same machine is the only client.
# - One worker: every worker loads the embedding and reranker models onto the
#   GPU, and requests already share one GPU lock inside the process.
# - Proxy headers are trusted only from Caddy (127.0.0.1), so client IPs in logs
#   are the real ones and nobody else can spoof them.
# - APP_ENV=production and the database settings come from .env.
# - uvicorn's own access log is off: the app writes one structured (JSON) line
#   per request with its request ID instead (LOG_REQUESTS).

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

& "$root\.venv\Scripts\python.exe" -m uvicorn src.api.main:app `
    --host 127.0.0.1 `
    --port 8000 `
    --workers 1 `
    --proxy-headers `
    --forwarded-allow-ips 127.0.0.1 `
    --no-server-header `
    --no-access-log `
    --log-level info
