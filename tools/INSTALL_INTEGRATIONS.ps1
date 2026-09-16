[CmdletBinding()]
param([switch]$Quiet)
$ErrorActionPreference = 'Continue'

$Tools = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Tools
$Tracker = Join-Path $Root 'START_JOB_TRACKER.bat'
$Vbs = Join-Path $Tools 'RUN_JOBSYNC.vbs'
$TaskName = 'JobSync - Auto Start'
$StartupVbs = Join-Path $Tools 'START_JOB_TRACKER_STARTUP.vbs'
$Log = Join-Path $Root 'data\integrations.log'

function Log([string]$m) {
    New-Item -ItemType Directory -Path (Split-Path $Log) -Force | Out-Null
    Add-Content -LiteralPath $Log -Value "[$(Get-Date -Format s)] $m"
    if(-not $Quiet){ Write-Host $m }
}

if(-not (Test-Path -LiteralPath $Tracker -PathType Leaf)) { Log "ERROR: Launcher not found: $Tracker"; exit 1 }
if(-not (Test-Path -LiteralPath $StartupVbs -PathType Leaf)) { Log "ERROR: Startup launcher not found: $StartupVbs"; exit 1 }

# Desktop shortcut is created only by the NSIS installer.
# This script must never create a second desktop icon.

# Per-user logon task. No admin rights are required.
# Use schtasks.exe because it is available on supported Windows editions and
# avoids failures caused by missing ScheduledTasks PowerShell cmdlets.
try {
    $wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
    $taskRun = '"{0}" "{1}"' -f $wscript, $StartupVbs
    & schtasks.exe /Delete /TN $TaskName /F 2>$null | Out-Null
    & schtasks.exe /Create /TN $TaskName /SC ONLOGON /TR $taskRun /F /RL LIMITED 2>&1 | ForEach-Object { Log "schtasks: $_" }
    if($LASTEXITCODE -eq 0) {
        Log "Automatic startup task ensured: $TaskName (JobSync launches at Windows sign-in)"
    } else {
        Log "ERROR: Could not create automatic startup task (exit code $LASTEXITCODE)."
    }
} catch {
    Log "ERROR: Startup task setup failed: $($_.Exception.Message)"
}

if(-not $Quiet){
    Write-Host ''
    Write-Host 'Job Tracker desktop shortcut and automatic startup are ready.'
}
