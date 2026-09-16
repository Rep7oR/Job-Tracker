# JobSync installer pre-install cleanup.
# This script is embedded in the NSIS installer and runs OUTSIDE the installation
# directory. It stops JobSync-owned processes and moves only persistent user data
# to a temporary backup. NSIS then removes the entire old Program Files\JobSync
# tree and installs a completely fresh application/runtime tree.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Root,
    [Parameter(Mandatory=$true)][string]$Backup
)

$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath($Root).TrimEnd('\')
$Backup = [IO.Path]::GetFullPath($Backup).TrimEnd('\')

New-Item -ItemType Directory -Path $Backup -Force | Out-Null
$log = Join-Path $Backup 'cleanup.log'
function Log([string]$m) {
    Add-Content -LiteralPath $log -Value ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m)
}

Log "JobSync v1.3.42 clean-up started. Root=$Root"

# Stop only processes that belong to JobSync, plus Ollama because JobSync stores
# its model cache under the installation and Ollama can keep model files locked.
$names = @('JobSync','streamlit','python','pythonw','ollama')
for ($round = 1; $round -le 12; $round++) {
    $stopped = 0
    foreach ($p in Get-CimInstance Win32_Process -ErrorAction SilentlyContinue) {
        if ($names -notcontains [IO.Path]::GetFileNameWithoutExtension([string]$p.Name)) { continue }
        $cmd = [string]$p.CommandLine
        $path = [string]$p.ExecutablePath
        $owned = $false
        if ($path -and $path.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)) { $owned = $true }
        if ($cmd -and $cmd.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0) { $owned = $true }
        if ($p.Name -ieq 'JobSync.exe') { $owned = $true }
        if ($p.Name -ieq 'streamlit.exe' -and $cmd -and $cmd.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0) { $owned = $true }
        if ($p.Name -ieq 'ollama.exe') {
            # JobSync launches Ollama for its local models. Stop it during a clean
            # replacement so model files and DLLs are not left locked.
            $owned = $true
        }
        if ($owned) {
            try {
                Log ("Stopping PID {0}: {1}" -f $p.ProcessId, $p.Name)
                Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
                $stopped++
            } catch {}
        }
    }
    Start-Sleep -Milliseconds 750
    $remaining = @()
    foreach ($p in Get-CimInstance Win32_Process -ErrorAction SilentlyContinue) {
        if ($names -notcontains [IO.Path]::GetFileNameWithoutExtension([string]$p.Name)) { continue }
        $cmd = [string]$p.CommandLine
        $path = [string]$p.ExecutablePath
        if (($path -and $path.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)) -or ($cmd -and $cmd.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0) -or $p.Name -ieq 'JobSync.exe' -or $p.Name -ieq 'ollama.exe') {
            $remaining += $p
        }
    }
    if ($remaining.Count -eq 0) { break }
    Log ("Cleanup wait round {0}: {1} process(es) still present." -f $round, $remaining.Count)
}

# Move persistent trees out of Program Files before the complete application tree
# is removed. Rename is intentionally used on the same volume for speed and to
# avoid copying large CV/PDF/model files.
$persistent = @(
    'data',
    'uploads',
    'output',
    'config',
    'user_blueprints',
    'ai'
)
foreach ($rel in $persistent) {
    $src = Join-Path $Root $rel
    $dst = Join-Path $Backup $rel
    if (Test-Path -LiteralPath $src) {
        Log "Preserving $rel"
            if (Test-Path -LiteralPath $dst) {
            # A previous interrupted installer may already have a backup. Merge
            # the live tree into that backup instead of deleting either copy.
            Log "Existing backup found for $rel; merging live data into backup."
            & robocopy.exe $src $dst /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /XJ /NFL /NDL /NJH /NJS | Out-Null
            $rc = $LASTEXITCODE
            if ($rc -ge 8) { throw "Could not merge persistent data for $rel (robocopy exit code $rc)." }
            Remove-Item -LiteralPath $src -Recurse -Force
        } else {
            Move-Item -LiteralPath $src -Destination $dst -Force
        }
    }
}

# Preserve root-level environment configuration if an older install has one.
$envSrc = Join-Path $Root '.env'
$envDst = Join-Path $Backup '.env'
if (Test-Path -LiteralPath $envSrc) {
    Log 'Preserving root .env configuration.'
    Move-Item -LiteralPath $envSrc -Destination $envDst -Force
}

Log 'Persistent data moved successfully; NSIS may now remove the complete old installation tree.'
exit 0
