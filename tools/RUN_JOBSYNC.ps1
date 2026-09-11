$ErrorActionPreference = "Stop"
$Tools = Split-Path -Parent $MyInvocation.MyCommand.Path
$Base = Split-Path -Parent $Tools
$Bat = Join-Path $Base 'START_JOB_TRACKER.bat'
if (Test-Path -LiteralPath $Bat) {
    Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c', ('"{0}"' -f $Bat)) -WorkingDirectory $Base -WindowStyle Hidden
    exit 0
}
throw "START_JOB_TRACKER.bat not found: $Bat"
