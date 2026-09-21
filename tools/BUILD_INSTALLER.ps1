#Requires -Version 5.1
<#
.SYNOPSIS
    Build the JobSync Windows installer (.exe) using NSIS.

.DESCRIPTION
    1. Reads the version from VERSION.txt / UPDATE_VERSION.json
    2. Stages a clean copy of the app (no .venv, .env, data, output, keys)
    3. Patches the version into JobSync.nsi
    4. Compiles with makensis → dist\JobSync-Setup-vX.X.X.exe

.REQUIREMENTS
    - NSIS installed (https://nsis.sourceforge.io/Download)
      Default path: C:\Program Files (x86)\NSIS\makensis.exe
      Or add makensis to PATH.

.USAGE
    Right-click BUILD_INSTALLER.ps1 → Run with PowerShell
    — or —
    powershell -ExecutionPolicy Bypass -File tools\BUILD_INSTALLER.ps1
#>

$ErrorActionPreference = 'Stop'
$Root  = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Tools = Join-Path $Root 'tools'
$NsiFile = Join-Path $Tools 'installer\JobSync.nsi'
$Staging = Join-Path $Tools 'installer\staging'
$Dist    = Join-Path $Root 'dist'

function Say([string]$m, [string]$c = 'Cyan') { Write-Host $m -ForegroundColor $c }
function Die([string]$m) { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

# ── 1. Read version ────────────────────────────────────────────────────────────
function Get-AppVersion {
    foreach ($f in @('VERSION.txt','UPDATE_VERSION.json')) {
        $p = Join-Path $Root $f
        if (Test-Path -LiteralPath $p) {
            $raw = Get-Content -LiteralPath $p -Raw
            if ($f -match '\.json$') {
                $v = ($raw | ConvertFrom-Json).version
            } else {
                $v = $raw.Trim()
            }
            if ($v -match '(\d+\.\d+(?:\.\d+)?)') { return $Matches[1] }
        }
    }
    return '1.0.0'
}
$Version = Get-AppVersion
Say "Building JobSync v$Version installer..."

# ── 2. Find makensis ───────────────────────────────────────────────────────────
$makensis = $null
foreach ($candidate in @(
    'makensis',
    'C:\Program Files (x86)\NSIS\makensis.exe',
    'C:\Program Files\NSIS\makensis.exe'
)) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) {
        $makensis = $candidate; break
    }
    if ((Test-Path -LiteralPath $candidate -PathType Leaf)) {
        $makensis = $candidate; break
    }
}
if (-not $makensis) {
    Die @"
NSIS (makensis.exe) not found.
Download and install NSIS from: https://nsis.sourceforge.io/Download
Then re-run this script.
"@
}
Say "makensis: $makensis"

# ── 3. Stage ONLY runtime application files ──────────────────────────────────
Say "Staging only required application files..."
if (Test-Path -LiteralPath $Staging) { Remove-Item -LiteralPath $Staging -Recurse -Force }
New-Item -ItemType Directory -Path $Staging -Force | Out-Null

function Copy-Required([string]$RelativePath) {
    $source = Join-Path $Root $RelativePath
    $target = Join-Path $Staging $RelativePath
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { Die "Required file missing: $RelativePath" }
    New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $target -Force
}

foreach ($f in @('START_JOB_TRACKER.bat','VERSION.txt','UPDATE_VERSION.json')) { Copy-Required $f }
foreach ($f in @('cv_base.tex','cover_letter_base.tex','master_cv.tex')) { Copy-Required "blueprint\$f" }
foreach ($f in @('app.py','requirements.txt','requirements-browser.txt')) { Copy-Required "program\$f" }
Copy-Required 'program\.streamlit\config.toml'
foreach ($f in @('app_paths.py','application_status_agent.py','apply_session.py','company_watch.py','cv_engine.py','cv_prompt.py','excel_export.py','free_job_sources.py','gmail.py','jobs.py','job_monitor.py','linkedin_browser.py','messaging.py','notifications.py','pdf_compiler.py','presence.py','storage.py','ai_cv_generation_prompt.txt')) {
    Copy-Required "program\services\$f"
}
# GitHub updater is part of the installed application so existing users can
# check/download releases from Settings -> Software updates.
foreach ($f in @('README.txt','update-config.json','updater.ps1')) {
    Copy-Required "github\$f"
}
foreach ($f in @('INSTALL_INTEGRATIONS.ps1','JobSync.ico','RUN_JOBSYNC.ps1','RUN_JOBSYNC_DESKTOP.py','RUN_JOBSYNC_DESKTOP.spec','RUN_JOBSYNC.vbs','SETUP_FIRST.bat','INSTALLER_SETUP.bat','INSTALLER_CLEANUP.ps1','START_JOB_MONITOR.bat','START_JOB_TRACKER_STARTUP.vbs')) {
    Copy-Required "tools\$f"
}
# JobSync.exe is a stable bootstrap generated on the first install (or repair).
# It is intentionally not shipped in staging because upgrades preserve the existing
# native launcher instead of replacing its PyInstaller DLL tree.
foreach ($f in @('program\app.py','program\requirements.txt','program\services\presence.py','program\services\storage.py','github\updater.ps1','github\update-config.json','tools\SETUP_FIRST.bat','tools\INSTALLER_SETUP.bat','tools\INSTALLER_CLEANUP.ps1','tools\RUN_JOBSYNC_DESKTOP.py','tools\RUN_JOBSYNC_DESKTOP.spec','tools\RUN_JOBSYNC.vbs')) {
    if (-not (Test-Path -LiteralPath (Join-Path $Staging $f) -PathType Leaf)) { Die "Staging verification failed: $f" }
}
Say "  runtime allow-list: OK"
Say "  development files excluded: .venv, .git, config, data, output, uploads, packaging, releases, old ZIPs, backups"
Say "Staging complete."

