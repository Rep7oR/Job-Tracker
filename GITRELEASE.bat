@echo off
setlocal

cd /d "%~dp0"

echo ============================================================
echo Job Tracker - GitHub Release
echo ============================================================
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\GITRELEASE.ps1"

if errorlevel 1 (
    echo.
    echo ============================================================
    echo RELEASE FAILED
    echo ============================================================
    pause
    exit /b 1
)

echo.
echo ============================================================
echo RELEASE COMPLETE
echo ============================================================
pause
endlocal
