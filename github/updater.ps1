#Requires -Version 5.1
param(
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [Parameter(Mandatory=$true)][string]$ConfigPath
)
$ErrorActionPreference = 'Stop'
$updaterDir = Split-Path -Parent $MyInvocation.MyCommand.Path
# The application is installed under Program Files, which is intentionally not
# writable by normal users. Keep updater state and downloaded installers in the
# per-user LocalAppData directory so update checks never require write access to
# the installation directory.
$userUpdateRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'JobSync'
$resultDir = Join-Path $userUpdateRoot 'update-state'
$downloadRoot = Join-Path $userUpdateRoot 'updates'
New-Item -ItemType Directory -Force -Path $resultDir,$downloadRoot | Out-Null
$resultPath = Join-Path $resultDir 'last-update-check.json'
$currentVersion = '0.0.0'; $latestVersion = ''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
function Write-Result($data) { $data | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $resultPath -Encoding UTF8 }
function Parse-Version([string]$text) {
    $m=[regex]::Match([string]$text,'(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?(?!\d)')
    if(-not $m.Success){return $null}
    return [version]::Parse("$([int]$m.Groups[1].Value).$([int]$m.Groups[2].Value).$([int]$(if($m.Groups[3].Success){$m.Groups[3].Value}else{'0'}))")
}
$form=New-Object System.Windows.Forms.Form; $form.Text='JobSync Updater'; $form.StartPosition='CenterScreen'; $form.Size=New-Object System.Drawing.Size(520,190); $form.FormBorderStyle='FixedDialog'; $form.MaximizeBox=$false; $form.MinimizeBox=$false; $form.TopMost=$true
$title=New-Object System.Windows.Forms.Label; $title.Text='JobSync Update'; $title.Font=New-Object System.Drawing.Font('Segoe UI',14,[System.Drawing.FontStyle]::Bold); $title.AutoSize=$true; $title.Location=New-Object System.Drawing.Point(24,18); $form.Controls.Add($title)
$status=New-Object System.Windows.Forms.Label; $status.Text='Checking GitHub for updates...'; $status.AutoSize=$true; $status.Location=New-Object System.Drawing.Point(24,58); $form.Controls.Add($status)
$progress=New-Object System.Windows.Forms.ProgressBar; $progress.Location=New-Object System.Drawing.Point(24,88); $progress.Size=New-Object System.Drawing.Size(455,24); $progress.Style='Marquee'; $progress.MarqueeAnimationSpeed=25; $form.Controls.Add($progress)
$detail=New-Object System.Windows.Forms.Label; $detail.Text=''; $detail.AutoSize=$false; $detail.Size=New-Object System.Drawing.Size(455,35); $detail.Location=New-Object System.Drawing.Point(24,122); $form.Controls.Add($detail)
$form.Show(); [System.Windows.Forms.Application]::DoEvents()
function Show-Status([string]$message,[string]$detailText=''){ $status.Text=$message; $detail.Text=$detailText; [System.Windows.Forms.Application]::DoEvents() }
try {
    if(-not(Test-Path -LiteralPath $InstallDir -PathType Container)){throw "JobSync installation directory was not found: $InstallDir"}
    $cfg=Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json; $owner=[string]$cfg.github_owner; $repo=[string]$cfg.github_repo
    if([string]::IsNullOrWhiteSpace($owner)-or[string]::IsNullOrWhiteSpace($repo)){throw 'GitHub updater configuration is missing github_owner or github_repo.'}
    foreach($versionFile in @('VERSION.txt','UPDATE_VERSION.json')){
        if($currentVersion -ne '0.0.0'){break}; $path=Join-Path $InstallDir $versionFile
        if(Test-Path -LiteralPath $path -PathType Leaf){try{if($versionFile -eq 'VERSION.txt'){$candidate=(Get-Content -LiteralPath $path -Raw).Trim()}else{$candidate=[string]((Get-Content -LiteralPath $path -Raw|ConvertFrom-Json).version)};if($candidate){$currentVersion=$candidate}}catch{}}
    }
    [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
    $headers=@{'Accept'='application/vnd.github+json';'User-Agent'='JobSync-Updater';'X-GitHub-Api-Version'='2022-11-28'}
    Show-Status 'Checking GitHub for updates...' "Installed version: v$currentVersion"
    $release=Invoke-RestMethod -Uri "https://api.github.com/repos/$owner/$repo/releases/latest" -Headers $headers -Method Get -UseBasicParsing -TimeoutSec 30
    $latestVersion=[string]$release.tag_name; if([string]::IsNullOrWhiteSpace($latestVersion)){throw 'GitHub returned a release without a tag name.'}
    $currentParsed=Parse-Version $currentVersion; $latestParsed=Parse-Version $latestVersion
    if($null -eq $latestParsed){throw "Could not parse GitHub release version '$latestVersion'."}
    if($null -ne $currentParsed -and $latestParsed -le $currentParsed){
        Show-Status 'JobSync is up to date.' "Installed: v$currentVersion · Latest: $latestVersion"
        Write-Result @{downloaded=$false;up_to_date=$true;installing=$false;current_version=$currentVersion;latest_version=$latestVersion;download_path='';installer_path='';checked_at=(Get-Date).ToUniversalTime().ToString('o');error=''}
        Start-Sleep -Milliseconds 900; exit 0
    }
    # Prefer an actual Windows installer asset. Do not depend on a fragile regex from JSON.
    $assets=@($release.assets)
    $asset=$assets|Where-Object{([IO.Path]::GetExtension([string]$_.name)).Equals('.exe',[StringComparison]::OrdinalIgnoreCase)}|Select-Object -First 1
    if($null -eq $asset){
        $available=($assets|ForEach-Object{[string]$_.name}|Where-Object{$_}) -join ', '
        if([string]::IsNullOrWhiteSpace($available)){$available='(no release assets were returned by GitHub)'}
        throw "No installer EXE release asset was found in release $latestVersion. Assets returned by GitHub: $available"
    }
    $downloadFolderName=[string]$cfg.download_folder
    if([string]::IsNullOrWhiteSpace($downloadFolderName)){$downloadFolderName='updates'}
    # Preserve the configured folder name while anchoring it under the user's
    # writable LocalAppData JobSync directory, never beside the installed updater.
    $downloadFolder=Join-Path $userUpdateRoot $downloadFolderName
    New-Item -ItemType Directory -Force -Path $downloadFolder|Out-Null
    $downloadPath=Join-Path $downloadFolder ([string]$asset.name);$partialPath="$downloadPath.part"
    if(Test-Path -LiteralPath $partialPath){Remove-Item -LiteralPath $partialPath -Force -ErrorAction SilentlyContinue};if(Test-Path -LiteralPath $downloadPath){Remove-Item -LiteralPath $downloadPath -Force -ErrorAction SilentlyContinue}
    $progress.Style='Continuous';$progress.Minimum=0;$progress.Maximum=100
    $client=New-Object System.Net.WebClient;foreach($k in $headers.Keys){$client.Headers[$k]=$headers[$k]}
    $client.add_DownloadProgressChanged({param($sender,$e);$progress.Value=[Math]::Min(100,[Math]::Max(0,$e.ProgressPercentage));$status.Text="Downloading JobSync $latestVersion... $($e.ProgressPercentage)%";$detail.Text="Downloaded $([Math]::Round($e.BytesReceived/1MB,1)) MB of $([Math]::Round($e.TotalBytesToReceive/1MB,1)) MB";[System.Windows.Forms.Application]::DoEvents()})
    Show-Status "Downloading JobSync $latestVersion..." "Installer: $([string]$asset.name)"; $client.DownloadFile(([string]$asset.browser_download_url),$partialPath);$client.Dispose()
    if(-not(Test-Path -LiteralPath $partialPath -PathType Leaf)){throw 'GitHub download completed without creating the installer.'};if((Get-Item -LiteralPath $partialPath).Length -le 0){throw 'GitHub returned an empty installer file.'}
    Move-Item -LiteralPath $partialPath -Destination $downloadPath -Force
    Write-Result @{downloaded=$true;up_to_date=$false;installing=$true;current_version=$currentVersion;latest_version=$latestVersion;download_path=$downloadPath;installer_path=$downloadPath;checked_at=(Get-Date).ToUniversalTime().ToString('o');error=''}
    Show-Status 'Download complete.' "Starting JobSync v$latestVersion installer...";Start-Sleep -Milliseconds 700
    Show-Status 'Closing JobSync...' 'The installer will open automatically.'
    try {
        # Stop the native launcher and JobSync-owned Python processes before launching the installer.
        # v1.3.42 preserves the native launcher across upgrades, but shutting it down
        # prevents it from holding application/runtime DLLs open during replacement.
        Get-Process -Name 'JobSync' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        $appPath = Join-Path $InstallDir 'program\app.py'
        $processes=Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.Name -match '^python(?:w)?\.exe$' -and $_.CommandLine -and
            $_.CommandLine -match 'streamlit' -and $_.CommandLine -match [regex]::Escape($appPath)
        }
        foreach($proc in $processes){ Stop-Process -Id ([int]$proc.ProcessId) -Force -ErrorAction SilentlyContinue }
        $deadline=(Get-Date).AddSeconds(15)
        do {
            $stillRunning = Get-Process -Name 'JobSync' -ErrorAction SilentlyContinue
            if($stillRunning){ Start-Sleep -Milliseconds 400 }
        } while($stillRunning -and (Get-Date) -lt $deadline)
    } catch {}
    Start-Sleep -Milliseconds 1000;Start-Process -FilePath $downloadPath -WorkingDirectory $downloadFolder|Out-Null;exit 0
}catch{
    $message=$_.Exception.Message;try{Write-Result @{downloaded=$false;up_to_date=$false;installing=$false;current_version=$currentVersion;latest_version=$latestVersion;download_path='';installer_path='';checked_at=(Get-Date).ToUniversalTime().ToString('o');error=$message}}catch{}
    Show-Status 'Update failed.' $message;$progress.Style='Continuous';$progress.Value=0;[System.Windows.Forms.MessageBox]::Show($form,$message,'JobSync Update','OK','Error')|Out-Null;exit 1
}finally{try{$form.Close()}catch{}}
