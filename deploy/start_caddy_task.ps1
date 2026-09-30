# Start Caddy from a Windows scheduled task (registered by register_startup_tasks.ps1).
#
# Waits until the API answers its health check on the loopback interface (status "ok"
# or "degraded"), then runs
# Caddy with deploy\Caddyfile and the variables from -EnvFile (SITE_ADDRESS,
# FRONTEND_DIST, CADDY_LOG_DIR, CADDY_ADMIN; see caddy.env.example). The file is read
# here (KEY=VALUE lines; blank lines, # comments and a UTF-8 byte-order mark are
# ignored) and must define SITE_ADDRESS and FRONTEND_DIST. Caddy's own messages go to
# dated log files; its access log goes to CADDY_LOG_DIR. Exits with Caddy's exit code,
# so the task's "restart on failure" setting applies.

param(
    [Parameter(Mandatory = $true)] [string] $CaddyExe,
    [Parameter(Mandatory = $true)] [string] $EnvFile,
    [Parameter(Mandatory = $true)] [string] $LogDir,
    [string] $Config = "",
    [string] $HealthUrl = "http://127.0.0.1:8000/api/v1/health",
    [int] $WaitSeconds = 600
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $Config) { $Config = Join-Path $root "deploy\Caddyfile" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$log = Join-Path $LogDir ("caddy-" + (Get-Date -Format "yyyyMMdd-HHmmss"))

function Write-TaskLog([string] $message) {
    Add-Content -Path "$log.task.log" -Value ("{0} {1}" -f (Get-Date -Format o), $message)
}

foreach ($file in @($CaddyExe, $EnvFile, $Config)) {
    if (-not (Test-Path -LiteralPath $file)) { Write-TaskLog "missing file: $file"; exit 1 }
}

# Caddy's settings, read here so that editors' byte-order marks or stray spaces cannot
# silently drop a variable (Caddy would then fall back to its defaults).
$settings = @{}
foreach ($line in (Get-Content -LiteralPath $EnvFile -Encoding UTF8)) {
    $line = $line.Trim().TrimStart([char]0xFEFF)
    if (-not $line -or $line.StartsWith("#")) { continue }
    if ($line -notmatch '^([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$') { Write-TaskLog "malformed line in $EnvFile (expected KEY=VALUE)"; exit 1 }
    $settings[$Matches[1]] = $Matches[2].Trim().Trim('"')
}
foreach ($required in @("SITE_ADDRESS", "FRONTEND_DIST")) {
    if (-not $settings[$required]) { Write-TaskLog "$required is not set in $EnvFile"; exit 1 }
}
if (-not (Test-Path -LiteralPath (Join-Path $settings["FRONTEND_DIST"] "index.html"))) {
    Write-TaskLog "FRONTEND_DIST has no index.html (build the frontend first)"; exit 1
}
foreach ($key in $settings.Keys) { Set-Item -Path "Env:$key" -Value $settings[$key] }

# 1. The API first (it loads the models, which takes a while after boot). Ready means
# HTTP 200 with status "ok" or "degraded" (degraded = AI answers unavailable, sources
# still work: the site must be served). "unavailable" (503) or no answer = keep waiting.
# Every change in what the API reports is logged, so a stuck wait is visible.
Write-TaskLog "waiting for the API at $HealthUrl (up to $WaitSeconds s)"
$deadline = (Get-Date).AddSeconds($WaitSeconds)
$observed = ""
while ($true) {
    try {
        $response = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 5
        $status = ($response.Content | ConvertFrom-Json).status
        $state = "HTTP $([int]$response.StatusCode) status=$status"
        $ready = ([int]$response.StatusCode -eq 200) -and ($status -in @("ok", "degraded"))
    } catch {
        $code = $null
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        $state = if ($code) { "HTTP $code" } else { "no answer ($($_.Exception.Message))" }
        $ready = $false
    }
    if ($ready) { break }
    if ($state -ne $observed) { Write-TaskLog "API not ready yet: $state"; $observed = $state }
    if ((Get-Date) -gt $deadline) { Write-TaskLog "API not ready at $HealthUrl after $WaitSeconds s (last: $state); giving up"; exit 1 }
    Start-Sleep -Seconds 5
}
Write-TaskLog "API ready ($state); starting Caddy for $($settings['SITE_ADDRESS'])"

# 2. Caddy (inherits the settings above from this process's environment).
$process = Start-Process -FilePath $CaddyExe `
    -ArgumentList @("run", "--config", "`"$Config`"", "--adapter", "caddyfile") `
    -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput "$log.out.log" -RedirectStandardError "$log.err.log"
Write-TaskLog "Caddy exited with code $($process.ExitCode)"
exit $process.ExitCode
