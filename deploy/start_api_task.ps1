# Start the API from a Windows scheduled task (registered by register_startup_tasks.ps1).
#
# Waits until PostgreSQL is running and accepting connections on localhost, then runs
# deploy\run_api.ps1 (loopback, one worker, proxy headers, no Server header) with its
# output in dated log files. Exits with the API's exit code, so the task's
# "restart on failure" setting applies.
#
#   -LogDir           folder for api-<time>.*.log (created if missing)
#   -HfHome           Hugging Face cache that holds the downloaded models. Needed because a
#                     task may not get the interactive user's environment, and the model
#                     library reads HF_HOME before .env is loaded.
#   -PostgresService  Windows service name (default postgresql-x64-18)
#   -PostgresPort     default 5432
#   -WaitSeconds      how long to wait for PostgreSQL (default 300)

param(
    [Parameter(Mandatory = $true)] [string] $LogDir,
    [string] $HfHome = "",
    [string] $PostgresService = "postgresql-x64-18",
    [int] $PostgresPort = 5432,
    [int] $WaitSeconds = 300
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$log = Join-Path $LogDir ("api-" + (Get-Date -Format "yyyyMMdd-HHmmss"))

function Write-TaskLog([string] $message) {
    Add-Content -Path "$log.task.log" -Value ("{0} {1}" -f (Get-Date -Format o), $message)
}

# 1. PostgreSQL first.
$deadline = (Get-Date).AddSeconds($WaitSeconds)
while ($true) {
    $ready = $false
    $service = Get-Service -Name $PostgresService -ErrorAction SilentlyContinue
    if ($service -and $service.Status -eq "Running") {
        $client = New-Object System.Net.Sockets.TcpClient
        try { $ready = $client.ConnectAsync("127.0.0.1", $PostgresPort).Wait(2000) -and $client.Connected } catch { $ready = $false } finally { $client.Close() }
    }
    if ($ready) { break }
    if ((Get-Date) -gt $deadline) {
        Write-TaskLog "PostgreSQL ($PostgresService, port $PostgresPort) not ready after $WaitSeconds s; giving up"
        exit 1
    }
    Start-Sleep -Seconds 5
}
Write-TaskLog "PostgreSQL ready; starting the API"

# 2. The API itself (settings, including APP_ENV=production, come from the project's .env).
if ($HfHome) { $env:HF_HOME = $HfHome }
$runApi = Join-Path $root "deploy\run_api.ps1"
$process = Start-Process -FilePath "powershell.exe" `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$runApi`"") `
    -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput "$log.out.log" -RedirectStandardError "$log.err.log"
Write-TaskLog "API exited with code $($process.ExitCode)"
exit $process.ExitCode
