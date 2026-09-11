[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)] [string]$InstallDir,
    [string]$ConfigPath = "",
    [switch]$Quiet
)
$ErrorActionPreference = "Stop"

function Say([string]$m) { if (-not $Quiet) { Write-Host $m } }
function Parse-Version([string]$v) {
    if([string]::IsNullOrWhiteSpace($v)){ throw "No version supplied." }
    $m=[regex]::Match($v,'(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?(?!\d)')
    if(-not $m.Success){ throw "No semantic version found in '$v'." }
    $patch=if($m.Groups[3].Success){[int]$m.Groups[3].Value}else{0}
    return [version]"$($m.Groups[1].Value).$($m.Groups[2].Value).$patch"
}
function Read-Version([string]$p) {
    if(-not(Test-Path -LiteralPath $p -PathType Leaf)){return $null}
    try{
        $raw=Get-Content -LiteralPath $p -Raw
        try{$j=$raw|ConvertFrom-Json; if($j.version){return [string]$j.version}}catch{}
        $m=[regex]::Match($raw,'(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?(?!\d)'); if($m.Success){return $m.Value}
    }catch{}
    return $null
}

$root=(Resolve-Path -LiteralPath $InstallDir).Path
if([string]::IsNullOrWhiteSpace($ConfigPath)){$ConfigPath=Join-Path $root 'github\update-config.json'}
if(-not(Test-Path -LiteralPath $ConfigPath -PathType Leaf)){throw "Updater config not found: $ConfigPath"}
$config=Get-Content -LiteralPath $ConfigPath -Raw|ConvertFrom-Json
$owner=[string]$config.github_owner
$repo=[string]$config.github_repo
$downloadFolder=[string]$config.download_folder
if([string]::IsNullOrWhiteSpace($downloadFolder)){$downloadFolder='downloads'}
$downloadDir=Join-Path (Split-Path -Parent $ConfigPath) $downloadFolder
New-Item -ItemType Directory -Path $downloadDir -Force|Out-Null

$versions=New-Object System.Collections.Generic.List[version]
foreach($p in @((Join-Path $root 'UPDATE_VERSION.json'),(Join-Path $root 'VERSION.txt'))){$v=Read-Version $p;if($v){try{$versions.Add((Parse-Version $v))}catch{}}}
$folder=(Split-Path -Leaf ($root.TrimEnd('\')))
try{$versions.Add((Parse-Version $folder))}catch{}
$current=if($versions.Count){$versions|Sort-Object -Descending|Select-Object -First 1}else{[version]'0.0.0'}

Say "Installed version: $current"
Say "Checking GitHub: $owner/$repo"
$headers=@{Accept='application/vnd.github+json';'X-GitHub-Api-Version'='2022-11-28';'User-Agent'='Job-Tracker-Universal-Updater'}
$api="https://api.github.com/repos/$owner/$repo/releases?per_page=30"
$releases=@(Invoke-RestMethod -Uri $api -Headers $headers -Method Get)|Where-Object{ -not $_.draft }
$candidates=@()
foreach($r in $releases){try{$v=Parse-Version ([string]$r.tag_name);$candidates += [pscustomobject]@{Release=$r;Version=$v}}catch{}}
if(-not $candidates){
  $resultPath=Join-Path (Split-Path -Parent $ConfigPath) 'last-update-check.json'
  [ordered]@{checked_at=(Get-Date).ToString('o');current_version=$current.ToString();latest_version=$null;release_tag=$null;downloaded=$false;download_path=$null;status='no_published_releases';repository="$owner/$repo"}|ConvertTo-Json -Depth 5|Set-Content -LiteralPath $resultPath -Encoding UTF8
  Say 'No published semantic-version GitHub releases were found.'
  Say 'Create a published release with a ZIP asset before using the updater.'
  exit 0
}
$best=$candidates|Sort-Object Version -Descending|Select-Object -First 1
$latest=$best.Version
$release=$best.Release
Say "Latest release: $latest ($([string]$release.tag_name))"

$result=[ordered]@{checked_at=(Get-Date).ToString('o');current_version=$current.ToString();latest_version=$latest.ToString();release_tag=[string]$release.tag_name;downloaded=$false;download_path=$null;status='up_to_date'}
if($latest -le $current){
  Say 'No update available.'
  $resultPath=Join-Path (Split-Path -Parent $ConfigPath) 'last-update-check.json'
  $result|ConvertTo-Json -Depth 5|Set-Content -LiteralPath $resultPath -Encoding UTF8
  exit 0
}

$assets=@($release.assets)
$asset=$assets|Where-Object{[string]$_.name -match '(?i)\.zip$' -and [string]$_.name -notmatch '(?i)source[ _-]?code'}|Sort-Object @{Expression={if(([string]$_.name)-match '(?i)(job|tracker|sync)'){0}else{1}}},name|Select-Object -First 1
if(-not $asset){throw "No downloadable ZIP asset found for release $latest."}
$dest=Join-Path $downloadDir ([string]$asset.name)
Say "New release found: $latest"
Say "Downloading: $([string]$asset.name)"
Invoke-WebRequest -Uri ([string]$asset.browser_download_url) -Headers $headers -OutFile $dest -UseBasicParsing
if(-not(Test-Path -LiteralPath $dest -PathType Leaf)){throw 'Download did not produce a file.'}
$result.downloaded=$true
$result.download_path=$dest
$result.status='update_downloaded'
$resultPath=Join-Path (Split-Path -Parent $ConfigPath) 'last-update-check.json'
$result|ConvertTo-Json -Depth 5|Set-Content -LiteralPath $resultPath -Encoding UTF8
Say "Downloaded to: $dest"
Say 'The running installation was not modified. Install the downloaded release when you are ready.'
