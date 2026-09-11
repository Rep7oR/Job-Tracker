@echo off
setlocal EnableExtensions
set "ROOT=%~dp0..\"
for %%I in ("%ROOT%") do set "ROOT=%%~fI\"
set "PROGRAM_DIR=%ROOT%program"
set "PY=%ROOT%.venv\Scripts\python.exe"
set "PYW=%ROOT%.venv\Scripts\pythonw.exe"
set "LOG=%ROOT%data\monitor_launcher.log"
if not exist "%ROOT%data" mkdir "%ROOT%data"

>>"%LOG%" echo ============================================================
>>"%LOG%" echo [%date% %time%] Monitor launcher requested

if not exist "%PY%" (
  >>"%LOG%" echo [%date% %time%] ERROR: Python environment not found: %PY%
  exit /b 1
)

if not exist "%PROGRAM_DIR%\services\job_monitor.py" (
  >>"%LOG%" echo [%date% %time%] ERROR: Monitor module not found: %PROGRAM_DIR%\services\job_monitor.py
  exit /b 1
)

rem Do not create duplicate monitor processes.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$p=Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'services\.job_monitor' -and $_.CommandLine -match 'python' }; if($p){exit 0}else{exit 1}" >nul 2>&1
if not errorlevel 1 (
  >>"%LOG%" echo [%date% %time%] Monitor already running.
  exit /b 0
)

pushd "%PROGRAM_DIR%"
if exist "%PYW%" (
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$p=Start-Process -FilePath '%PYW%' -ArgumentList '-m','services.job_monitor' -WorkingDirectory '%PROGRAM_DIR%' -WindowStyle Hidden -PassThru; if($p){exit 0}else{exit 1}" >>"%LOG%" 2>&1
) else (
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$p=Start-Process -FilePath '%PY%' -ArgumentList '-m','services.job_monitor' -WorkingDirectory '%PROGRAM_DIR%' -WindowStyle Hidden -PassThru; if($p){exit 0}else{exit 1}" >>"%LOG%" 2>&1
)
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" (
  >>"%LOG%" echo [%date% %time%] ERROR: Monitor process could not be started.
  exit /b %RC%
)
>>"%LOG%" echo [%date% %time%] Monitor started hidden from %PROGRAM_DIR%.
exit /b 0
