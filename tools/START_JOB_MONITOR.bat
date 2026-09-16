@echo off
setlocal
set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI\"
set "PROGRAM_DIR=%ROOT%program"
set "DATA_ROOT=%ROOT%data"
set "PY=%ROOT%runtime\.venv\Scripts\python.exe"
set "PYW=%ROOT%runtime\.venv\Scripts\pythonw.exe"
if not exist "%PY%" exit /b 1
if not exist "%PROGRAM_DIR%\services\job_monitor.py" exit /b 1
if not exist "%DATA_ROOT%" mkdir "%DATA_ROOT%" >nul 2>&1
pushd "%PROGRAM_DIR%"
set "JOBSYNC_ROOT=%ROOT%"
set "JOBSYNC_DATA_DIR=%ROOT%"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$p=Get-CimInstance Win32_Process -ErrorAction SilentlyContinue|Where-Object{$_.CommandLine -match 'services\.job_monitor' -and $_.CommandLine -match 'python'};if($p){exit 0}else{exit 1}" >nul 2>&1
if not errorlevel 1 (popd & exit /b 0)
start "" /b "%PYW%" -m services.job_monitor
popd
exit /b 0
