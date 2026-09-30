# Register (or remove) the startup tasks for the API and Caddy. Run as Administrator.
#
#   Dry run (no changes, no elevation needed):
#     powershell -ExecutionPolicy Bypass -File deploy\register_startup_tasks.ps1 -WhatIf
#   Register:
#     powershell -ExecutionPolicy Bypass -File deploy\register_startup_tasks.ps1
#   Remove (rollback):
#     powershell -ExecutionPolicy Bypass -File deploy\register_startup_tasks.ps1 -Unregister
#
# Order at boot: PostgreSQL (Windows service, Automatic) -> "RAG API" -> "RAG Caddy".
# Each task starts at system startup (Caddy's later than the API's), and each wrapper
# waits for its dependency (start_api_task.ps1 waits for PostgreSQL, start_caddy_task.ps1
# for the API's health check), so the order holds even when startup is slow.
#
# The tasks run as -UserId (default: the current user), not as SYSTEM. Logon type S4U
# runs without a stored password and without access to network shares (not needed:
# everything is local); use -LogonType Password if S4U is not allowed on this machine.

param(
    [string] $CaddyExe = "C:\Program Files\Caddy\caddy.exe",
    [string] $CaddyEnvFile = "",
    [string] $LogDir = "C:\rag-logs",
    [string] $HfHome = (Join-Path $env:USERPROFILE ".cache\huggingface"),
    [string] $UserId = "$env:USERDOMAIN\$env:USERNAME",
    [ValidateSet("S4U", "Password")] [string] $LogonType = "S4U",
    [switch] $Unregister,
    [switch] $WhatIf
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $CaddyEnvFile) { $CaddyEnvFile = Join-Path $root "deploy\caddy.env" }
$taskPath = "\EnterpriseRAG\"
$apiTask, $caddyTask = "RAG API", "RAG Caddy"

function Test-Admin {
    ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

if ($Unregister) {
    if ($WhatIf) { "Would stop and unregister: $taskPath$caddyTask, $taskPath$apiTask"; exit 0 }
    if (-not (Test-Admin)) { throw "Run this from an elevated PowerShell (Run as administrator)." }
    foreach ($name in @($caddyTask, $apiTask)) {
        $task = Get-ScheduledTask -TaskPath $taskPath -TaskName $name -ErrorAction SilentlyContinue
        if ($task) {
            Stop-ScheduledTask -TaskPath $taskPath -TaskName $name -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskPath $taskPath -TaskName $name -Confirm:$false
            "Removed $taskPath$name"
        } else { "Not registered: $taskPath$name" }
    }
    "Processes started by the tasks may still be running; check ports 8000/80/443 (see PRODUCTION_RUNBOOK.md)."
    exit 0
}

# --- checks (reported in the dry run too) -----------------------------------------------------
$problems = @()
foreach ($file in @((Join-Path $root ".env"), (Join-Path $root "deploy\run_api.ps1"), (Join-Path $root ".venv\Scripts\python.exe"),
                    $CaddyExe, $CaddyEnvFile)) {
    if (-not (Test-Path -LiteralPath $file)) { $problems += "missing: $file" }
}
if (-not (Test-Path -LiteralPath (Join-Path $HfHome "hub"))) { $problems += "no Hugging Face cache at $HfHome (hub folder missing)" }
if (-not (Get-Service -Name "postgresql-x64-18" -ErrorAction SilentlyContinue)) { $problems += "service postgresql-x64-18 not found" }

$powershell = "powershell.exe"
$apiArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$root\deploy\start_api_task.ps1`" -LogDir `"$LogDir`" -HfHome `"$HfHome`""
$caddyArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$root\deploy\start_caddy_task.ps1`" -CaddyExe `"$CaddyExe`" " +
             "-EnvFile `"$CaddyEnvFile`" -LogDir `"$LogDir`""

"Tasks to register under $taskPath (run as $UserId, logon type $LogonType, limited rights):"
"  $apiTask   : at startup + 30 s -> $powershell $apiArgs"
"  $caddyTask : at startup + 60 s -> $powershell $caddyArgs"
"  settings  : no time limit, restart 3x every 1 min on failure, one instance, run on battery"
if ($problems) { "Problems:"; $problems | ForEach-Object { "  - $_" } } else { "Checks: OK" }
if ($WhatIf) { "Dry run: nothing registered."; exit 0 }
if ($problems) { throw "Fix the problems above first." }
if (-not (Test-Admin)) { throw "Run this from an elevated PowerShell (Run as administrator)." }

# --- register ------------------------------------------------------------------------------------
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$definitions = @(
    @{ Name = $apiTask;   Args = $apiArgs;   Delay = "PT30S" },
    @{ Name = $caddyTask; Args = $caddyArgs; Delay = "PT60S" }
)
$credential = $null
if ($LogonType -eq "Password") { $credential = Get-Credential -UserName $UserId -Message "Password for the account that runs the tasks" }
foreach ($definition in $definitions) {
    $action = New-ScheduledTaskAction -Execute $powershell -Argument $definition.Args -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = $definition.Delay
    if ($LogonType -eq "S4U") {
        $principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType S4U -RunLevel Limited
        Register-ScheduledTask -TaskPath $taskPath -TaskName $definition.Name -Action $action -Trigger $trigger `
            -Settings $settings -Principal $principal -Force | Out-Null
    } else {
        Register-ScheduledTask -TaskPath $taskPath -TaskName $definition.Name -Action $action -Trigger $trigger `
            -Settings $settings -User $UserId -Password $credential.GetNetworkCredential().Password -RunLevel Limited -Force | Out-Null
    }
    "Registered $taskPath$($definition.Name)"
}
"Start now (in order): Start-ScheduledTask -TaskPath '$taskPath' -TaskName '$apiTask'; then '$caddyTask' (it waits for the API)."
