# Register (or remove) the daily database backup task. Run as Administrator.
#
#   Dry run (no changes, no elevation needed):
#     powershell -ExecutionPolicy Bypass -File deploy\register_backup_task.ps1 -WhatIf
#   Register:
#     powershell -ExecutionPolicy Bypass -File deploy\register_backup_task.ps1
#   Remove (rollback; existing backups are not touched):
#     powershell -ExecutionPolicy Bypass -File deploy\register_backup_task.ps1 -Unregister
#
# Task \EnterpriseRAG\RAG Backup runs deploy\run_backup_task.ps1 every day at -At
# (default 02:30), as -UserId (default: the current user; logon type S4U = no stored
# Windows password). If the machine was off at that time, it runs as soon as possible.
#
# The database owner's password is never in the task: create the PostgreSQL password
# file -PgPassFile first (deploy\BACKUP.md, "Scheduled backups"); the task only passes
# its path and the owner role name.

param(
    [string] $BackupDir = "C:\rag-backups",
    [int] $Keep = 14,
    [string] $At = "02:30",
    [string] $LogDir = "C:\rag-logs",
    [string] $DbUser = "postgres",
    [string] $PgPassFile = (Join-Path $env:APPDATA "postgresql\pgpass.conf"),
    [string] $UserId = "$env:USERDOMAIN\$env:USERNAME",
    [ValidateSet("S4U", "Password")] [string] $LogonType = "S4U",
    [switch] $Unregister,
    [switch] $WhatIf
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$taskPath = "\EnterpriseRAG\"
$taskName = "RAG Backup"

function Test-Admin {
    ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

if ($Unregister) {
    if ($WhatIf) { "Would stop and unregister: $taskPath$taskName (backups in the backup folder are kept)"; exit 0 }
    if (-not (Test-Admin)) { throw "Run this from an elevated PowerShell (Run as administrator)." }
    if (Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskPath $taskPath -TaskName $taskName -Confirm:$false
        "Removed $taskPath$taskName (existing backups were not touched)"
    } else { "Not registered: $taskPath$taskName" }
    exit 0
}

# --- checks (reported in the dry run too) -----------------------------------------------------
$problems = @()
foreach ($file in @((Join-Path $root ".env"), (Join-Path $root ".venv\Scripts\python.exe"), (Join-Path $root "deploy\run_backup_task.ps1"))) {
    if (-not (Test-Path -LiteralPath $file)) { $problems += "missing: $file" }
}
if (-not (Test-Path -LiteralPath $PgPassFile)) { $problems += "PostgreSQL password file not found: $PgPassFile (see BACKUP.md)" }
if ($Keep -lt 1) { $problems += "-Keep must be at least 1" }
try { [void][datetime]::ParseExact($At, "HH:mm", $null) } catch { $problems += "-At must be HH:mm (24-hour)" }
$fullBackupDir = [IO.Path]::GetFullPath($BackupDir)
if ($fullBackupDir.StartsWith([IO.Path]::GetFullPath($root), [StringComparison]::OrdinalIgnoreCase)) {
    $problems += "the backup folder must be outside the project folder: $fullBackupDir"
}

$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$root\deploy\run_backup_task.ps1`" -BackupDir `"$fullBackupDir`" " +
             "-LogDir `"$LogDir`" -Keep $Keep -DbUser `"$DbUser`" -PgPassFile `"$PgPassFile`""

"Task to register: $taskPath$taskName (run as $UserId, logon type $LogonType, limited rights)"
"  schedule  : daily at $At; if missed (machine off), as soon as possible"
"  action    : powershell.exe $arguments"
"  retention : newest $Keep backups of this database in $fullBackupDir"
"  settings  : stop after 2 h, retry once after 30 min on failure, one instance, run on battery"
if ($problems) { "Problems:"; $problems | ForEach-Object { "  - $_" } } else { "Checks: OK" }
if ($WhatIf) { "Dry run: nothing registered."; exit 0 }
if ($problems) { throw "Fix the problems above first." }
if (-not (Test-Admin)) { throw "Run this from an elevated PowerShell (Run as administrator)." }

# --- register ------------------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $fullBackupDir | Out-Null
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
if ($LogonType -eq "S4U") {
    $principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType S4U -RunLevel Limited
    Register-ScheduledTask -TaskPath $taskPath -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Force | Out-Null
} else {
    $credential = Get-Credential -UserName $UserId -Message "Password for the Windows account that runs the backup task"
    Register-ScheduledTask -TaskPath $taskPath -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
        -User $UserId -Password $credential.GetNetworkCredential().Password -RunLevel Limited -Force | Out-Null
}
"Registered $taskPath$taskName. Test it now: Start-ScheduledTask -TaskPath '$taskPath' -TaskName '$taskName'"
