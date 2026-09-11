@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
set "APP_DIR=%CD%\"
set "DESKTOP=%USERPROFILE%\Desktop"
if not exist "%DESKTOP%" set "DESKTOP=%OneDrive%\Desktop"
if not exist "%DESKTOP%" (
  echo ERROR: Windows Desktop folder could not be located.
  pause
  exit /b 1
)
set "SHORTCUT=%DESKTOP%\Job Tracker.lnk"
set "LAUNCHER=%APP_DIR%tools\RUN_JOBSYNC.vbs"
set "ICON=%APP_DIR%tools\JobSync.ico"

set "PSFILE=%TEMP%\JobTracker_CreateShortcut_%RANDOM%.ps1"
>"%PSFILE%" echo $ErrorActionPreference='Stop'
>>"%PSFILE%" echo $desktop=[Environment]::GetFolderPath('Desktop')
>>"%PSFILE%" echo if(-not(Test-Path -LiteralPath $desktop)){ $desktop=Join-Path $env:OneDrive 'Desktop' }
>>"%PSFILE%" echo $shortcutPath=Join-Path $desktop 'Job Tracker.lnk'
>>"%PSFILE%" echo $launcher='%LAUNCHER%'
>>"%PSFILE%" echo $icon='%ICON%'
>>"%PSFILE%" echo $ws=New-Object -ComObject WScript.Shell
>>"%PSFILE%" echo $s=$ws.CreateShortcut($shortcutPath)
>>"%PSFILE%" echo $s.TargetPath=Join-Path $env:WINDIR 'System32\wscript.exe'
>>"%PSFILE%" echo $s.Arguments='"'+$launcher+'"'
>>"%PSFILE%" echo $s.WorkingDirectory='%APP_DIR%'
>>"%PSFILE%" echo if(Test-Path -LiteralPath $icon){$s.IconLocation=$icon+',0'}
>>"%PSFILE%" echo $s.Description='Open Job Tracker'
>>"%PSFILE%" echo $s.Save()

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%PSFILE%"
set "RC=%ERRORLEVEL%"
del /q "%PSFILE%" >nul 2>&1
if not "%RC%"=="0" (
  echo ERROR: Could not create the desktop shortcut.
  pause
  exit /b 1
)

echo Desktop shortcut created:
echo   %SHORTCUT%
pause
exit /b 0
