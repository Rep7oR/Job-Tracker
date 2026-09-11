[CmdletBinding()]
param(
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"

$Tools = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Tools

$VersionFile       = Join-Path $Root "VERSION.txt"
$UpdateVersionFile = Join-Path $Root "UPDATE_VERSION.json"
$ConfigFile        = Join-Path $Root "github\update-config.json"
$ReleaseDir        = Join-Path $Root "releases"

function Fail([string]$Message) {
    throw $Message
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory)]
        [string]$FilePath,

        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    $errorFile = Join-Path `
        ([IO.Path]::GetTempPath()) `
        ("JobTrackerNativeError_{0}.txt" -f $PID)

    if (Test-Path -LiteralPath $errorFile) {
        Remove-Item -LiteralPath $errorFile -Force -ErrorAction SilentlyContinue
    }

    try {
        $output = & $FilePath @Arguments 2> $errorFile
        $exitCode = $LASTEXITCODE

        $stderr = ""
        if (Test-Path -LiteralPath $errorFile) {
            $stderr = [string]::Join(
                "`n",
                @(Get-Content -LiteralPath $errorFile -ErrorAction SilentlyContinue)
            )
        }

        return [pscustomobject]@{
            ExitCode = $exitCode
            StdOut   = [string]::Join("`n", @($output))
            StdErr   = $stderr
        }
    }
    finally {
        if (Test-Path -LiteralPath $errorFile) {
            Remove-Item -LiteralPath $errorFile -Force -ErrorAction SilentlyContinue
        }
    }
}

function Run-Git {
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    $result = Invoke-NativeCommand `
        -FilePath "git.exe" `
        -Arguments $Arguments

    if ($result.StdOut) {
        Write-Host $result.StdOut.TrimEnd()
    }

    if ($result.ExitCode -ne 0) {
        $details = $result.StdErr.Trim()

        if (-not $details) {
            $details = $result.StdOut.Trim()
        }

        throw "Git command failed:`n  git $($Arguments -join ' ')`n$details"
    }

    return $result.StdOut
}

function Run-Gh {
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    $result = Invoke-NativeCommand `
        -FilePath "gh.exe" `
        -Arguments $Arguments

    if ($result.StdOut) {
        Write-Host $result.StdOut.TrimEnd()
    }

    if ($result.ExitCode -ne 0) {
        $details = $result.StdErr.Trim()

        if (-not $details) {
            $details = $result.StdOut.Trim()
        }

        throw "GitHub CLI command failed:`n  gh $($Arguments -join ' ')`n$details"
    }

    return $result.StdOut
}
function Normalize-Version([string]$Value) {
    $Value = if ($null -eq $Value) { "" } else { ([string]$Value).Trim() }
    $Value = $Value -replace '^[vV]\.?', ''

    if ($Value -notmatch '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$') {
        Fail "Version must be exactly MAJOR.MINOR.PATCH, for example 1.3.1."
    }

    return $Value
}

# ------------------------------------------------------------
# Validate repository
# ------------------------------------------------------------

if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
    Fail "git.exe was not found in PATH."
}

if (-not (Test-Path -LiteralPath $VersionFile -PathType Leaf)) {
    Fail "VERSION.txt was not found."
}

$isRepo = Run-Git @("-C", $Root, "rev-parse", "--is-inside-work-tree")
$isRepo = [string]$isRepo.Trim()

if ($isRepo -ne "true") {
    Fail "$Root is not a Git repository."
}

$remote = Run-Git @("-C", $Root, "remote", "get-url", "origin")
$remote = [string]$remote.Trim()

if ($remote -notmatch 'github\.com[:/](?<owner>[^/]+)/(?<repo>[^/]+?)(?:\.git)?$') {
    Fail "origin is not a GitHub repository: $remote"
}

$owner = $Matches.owner
$repo  = $Matches.repo

# ------------------------------------------------------------
# Determine version
# ------------------------------------------------------------

$currentVersion = (Get-Content -LiteralPath $VersionFile -Raw)
$currentVersion = [string]$currentVersion.Trim()

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

$StageParent = Join-Path ([IO.Path]::GetTempPath()) "JobTrackerRelease_$PID"
$Stage = Join-Path $StageParent $PackageName.Replace(".zip","")

Write-Host "============================================================"
Write-Host "Job Tracker - GitHub Release"
Write-Host "============================================================"
Write-Host "Repository : $owner/$repo"
Write-Host "Version    : $Version"
Write-Host "Tag        : $Tag"
Write-Host "ZIP        : $PackageName"
Write-Host ""

# ------------------------------------------------------------
# Validate tag/release do not already exist
# ------------------------------------------------------------

$localTag = Run-Git @("-C", $Root, "tag", "-l", $Tag)
$localTag = [string]$localTag.Trim()

if ($localTag) {
    Fail "Git tag $Tag already exists locally. Choose a new version."
}

$remoteTag = & git -C $Root ls-remote --tags origin "refs/tags/$Tag" 2>$null
if ($LASTEXITCODE -eq 0 -and $remoteTag) {
    Fail "Git tag $Tag already exists on origin. Choose a new version."
}

