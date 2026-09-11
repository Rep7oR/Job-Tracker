@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM Job Tracker - Install automatic Windows login startup
REM This creates a per-user Scheduled Task. It does not require
REM administrator rights and survives normal Windows restarts.
REM ============================================================

cd /d "%~dp0.."
set "APP_DIR=%CD%\"
set "TRACKER=%APP_DIR%START_JOB_TRACKER.bat"
set "TASK_NAME=Job Tracker - Auto Start"
set "LOG=%APP_DIR%data\startup_install.log"
if not exist "%APP_DIR%data" mkdir "%APP_DIR%data"

if not exist "%TRACKER%" (
  echo ERROR: %TRACKER% was not found.
  pause
  exit /b 1
)

>>"%LOG%" echo [%date% %time%] Installing startup task for %APP_DIR%

REM Launch through cmd.exe; the launcher itself starts the monitor.
set "TASK_CMD=cmd.exe /c ""%TRACKER%"""

schtasks.exe /Create /TN "%TASK_NAME%" /SC ONLOGON /TR "%TASK_CMD%" /F /RL LIMITED >nul 2>&1
if errorlevel 1 (
  echo ERROR: Could not create the Windows startup task.
  echo Task Scheduler rejected the request.
  >>"%LOG%" echo [%date% %time%] ERROR: schtasks creation failed.
  pause
  exit /b 1
)

>>"%LOG%" echo [%date% %time%] Startup task installed successfully.
echo.
echo ============================================================
echo Automatic startup installed.
echo ============================================================
echo Task: %TASK_NAME%
echo App : %APP_DIR%
echo.
echo Job Tracker will start automatically after your Windows user logs in.
echo The monitor is started by START_JOB_TRACKER.bat as well.
echo.
pause
exit /b 0
