# One scheduled database backup (run by the "RAG Backup" task; see register_backup_task.ps1).
#
# Runs python -m src.rag.backup_schedule: a timestamped pg_dump (custom format) into
# -BackupDir, checked with pg_restore --list, then retention (-Keep newest backups of
# this database; nothing else is ever deleted). Results are appended to
# <LogDir>\backup.log. Exits with the backup's exit code (0 ok, 1 backup failed,
# 2 retention failed), so Task Scheduler records failures.
#
# Credentials: the database owner's password is NOT passed here. PostgreSQL's client
# tools read it from the password file -PgPassFile (default location
# %APPDATA%\postgresql\pgpass.conf, readable only by the task's account); -DbUser names
# the owner role. Host, port and database come from the project's .env.

param(
    [Parameter(Mandatory = $true)] [string] $BackupDir,
    [Parameter(Mandatory = $true)] [string] $LogDir,
    [int] $Keep = 14,
    [string] $DbUser = "postgres",
    [string] $PgPassFile = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-TaskLog([string] $message) {
    Add-Content -Path (Join-Path $LogDir "backup.log") -Value ("{0} {1}" -f (Get-Date -Format o), $message)
}

if ($PgPassFile) {
    if (-not (Test-Path -LiteralPath $PgPassFile)) { Write-TaskLog "FAILED backup: password file not found: $PgPassFile"; exit 1 }
    $env:PGPASSFILE = $PgPassFile
}
$env:PGUSER = $DbUser

Set-Location $root
& (Join-Path $root ".venv\Scripts\python.exe") -m src.rag.backup_schedule --dir $BackupDir --keep $Keep --log-dir $LogDir
exit $LASTEXITCODE