if (-not (Get-Command gh.exe -ErrorAction SilentlyContinue)) {
    Fail "GitHub CLI (gh) is required to publish releases automatically."
}

& gh auth status | Out-Null
if ($LASTEXITCODE -ne 0) {
    Fail "GitHub CLI is not authenticated. Run: gh auth login"
}



# ------------------------------------------------------------
# Update version files
# ------------------------------------------------------------

Set-Content -LiteralPath $VersionFile -Value $Version -Encoding UTF8

@{
    version = $Version
} | ConvertTo-Json | Set-Content -LiteralPath $UpdateVersionFile -Encoding UTF8

# ------------------------------------------------------------
# Sync updater repository configuration
# ------------------------------------------------------------

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

$cfg | ConvertTo-Json -Depth 10 |
    Set-Content -LiteralPath $ConfigFile -Encoding UTF8

Write-Host "Updater repository: $owner/$repo"

# ------------------------------------------------------------
# Remove development virtual environment
# ------------------------------------------------------------

$Venv = Join-Path $Root ".venv"

if (Test-Path -LiteralPath $Venv) {
    Write-Host "Removing .venv ..."
    Remove-Item -LiteralPath $Venv -Recurse -Force
}

# ------------------------------------------------------------
# Create clean staging tree
# ------------------------------------------------------------

if (Test-Path -LiteralPath $StageParent) {
    Remove-Item -LiteralPath $StageParent -Recurse -Force
}

New-Item -ItemType Directory -Path $Stage -Force | Out-Null

$excludedDirectories = @(
    ".git",
    ".venv",
    "data",
    "uploads",
    "output",
    "releases",
    "__pycache__"
)

$excludedFiles = @(
    ".env",
    "last-update-check.json"
)

Write-Host "Preparing clean release tree..."

Get-ChildItem -LiteralPath $Root -Recurse -Force -File | ForEach-Object {

    $fullPath = $_.FullName
    $relative = $fullPath.Substring($Root.Length).TrimStart('\')

    $parts = $relative -split '[\\/]'

    $skipDirectory = $false

    foreach ($directory in $excludedDirectories) {
        if ($parts -contains $directory) {
            $skipDirectory = $true
            break
        }
    }

    if ($skipDirectory) {
        return
    }

    if ($excludedFiles -contains $_.Name) {
        return
    }

    $destination = Join-Path $Stage $relative
    $destinationDirectory = Split-Path -Parent $destination

    New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
    Copy-Item -LiteralPath $fullPath -Destination $destination -Force
}

# Empty runtime directories expected by the application
@(
    "data",
    "uploads",
    "output"
) | ForEach-Object {
    New-Item -ItemType Directory -Path (Join-Path $Stage $_) -Force | Out-Null
}

# ------------------------------------------------------------
# Create ZIP
# ------------------------------------------------------------

New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null

if (Test-Path -LiteralPath $PackagePath) {
    Remove-Item -LiteralPath $PackagePath -Force
}

Write-Host "Creating $PackageName ..."

Compress-Archive `
    -Path (Join-Path $Stage "*") `
    -DestinationPath $PackagePath `
    -CompressionLevel Optimal

if (-not (Test-Path -LiteralPath $PackagePath -PathType Leaf)) {
    Fail "ZIP creation failed."
}

$sizeMB = [Math]::Round(
    ((Get-Item -LiteralPath $PackagePath).Length / 1MB),
    2
)

Write-Host "ZIP ready: $PackagePath ($sizeMB MB)"

# ------------------------------------------------------------
# Commit release changes
# ------------------------------------------------------------

$status = Run-Git @("-C", $Root, "status", "--porcelain")
$status = [string]$status

if (-not [string]::IsNullOrWhiteSpace($status)) {
    Write-Host "Committing release changes..."

    Run-Git @("-C", $Root, "add", "-A") | Out-Null
    Run-Git @("-C", $Root, "commit", "-m", "Release $Tag") | Out-Null

    Write-Host "Release commit created."
}
else {
    Write-Host "Working tree already clean."
}

# ------------------------------------------------------------
# Push main
# ------------------------------------------------------------

Write-Host "Pushing main..."

Run-Git @("-C", $Root, "push", "origin", "main") | Out-Null

# ------------------------------------------------------------
# Create and push tag
# ------------------------------------------------------------

Write-Host "Creating Git tag $Tag..."

Run-Git @(
    "-C", $Root,
    "tag",
    "-a", $Tag,
    "-m", "Job Tracker $Tag"
) | Out-Null

Write-Host "Pushing tag $Tag..."

Run-Git @(
    "-C", $Root,
    "push",
    "origin",
    $Tag
) | Out-Null

# ------------------------------------------------------------
# Create GitHub Release + upload ZIP
# ------------------------------------------------------------

Write-Host "Creating GitHub Release $Tag..."

Run-Gh @(
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

# ------------------------------------------------------------
# Cleanup staging tree
# ------------------------------------------------------------

try {
    if (Test-Path -LiteralPath $StageParent) {
        Remove-Item -LiteralPath $StageParent -Recurse -Force
    }
}
catch {
    Write-Warning "Could not remove temporary staging directory: $StageParent"
}

exit 0
