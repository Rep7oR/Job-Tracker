@echo off
setlocal EnableExtensions
title Job Tracker - GitHub Release

cd /d "%~dp0"

if not exist "%~dp0tools\GITRELEASE.ps1" (
    echo ERROR: tools\GITRELEASE.ps1 was not found.
    pause
    exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\GITRELEASE.ps1" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo ============================================================
    echo RELEASE FAILED
    echo ============================================================
    pause
    exit /b %RC%
)

echo.
echo ============================================================
echo RELEASE COMPLETE
echo ============================================================
pause
endlocal
