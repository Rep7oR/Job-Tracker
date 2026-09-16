$ErrorActionPreference = "Stop"

$Tools = Split-Path -Parent $MyInvocation.MyCommand.Path
$Base = Split-Path -Parent $Tools
$VenvPy = Join-Path $Base 'runtime\.venv\Scripts\python.exe'
$DesktopLauncher = Join-Path $Tools 'RUN_JOBSYNC_DESKTOP.py'
$DesktopExe = Join-Path $Tools 'JobSync\JobSync.exe'

if (-not (Test-Path -LiteralPath $VenvPy -PathType Leaf)) {
    $setup = Join-Path $Tools 'SETUP_FIRST.bat'
    if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) { throw "SETUP_FIRST.bat not found: $setup" }
    $p = Start-Process -FilePath "$env:ComSpec" -ArgumentList @('/c', "`"$setup`"") -WorkingDirectory $Base -WindowStyle Hidden -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "JobSync setup failed with exit code $($p.ExitCode). See $Base\data\setup.log" }
}

if (-not (Test-Path -LiteralPath $VenvPy -PathType Leaf)) { throw "Python environment is not ready: $VenvPy" }

# Prefer the packaged JobSync.exe so Windows uses the JobSync logo in the
# taskbar and app switcher. Fall back to the Python launcher for development.
if (Test-Path -LiteralPath $DesktopExe -PathType Leaf) {
    Start-Process -FilePath $DesktopExe -WorkingDirectory $Base -WindowStyle Hidden | Out-Null
} elseif (Test-Path -LiteralPath $DesktopLauncher -PathType Leaf) {
    Start-Process -FilePath $VenvPy -ArgumentList @($DesktopLauncher) -WorkingDirectory $Base -WindowStyle Hidden | Out-Null
} else {
    throw "Desktop launcher not found: $DesktopExe / $DesktopLauncher"
}
