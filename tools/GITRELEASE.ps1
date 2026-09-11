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
    $Value = if ($null -eq $Value) { "" } else { [string]$Value.Trim() }
    $Value = $Value -replace '^[vV]\.?', ''
    $m = [regex]::Match($Value, '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$')
    if (-not $m.Success) {
        throw "Version must be exactly MAJOR.MINOR.PATCH, for example 1.3.1."
    }
    return "$($m.Groups[1].Value).$($m.Groups[2].Value).$($m.Groups[3].Value)"
}

if (-not (Test-Path -LiteralPath $VersionFile -PathType Leaf)) {
    throw "VERSION.txt was not found."
}

if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = (Get-Content -LiteralPath $VersionFile -Raw).Trim()
    $Version = Read-Host "Release version [$Version]"
    if ([string]::IsNullOrWhiteSpace($Version)) {
        $Version = (Get-Content -LiteralPath $VersionFile -Raw).Trim()
    }
}

$Version = Normalize-Version $Version
$Tag = "v$Version"
$PackageName = "Job-Tracker-v$Version.zip"
$PackagePath = Join-Path $ReleaseDir $PackageName
$StageParent = Join-Path ([IO.Path]::GetTempPath()) ("JobTrackerRelease_{0}_{1}" -f $PID, (Get-Random))
$Stage = Join-Path $StageParent ("Job-Tracker-v$Version")

Write-Host "============================================================"
Write-Host "Job Tracker - Prepare GitHub Release"
Write-Host "============================================================"
Write-Host "Version : $Version"
Write-Host "Tag     : $Tag"
Write-Host "ZIP     : $PackagePath"
Write-Host ""

# ------------------------------------------------------------
# 1. Update the two version files. These are the only version
#    files the application/updater needs.
# ------------------------------------------------------------
Set-Content -LiteralPath $VersionFile -Value $Version -Encoding UTF8
@{ version = $Version } | ConvertTo-Json | Set-Content -LiteralPath $UpdateVersionFile -Encoding UTF8

# ------------------------------------------------------------
# 2. Keep updater repository information in sync with git remote.
# ------------------------------------------------------------
if (Get-Command git.exe -ErrorAction SilentlyContinue) {
    try {
        $remote = ([string](& git -C $Root remote get-url origin 2>$null)).Trim()
        if ($remote -match 'github\.com[:/](?<owner>[^/]+)/(?<repo>[^/]+?)(?:\.git)?$') {
            $cfg = if (Test-Path $ConfigFile) {
                Get-Content $ConfigFile -Raw | ConvertFrom-Json
            } else {
                [pscustomobject]@{}
            }
            $cfg | Add-Member -NotePropertyName github_owner -NotePropertyValue $Matches.owner -Force
            $cfg | Add-Member -NotePropertyName github_repo -NotePropertyValue $Matches.repo -Force
            if (-not $cfg.channel) { $cfg | Add-Member -NotePropertyName channel -NotePropertyValue "stable" -Force }
            if (-not $cfg.download_folder) { $cfg | Add-Member -NotePropertyName download_folder -NotePropertyValue "downloads" -Force }
            $cfg | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ConfigFile -Encoding UTF8
            Write-Host "Updater repository: $($Matches.owner)/$($Matches.repo)"
        }
    } catch {
        Write-Warning "Could not update github/update-config.json from origin: $($_.Exception.Message)"
    }
}

# ------------------------------------------------------------
# 3. Delete the development virtual environment as requested.
#    It is never included in a release ZIP.
# ------------------------------------------------------------
$Venv = Join-Path $Root ".venv"
if (Test-Path -LiteralPath $Venv) {
    Write-Host "Removing .venv ..."
    Remove-Item -LiteralPath $Venv -Recurse -Force
    if (Test-Path -LiteralPath $Venv) {
        throw "Could not remove .venv."
    }
} else {
    Write-Host ".venv not present."
}

# ------------------------------------------------------------
# 4. Build a clean release staging tree.
# ------------------------------------------------------------
New-Item -ItemType Directory -Path $Stage -Force | Out-Null
New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null

$excludeDirs = @(
    (Join-Path $Root ".git"),
    (Join-Path $Root ".venv"),
    (Join-Path $Root "data"),
    (Join-Path $Root "uploads"),
    (Join-Path $Root "output"),
    (Join-Path $Root "github\downloads")
)

# Robocopy is used only for fast recursive copying. Its normal
# success codes 0-7 are all acceptable.
$robocopyArgs = @(
    $Root, $Stage, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP"
)
foreach ($d in $excludeDirs) { $robocopyArgs += @("/XD", $d) }
$robocopyArgs += @(
    "/XF", ".env", "last-update-check.json"
)

& robocopy.exe @robocopyArgs | Out-Null
if ($LASTEXITCODE -gt 7) {
    throw "Could not build the clean release staging folder (robocopy exit $LASTEXITCODE)."
}

