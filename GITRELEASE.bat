@echo off
setlocal EnableExtensions
title Job Tracker - GitHub Release

cd /d "%~dp0"
set "ROOT=%CD%"
set "RELEASE_SCRIPT=%ROOT%\tools\GITRELEASE.ps1"

if not exist "%RELEASE_SCRIPT%" (
    echo ERROR: tools\GITRELEASE.ps1 was not found.
    pause
    exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%RELEASE_SCRIPT%" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo Release preparation failed. See the message above.
    pause
    exit /b %RC%
)

echo.
echo Release process completed successfully.
pause
exit /b 0
