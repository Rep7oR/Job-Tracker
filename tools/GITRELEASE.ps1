[CmdletBinding()]
param(
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"

$Tools = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Tools
$VersionFile = Join-Path $Root "VERSION.txt"
$UpdateVersionFile = Join-Path $Root "UPDATE_VERSION.json"
$ConfigFile = Join-Path $Root "github\update-config.json"
$ReleaseDir = Join-Path $Root "releases"

function Normalize-Version([string]$Value) {
    if ($null -eq $Value) { $Value = "" }
    $Value = ([string]$Value).Trim()
    $Value = $Value -replace '^[vV]\.?', ''

    if ($Value -notmatch '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$') {
        throw "Version must be exactly MAJOR.MINOR.PATCH, for example 1.3.1."
    }

    return $Value
}

function Quote-ProcessArgument([string]$Argument) {
    if ($null -eq $Argument) { return '""' }
    if ($Argument -notmatch '[\s"]') { return $Argument }

    # All arguments used by this script contain simple paths/text.
    # Escaping embedded quotes keeps Start-Process compatible with Windows PowerShell 5.1.
    return '"' + $Argument.Replace('"', '\"') + '"'
}

function Invoke-NativeTool {
    param(
        [Parameter(Mandatory)]
        [string]$FilePath,

        [Parameter(Mandatory)]
        [string[]]$Arguments,

        [switch]$AllowNonZero
    )

    $tempRoot = Join-Path $env:TEMP ("JobTrackerReleaseCmd_{0}" -f [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null

    $stdoutFile = Join-Path $tempRoot "stdout.txt"
    $stderrFile = Join-Path $tempRoot "stderr.txt"

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = (($Arguments | ForEach-Object { Quote-ProcessArgument $_ }) -join " ")
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $psi

    try {
        [void]$process.Start()

        $stdout = $process.StandardOutput.ReadToEnd()
        $stderr = $process.StandardError.ReadToEnd()

        $process.WaitForExit()
        $exitCode = $process.ExitCode

        if ($stdout) {
            Write-Host $stdout.TrimEnd()
        }

        if ($exitCode -ne 0 -and -not $AllowNonZero) {
            $details = $stderr.Trim()
            if (-not $details) { $details = $stdout.Trim() }

            throw "$FilePath failed with exit code $exitCode.`n$details"
        }

        return [pscustomobject]@{
            ExitCode = $exitCode
            StdOut   = $stdout
            StdErr   = $stderr
        }
    }
    finally {
        $process.Dispose()
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-Git {
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments,

        [switch]$AllowNonZero
    )

    return Invoke-NativeTool -FilePath "git.exe" -Arguments $Arguments -AllowNonZero:$AllowNonZero
}

function Invoke-Gh {
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments,

        [switch]$AllowNonZero
    )

    return Invoke-NativeTool -FilePath "gh.exe" -Arguments $Arguments -AllowNonZero:$AllowNonZero
}

if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
    throw "git.exe was not found in PATH."
}

if (-not (Get-Command gh.exe -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI (gh.exe) was not found in PATH. Install GitHub CLI before creating a release."
}

if (-not (Test-Path -LiteralPath $VersionFile -PathType Leaf)) {
    throw "VERSION.txt was not found."
}

$repoCheck = Invoke-Git @("-C", $Root, "rev-parse", "--is-inside-work-tree")
if ($repoCheck.ExitCode -ne 0 -or $repoCheck.StdOut.Trim() -ne "true") {
    throw "$Root is not a Git repository."
}

$remoteResult = Invoke-Git @("-C", $Root, "remote", "get-url", "origin")
$remote = $remoteResult.StdOut.Trim()

if ($remote -notmatch 'github\.com[:/](?<owner>[^/]+)/(?<repo>[^/]+?)(?:\.git)?$') {
    throw "origin is not a GitHub repository: $remote"
}

$owner = $Matches.owner
$repo = $Matches.repo

$currentVersion = ([string](Get-Content -LiteralPath $VersionFile -Raw)).Trim()

if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = Read-Host "Release version [$currentVersion]"
    if ([string]::IsNullOrWhiteSpace($Version)) {
        $Version = $currentVersion
    }
}

$Version = Normalize-Version $Version
$Tag = "v$Version"
$PackageName = "Job-Tracker-v$Version.zip"
$PackagePath = Join-Path $ReleaseDir $PackageName

Write-Host "============================================================"
Write-Host "Job Tracker - GitHub Release"
Write-Host "============================================================"
Write-Host "Repository : $owner/$repo"
Write-Host "Version    : $Version"
Write-Host "Tag        : $Tag"
Write-Host "ZIP        : $PackageName"
Write-Host ""

# Require authentication before changing anything.
Invoke-Gh @("auth", "status") | Out-Null

# Refuse duplicate local/remote tags.
$localTag = (Invoke-Git @("-C", $Root, "tag", "-l", $Tag)).StdOut.Trim()
if ($localTag) {
    throw "Git tag $Tag already exists locally. Choose a new version."
}

$remoteTag = (Invoke-Git @("-C", $Root, "ls-remote", "--tags", "origin", "refs/tags/$Tag")).StdOut.Trim()
if ($remoteTag) {
    throw "Git tag $Tag already exists on origin. Choose a new version."
}

# Update canonical version metadata.
Set-Content -LiteralPath $VersionFile -Value $Version -Encoding UTF8
@{ version = $Version } | ConvertTo-Json | Set-Content -LiteralPath $UpdateVersionFile -Encoding UTF8

# Keep updater configuration synchronized with the repository remote.
if (Test-Path -LiteralPath $ConfigFile -PathType Leaf) {
    try {
        $cfg = Get-Content -LiteralPath $ConfigFile -Raw | ConvertFrom-Json
    }
    catch {
        $cfg = [pscustomobject]@{}
    }
}
else {
    $cfg = [pscustomobject]@{}
}

$cfg | Add-Member -NotePropertyName github_owner -NotePropertyValue $owner -Force
$cfg | Add-Member -NotePropertyName github_repo -NotePropertyValue $repo -Force
if (-not $cfg.channel) {
    $cfg | Add-Member -NotePropertyName channel -NotePropertyValue "stable" -Force
}
if (-not $cfg.download_folder) {
    $cfg | Add-Member -NotePropertyName download_folder -NotePropertyValue "downloads" -Force
}

$cfg | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $ConfigFile -Encoding UTF8
Write-Host "Updater repository: $owner/$repo"

# Remove development environment.
$Venv = Join-Path $Root ".venv"
if (Test-Path -LiteralPath $Venv) {
    Write-Host "Removing .venv ..."
    Remove-Item -LiteralPath $Venv -Recurse -Force
    if (Test-Path -LiteralPath $Venv) {
        throw "Could not remove .venv."
    }
}
else {
    Write-Host ".venv not present."
}

# Build clean staging tree.
$StageParent = Join-Path ([IO.Path]::GetTempPath()) ("JobTrackerRelease_{0}_{1}" -f $PID, [guid]::NewGuid().ToString("N"))
$Stage = Join-Path $StageParent ("Job-Tracker-v$Version")

if (Test-Path -LiteralPath $StageParent) {
    Remove-Item -LiteralPath $StageParent -Recurse -Force
}
New-Item -ItemType Directory -Path $Stage -Force | Out-Null
New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null

$excludeDirs = @(
    (Join-Path $Root ".git"),
    (Join-Path $Root ".venv"),
    (Join-Path $Root "data"),
    (Join-Path $Root "uploads"),
    (Join-Path $Root "output"),
    (Join-Path $Root "releases"),
    (Join-Path $Root "github\downloads")
)

$robocopyArgs = @(
    $Root,
    $Stage,
    "/E",
    "/NFL",
    "/NDL",
    "/NJH",
    "/NJS",
    "/NP"
)

foreach ($d in $excludeDirs) {
    $robocopyArgs += @("/XD", $d)
}

$robocopyArgs += @(
    "/XF", ".env", "last-update-check.json"
)

$copyResult = Invoke-NativeTool -FilePath "robocopy.exe" -Arguments $robocopyArgs -AllowNonZero

# Robocopy exit codes 0..7 are success/non-fatal.
if ($copyResult.ExitCode -gt 7) {
    throw "Could not build release staging tree. Robocopy exit code: $($copyResult.ExitCode)"
}

foreach ($runtimePath in @(
    "config\google_oauth.json",
    "github\last-update-check.json",
    "github\downloads"
)) {
    $p = Join-Path $Stage $runtimePath
    if (Test-Path -LiteralPath $p) {
        Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$userBlueprints = Join-Path $Stage "user_blueprints"
if (Test-Path -LiteralPath $userBlueprints) {
    Remove-Item -LiteralPath $userBlueprints -Recurse -Force
}
New-Item -ItemType Directory -Path $userBlueprints -Force | Out-Null

@(
    "data",
    "uploads\cv",
    "uploads\coverletters",
    "output\cv",
    "output\coverletters"
) | ForEach-Object {
    New-Item -ItemType Directory -Path (Join-Path $Stage $_) -Force | Out-Null
}

$required = @(
    "VERSION.txt",
    "UPDATE_VERSION.json",
    "START_JOB_TRACKER.bat",
    "program\app.py",
    "github\updater.ps1",
    "github\update-config.json",
    "blueprint\cv_base.tex",
    "blueprint\cover_letter_base.tex"
)

foreach ($relativePath in $required) {
    $requiredPath = Join-Path $Stage $relativePath
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required release file is missing: $relativePath"
    }
}

$stageVersion = ([string](Get-Content -LiteralPath (Join-Path $Stage "VERSION.txt") -Raw)).Trim()
if ($stageVersion -ne $Version) {
    throw "Staged VERSION.txt is '$stageVersion' but expected '$Version'."
}

# Create ZIP with Job-Tracker-vX.Y.Z as the top-level folder.
if (Test-Path -LiteralPath $PackagePath -PathType Leaf) {
    Remove-Item -LiteralPath $PackagePath -Force
}

Write-Host "Creating $PackageName ..."
Compress-Archive -Path $Stage -DestinationPath $PackagePath -CompressionLevel Optimal -Force

if (-not (Test-Path -LiteralPath $PackagePath -PathType Leaf)) {
    throw "ZIP was not created."
}

$sizeMB = [Math]::Round((Get-Item -LiteralPath $PackagePath).Length / 1MB, 2)
Write-Host "ZIP ready: $PackagePath ($sizeMB MB)"

# Commit version/config changes.
$statusResult = Invoke-Git @("-C", $Root, "status", "--porcelain")
if (-not [string]::IsNullOrWhiteSpace($statusResult.StdOut)) {
    Write-Host "Committing release changes..."
    Invoke-Git @("-C", $Root, "add", "-A") | Out-Null
    Invoke-Git @("-C", $Root, "commit", "-m", "Release $Tag") | Out-Null
    Write-Host "Release commit created."
}
else {
    Write-Host "Working tree already clean."
}

# Push main first so the commit containing the final version exists remotely.
Write-Host "Pushing main..."
Invoke-Git @("-C", $Root, "push", "origin", "main") | Out-Null

# Create and push the immutable release tag.
Write-Host "Creating Git tag $Tag..."
Invoke-Git @("-C", $Root, "tag", "-a", $Tag, "-m", "Job Tracker $Tag") | Out-Null

Write-Host "Pushing tag $Tag..."
Invoke-Git @("-C", $Root, "push", "origin", $Tag) | Out-Null

# Create the published GitHub Release and upload the exact ZIP.
Write-Host "Creating GitHub Release $Tag and uploading ZIP..."
Invoke-Gh @(
    "release",
    "create",
    $Tag,
    $PackagePath,
    "--repo", "$owner/$repo",
    "--title", "Job Tracker $Tag",
    "--generate-notes"
) | Out-Null

Write-Host ""
Write-Host "============================================================"
Write-Host "RELEASE SUCCESSFUL"
Write-Host "============================================================"
Write-Host "Version : $Version"
Write-Host "Tag     : $Tag"
Write-Host "ZIP     : $PackagePath"
Write-Host "GitHub  : https://github.com/$owner/$repo/releases/tag/$Tag"
Write-Host "============================================================"

try {
    if (Test-Path -LiteralPath $StageParent) {
        Remove-Item -LiteralPath $StageParent -Recurse -Force
    }
}
catch {
    Write-Warning "Could not remove temporary staging directory: $StageParent"
}

exit 0