# Remove personal/runtime files that may live outside the excluded dirs.
$removeFiles = @(
    (Join-Path $Stage "config\google_oauth.json"),
    (Join-Path $Stage "github\last-update-check.json"),
    (Join-Path $Stage "github\downloads")
)
foreach ($f in $removeFiles) {
    if (Test-Path -LiteralPath $f) {
        Remove-Item -LiteralPath $f -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# User-created blueprints must not be distributed.
$userBlueprints = Join-Path $Stage "user_blueprints"
if (Test-Path -LiteralPath $userBlueprints) {
    Remove-Item -LiteralPath $userBlueprints -Recurse -Force
}
New-Item -ItemType Directory -Path $userBlueprints -Force | Out-Null

# Runtime directories are created fresh.
@(
    "data",
    "uploads\cv",
    "uploads\coverletters",
    "output\cv",
    "output\coverletters"
) | ForEach-Object {
    New-Item -ItemType Directory -Path (Join-Path $Stage $_) -Force | Out-Null
}

# Verify release-critical files.
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
foreach ($rel in $required) {
    $path = Join-Path $Stage $rel
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required release file is missing: $rel"
    }
}

$stageVersion = (Get-Content (Join-Path $Stage "VERSION.txt") -Raw).Trim()
if ($stageVersion -ne $Version) {
    throw "Release VERSION.txt is $stageVersion but expected $Version."
}

# ------------------------------------------------------------
# 5. Create the ZIP with a stable, predictable root folder.
# ------------------------------------------------------------
if (Test-Path -LiteralPath $PackagePath -PathType Leaf) {
    Remove-Item -LiteralPath $PackagePath -Force
}

Write-Host "Creating $PackageName ..."
Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $PackagePath -Force

if (-not (Test-Path -LiteralPath $PackagePath -PathType Leaf)) {
    throw "ZIP was not created."
}

$sizeMB = [Math]::Round((Get-Item $PackagePath).Length / 1MB, 2)
Write-Host "ZIP ready: $PackagePath ($sizeMB MB)"

# ------------------------------------------------------------
# 6. Commit the release state, then create and push the tag.
#    The tag MUST point to the same commit that contains the version
#    files and application changes used to build this release.
# ------------------------------------------------------------
if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
    Write-Warning "git.exe is not installed/in PATH. ZIP was created, but no Git tag was made."
} else {
    $isRepo = ([string](& git -C $Root rev-parse --is-inside-work-tree 2>$null)).Trim()
    if ($isRepo -ne "true") {
        Write-Warning "This folder is not a Git repository. ZIP was created, but no Git tag was made."
    } else {
        $status = (& git -C $Root status --porcelain 2>$null)
        if ($status) {
            Write-Host "Committing current release changes..."
            & git -C $Root add -A
            if ($LASTEXITCODE -ne 0) { throw "Could not stage release changes." }
            & git -C $Root commit -m "Release $Tag"
            if ($LASTEXITCODE -ne 0) { throw "Could not commit release $Tag." }
            Write-Host "Created release commit for $Tag."
        } else {
            Write-Host "Working tree is already clean."
        }

        $existingTag = ([string](& git -C $Root tag -l $Tag)).Trim()
        if ($existingTag) {
            $tagCommit = ([string](& git -C $Root rev-list -n 1 $Tag)).Trim()
            $headCommit = ([string](& git -C $Root rev-parse HEAD)).Trim()
            if ($tagCommit -ne $headCommit) {
                throw "Tag $Tag already exists and does not point to the current release commit. Use a new version instead of overwriting an existing tag."
            }
            Write-Host "Tag $Tag already exists and matches the release commit."
        } else {
            & git -C $Root tag -a $Tag -m "Job Tracker $Tag"
            if ($LASTEXITCODE -ne 0) { throw "Could not create Git tag $Tag." }
            Write-Host "Created Git tag $Tag."
        }

        $origin = ([string](& git -C $Root remote get-url origin 2>$null)).Trim()
        if ($origin) {
            & git -C $Root push origin $Tag
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "Tag was created locally, but could not be pushed to origin."
            } else {
                Write-Host "Pushed tag $Tag to origin."
            }
        } else {
            Write-Warning "No origin remote found. Tag was created locally only."
        }
    }
}

# ------------------------------------------------------------
# 7. If GitHub CLI is installed and authenticated, publish the
#    actual GitHub release and upload the ZIP automatically.
#    Otherwise the ZIP + tag are still ready for manual upload.
# ------------------------------------------------------------
$gh = Get-Command gh.exe -ErrorAction SilentlyContinue
if ($gh) {
    try {
        & gh auth status | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "GitHub CLI is not authenticated." }

        & gh release view $Tag | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "GitHub release $Tag already exists; replacing its ZIP asset..."
            & gh release upload $Tag $PackagePath --clobber
        } else {
            Write-Host "Creating GitHub release $Tag and uploading ZIP..."
            & gh release create $Tag $PackagePath --title "Job Tracker $Tag" --generate-notes
        }

        if ($LASTEXITCODE -ne 0) {
            throw "GitHub CLI could not publish release $Tag."
        }

        Write-Host "GitHub release published: $Tag"
    } catch {
        Write-Warning "GitHub release was not published automatically: $($_.Exception.Message)"
        Write-Host "Upload this file to the GitHub release tagged ${Tag}:"
        Write-Host "  $PackagePath"
    }
} else {
    Write-Host ""
    Write-Host "GitHub CLI (gh) is not installed."
    Write-Host "The ZIP and Git tag are ready. Create GitHub release $Tag and upload:"
    Write-Host "  $PackagePath"
}

Write-Host ""
Write-Host "============================================================"
Write-Host "RELEASE READY: Job Tracker $Tag"
Write-Host "============================================================"
Write-Host "Version files updated : VERSION.txt + UPDATE_VERSION.json"
Write-Host "Development .venv     : removed"
Write-Host "Release ZIP            : $PackagePath"
Write-Host "Git tag                : $Tag"
Write-Host ""
Write-Host "Important: commit VERSION.txt, UPDATE_VERSION.json, app.py and"
Write-Host "github/update-config.json before your next normal source push."
Write-Host ""

try {
    if (Test-Path -LiteralPath $StageParent) {
        Remove-Item -LiteralPath $StageParent -Recurse -Force
    }
} catch {}

exit 0
