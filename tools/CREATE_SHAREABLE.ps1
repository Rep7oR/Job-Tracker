$ErrorActionPreference = 'Stop'

Write-Host '============================================================'
Write-Host 'Job Tracker - Create Shareable Package'
Write-Host '============================================================'
Write-Host ''

$ScriptPath = $MyInvocation.MyCommand.Path

if ([string]::IsNullOrWhiteSpace($ScriptPath)) {
    throw 'Cannot determine the location of CREATE_SHAREABLE.ps1.'
}

$Source = [System.IO.Path]::GetDirectoryName(
    [System.IO.Path]::GetFullPath($ScriptPath)
)

if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
    throw "Source folder does not exist: $Source"
}

$Desktop = [Environment]::GetFolderPath('Desktop')
$Output = Join-Path $Desktop 'JobTracker-Shareable.zip'

Write-Host "Source : $Source"
Write-Host "Output : $Output"
Write-Host ''

$Temp = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ("JobTrackerShare_{0}_{1}" -f $PID, (Get-Random))

try {

    # ---------------------------------------------------------
    # Create temporary package
    # ---------------------------------------------------------

    New-Item -ItemType Directory -Path $Temp -Force | Out-Null

    Write-Host 'Copying application files...'

    Get-ChildItem -LiteralPath $Source -Force |
        Where-Object {
            $_.Name -notin @(
                'JobTracker-Shareable.zip'
            )
        } |
        Copy-Item `
            -Destination $Temp `
            -Recurse `
            -Force


    # ---------------------------------------------------------
    # Remove development/runtime content
    # ---------------------------------------------------------

    $removeDirectories = @(
        '.venv',
        '.git',
        '__pycache__',
        'data',
        'uploads',
        'output',
        'job_hunter_release'
    )

    foreach ($name in $removeDirectories) {

        $path = Join-Path $Temp $name

        if (Test-Path -LiteralPath $path) {

            Write-Host "Removing runtime folder: $name"

            Remove-Item `
                -LiteralPath $path `
                -Recurse `
                -Force `
                -ErrorAction SilentlyContinue
        }
    }


    # ---------------------------------------------------------
    # Remove development/personal files
    # ---------------------------------------------------------

    $removeFiles = @(
        '.env',
        'state.json',
        'account.json',
        'gmail_token.json',
        'google_oauth.json',
        'Job Hunter.exe'
    )

    foreach ($name in $removeFiles) {

        $path = Join-Path $Temp $name

        if (Test-Path -LiteralPath $path -PathType Leaf) {

            Write-Host "Removing personal file: $name"

            Remove-Item `
                -LiteralPath $path `
                -Force `
                -ErrorAction SilentlyContinue
        }
    }


    # ---------------------------------------------------------
    # Remove Python bytecode
    # ---------------------------------------------------------

    Get-ChildItem `
        -LiteralPath $Temp `
        -Recurse `
        -File `
        -Filter '*.pyc' `
        -ErrorAction SilentlyContinue |
        Remove-Item `
            -Force `
            -ErrorAction SilentlyContinue


    # ---------------------------------------------------------
    # IMPORTANT:
    # blueprint is APPLICATION CONTENT.
    #
    # DO NOT DELETE blueprint.
    # The base CV and cover-letter templates MUST be shipped
    # with every release.
    # ---------------------------------------------------------

    $BlueprintDir = Join-Path $Temp 'blueprint'

    if (-not (Test-Path -LiteralPath $BlueprintDir -PathType Container)) {
        throw "CRITICAL: blueprint folder is missing from release package."
    }

    $RequiredTemplates = @(
        'cv_base.tex',
        'cover_letter_base.tex'
    )

    foreach ($template in $RequiredTemplates) {

        $templatePath = Join-Path $BlueprintDir $template

        if (-not (Test-Path -LiteralPath $templatePath -PathType Leaf)) {

            throw "CRITICAL: Required template is missing: blueprint\$template"
        }

        $size = (Get-Item -LiteralPath $templatePath).Length

        if ($size -le 0) {

            throw "CRITICAL: Template is empty: blueprint\$template"
        }

        Write-Host "Verified template: blueprint\$template"
    }


    # ---------------------------------------------------------
    # USER BLUEPRINTS
    #
    # User-created templates are removed from share package,
    # but the directory itself is recreated.
    # ---------------------------------------------------------

    $UserBlueprintDir = Join-Path $Temp 'user_blueprints'

    if (Test-Path -LiteralPath $UserBlueprintDir -PathType Container) {

        Get-ChildItem `
            -LiteralPath $UserBlueprintDir `
            -Recurse `
            -File `
            -ErrorAction SilentlyContinue |
            Remove-Item `
                -Force `
                -ErrorAction SilentlyContinue
    }
    else {

        New-Item `
            -ItemType Directory `
            -Path $UserBlueprintDir `
            -Force |
            Out-Null
    }


    # ---------------------------------------------------------
    # Recreate runtime directories
    # ---------------------------------------------------------

    $runtimeDirectories = @(
        'data',
        'uploads',
        'uploads\cv',
        'uploads\coverletters',
        'output',
        'output\cv',
        'output\coverletters',
        'user_blueprints'
    )

    foreach ($relativePath in $runtimeDirectories) {

        $path = Join-Path $Temp $relativePath

        New-Item `
            -ItemType Directory `
            -Path $path `
            -Force |
            Out-Null
    }


    # ---------------------------------------------------------
    # Create ZIP
    # ---------------------------------------------------------

    if (Test-Path -LiteralPath $Output -PathType Leaf) {

        Remove-Item `
            -LiteralPath $Output `
            -Force
    }

    Write-Host ''
    Write-Host 'Creating ZIP package...'

    Compress-Archive `
        -Path (Join-Path $Temp '*') `
        -DestinationPath $Output `
        -Force


    # ---------------------------------------------------------
    # Verify ZIP exists
    # ---------------------------------------------------------

    if (-not (Test-Path -LiteralPath $Output -PathType Leaf)) {

        throw "ZIP file was not created: $Output"
    }

    $sizeMB = [Math]::Round(
        ((Get-Item -LiteralPath $Output).Length / 1MB),
        2
    )


    Write-Host ''
    Write-Host '============================================================'
    Write-Host 'SUCCESS'
    Write-Host '============================================================'
    Write-Host ''
    Write-Host "Package : $Output"
    Write-Host "Size    : $sizeMB MB"
    Write-Host ''
    Write-Host 'Bundled templates:'
    Write-Host '  [OK] blueprint\cv_base.tex'
    Write-Host '  [OK] blueprint\cover_letter_base.tex'
    Write-Host ''
    Write-Host 'The package is ready for GitHub release.'
    Write-Host ''
}
catch {

    Write-Host ''
    Write-Host '============================================================'
    Write-Host 'ERROR'
    Write-Host '============================================================'
    Write-Host ''
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ''

    exit 1
}
finally {

    if (Test-Path -LiteralPath $Temp) {

        Remove-Item `
            -LiteralPath $Temp `
            -Recurse `
            -Force `
            -ErrorAction SilentlyContinue
    }
}