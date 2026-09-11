[CmdletBinding()]
param([switch]$Quiet)
$ErrorActionPreference = 'Continue'

$Tools = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Tools
$Tracker = Join-Path $Root 'START_JOB_TRACKER.bat'
$Vbs = Join-Path $Tools 'RUN_JOBSYNC.vbs'
$Icon = Join-Path $Tools 'JobSync.ico'
$TaskName = 'Job Tracker - Auto Start'
$StartupVbs = Join-Path $Tools 'START_JOB_TRACKER_STARTUP.vbs'
$Log = Join-Path $Root 'data\integrations.log'

function Log([string]$m) {
    New-Item -ItemType Directory -Path (Split-Path $Log) -Force | Out-Null
    Add-Content -LiteralPath $Log -Value "[$(Get-Date -Format s)] $m"
    if(-not $Quiet){ Write-Host $m }
}

if(-not (Test-Path -LiteralPath $Tracker -PathType Leaf)) { Log "ERROR: Launcher not found: $Tracker"; exit 1 }
if(-not (Test-Path -LiteralPath $Vbs -PathType Leaf)) { Log "ERROR: Desktop launcher not found: $Vbs"; exit 1 }
if(-not (Test-Path -LiteralPath $StartupVbs -PathType Leaf)) { Log "ERROR: Startup launcher not found: $StartupVbs"; exit 1 }

# Desktop shortcut
$desktop = [Environment]::GetFolderPath('Desktop')
if([string]::IsNullOrWhiteSpace($desktop) -or -not (Test-Path -LiteralPath $desktop)) {
    $desktop = Join-Path $env:OneDrive 'Desktop'
}
if(Test-Path -LiteralPath $desktop) {
    $shortcutPath = Join-Path $desktop 'Job Tracker.lnk'
    $ws = New-Object -ComObject WScript.Shell
    $s = $ws.CreateShortcut($shortcutPath)
    $s.TargetPath = Join-Path $env:WINDIR 'System32\wscript.exe'
    $s.Arguments = '"' + $Vbs + '"'
    $s.WorkingDirectory = $Root
    if(Test-Path -LiteralPath $Icon){ $s.IconLocation = $Icon + ',0' }
    $s.Description = 'Open Job Tracker'
    $s.Save()
    Log "Desktop shortcut ensured: $shortcutPath"
} else {
    Log 'Desktop folder not found; skipped shortcut creation.'
}

# Per-user logon task. No admin rights are required.
# Use schtasks.exe because it is available on supported Windows editions and
# avoids failures caused by missing ScheduledTasks PowerShell cmdlets.
try {
    $wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
    $taskRun = '"{0}" "{1}"' -f $wscript, $StartupVbs
    & schtasks.exe /Delete /TN $TaskName /F 2>$null | Out-Null
    & schtasks.exe /Create /TN $TaskName /SC ONLOGON /TR $taskRun /F /RL LIMITED 2>&1 | ForEach-Object { Log "schtasks: $_" }
    if($LASTEXITCODE -eq 0) {
        Log "Automatic startup task ensured: $TaskName"
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
