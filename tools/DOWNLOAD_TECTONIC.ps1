# Downloads the latest Windows x86_64 Tectonic release binary from GitHub
# releases and places it at -Destination. Not run automatically outside
# BUILD_INSTALLER.ps1's staging step. Failure here is non-fatal to the build:
# it prints a warning and leaves -Destination absent, and JobSync's
# pdf_compiler.py already handles a missing tectonic.exe by disabling PDF
# preview/compile rather than crashing.
param(
    [Parameter(Mandatory = $true)][string]$Destination
)

$ErrorActionPreference = 'Stop'

try {
    $releaseApi = 'https://api.github.com/repos/tectonic-typesetting/tectonic/releases/latest'
    $headers = @{ 'User-Agent' = 'JobSync-Build' }
    $release = Invoke-RestMethod -Uri $releaseApi -Headers $headers -TimeoutSec 30

    $asset = $release.assets | Where-Object {
        $_.name -match 'x86_64-pc-windows-msvc' -and $_.name -match '\.zip$'
    } | Select-Object -First 1

    if (-not $asset) {
        Write-Host "WARNING: No Windows x86_64 Tectonic asset found in latest release; skipping." -ForegroundColor Yellow
        return
    }

    $tmpZip = Join-Path $env:TEMP "tectonic_download_$([guid]::NewGuid()).zip"
    $tmpDir = Join-Path $env:TEMP "tectonic_extract_$([guid]::NewGuid())"
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $tmpZip -Headers $headers -TimeoutSec 120

    New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null
    Expand-Archive -LiteralPath $tmpZip -DestinationPath $tmpDir -Force

    $exe = Get-ChildItem -Path $tmpDir -Filter 'tectonic.exe' -Recurse | Select-Object -First 1
    if (-not $exe) {
        Write-Host "WARNING: tectonic.exe not found inside downloaded archive; skipping." -ForegroundColor Yellow
        return
    }

    New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
    Copy-Item -LiteralPath $exe.FullName -Destination $Destination -Force

    Remove-Item -LiteralPath $tmpZip -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}
catch {
    Write-Host "WARNING: Could not download Tectonic ($($_.Exception.Message)); PDF compile will be disabled until installed manually." -ForegroundColor Yellow
}