# tectonic.exe is fetched at build time (not committed to git -- it's a ~25-40MB
# binary and every commit would bloat the repo forever). If the download fails
# (no network / GitHub unreachable) the build still succeeds; JobSync degrades
# gracefully at runtime by showing "Tectonic is not installed" instead of a PDF.
$tectonicScript = Join-Path $Tools 'DOWNLOAD_TECTONIC.ps1'
if (Test-Path -LiteralPath $tectonicScript) {
    Say "Fetching tectonic.exe for staging..."
    $tectonicTarget = Join-Path $Staging 'tools\tectonic.exe'
    & $tectonicScript -Destination $tectonicTarget
    if (Test-Path -LiteralPath $tectonicTarget) {
        Say "  tectonic.exe staged: OK"
    } else {
        Say "  tectonic.exe not staged (download unavailable) -- PDF compile will be disabled until installed manually" 'Yellow'
    }
}

# ── 4. Patch version into .nsi ─────────────────────────────────────────────────
Say "Patching version $Version into NSI script..."
$nsiContent = Get-Content -LiteralPath $NsiFile -Raw
$nsiContent = $nsiContent -replace '(!define APP_VERSION\s+")[^"]*(")', "`${1}$Version`${2}"
# Patch the output file name too
$OutName = "JobSync-Setup-$Version"
$nsiContent = $nsiContent -replace '(OutFile\s+"[^"]*\\)[^"\\]+(\.exe")', "`${1}$OutName`${2}"
$NsiPatched = Join-Path $Tools "installer\JobSync_patched.nsi"
if (Test-Path -LiteralPath $NsiPatched) { Remove-Item -LiteralPath $NsiPatched -Force -ErrorAction SilentlyContinue }
$nsiContent | Set-Content -LiteralPath $NsiPatched -Encoding UTF8
Say "  NSI output: dist\$OutName"

# ── 5. Create dist folder ─────────────────────────────────────────────────────
New-Item -ItemType Directory -Path $Dist -Force | Out-Null

# ── 6. Compile with NSIS ───────────────────────────────────────────────────────
Say "Compiling installer (this takes ~30 seconds)..."
$InstallerWorkDir = Join-Path $Tools 'installer'

# IMPORTANT: invoke makensis with PowerShell's call operator instead of
# Start-Process -ArgumentList.  The latter can re-tokenize paths on Windows
# and NSIS may receive a truncated script path (especially when the extracted
# project directory contains spaces or parentheses).
if (-not (Test-Path -LiteralPath $NsiPatched -PathType Leaf)) {
    Die "Patched NSI script was not created: $NsiPatched"
}
Say "  NSI script: $NsiPatched"
Say "  Working dir: $InstallerWorkDir"

Push-Location $InstallerWorkDir
try {
    & $makensis '/V2' $NsiPatched
    $NsisExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

if ($NsisExitCode -ne 0) {
    Die "makensis failed with exit code $NsisExitCode. Check the NSIS output above."
}

# ── 7. Verify output ──────────────────────────────────────────────────────────
$OutExe = Join-Path -Path $Dist -ChildPath ($OutName + '.exe')
if (-not (Test-Path -LiteralPath $OutExe -PathType Leaf)) {
    Die "Expected installer not found: $OutExe"
}
$SizeMB = [Math]::Round((Get-Item -LiteralPath $OutExe).Length / 1MB, 1)

# Clean up patched nsi and staging
Remove-Item -LiteralPath $NsiPatched -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $Staging -Recurse -Force -ErrorAction SilentlyContinue

Say ""
Say "============================================================" 'Green'
Say "  SUCCESS" 'Green'
Say "============================================================" 'Green'
Say ""
Say "  Installer : $OutExe"
Say "  Size      : $SizeMB MB"
Say ""
Say "  Share dist\$OutName with your users."
Say "  They just double-click it - no console, no Python needed first."
Say ""
