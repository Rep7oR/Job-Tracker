@echo off
setlocal
title Job Tracker - Create Shareable Package

cd /d "%~dp0"

echo ============================================================
echo Job Tracker - Create Shareable Package
echo ============================================================
echo.
echo Running CREATE_SHAREABLE.ps1...
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0CREATE_SHAREABLE.ps1"

if errorlevel 1 (
    echo.
    echo ERROR: Could not prepare the shareable package.
    echo.
    pause
    exit /b 1
)

echo.
echo Shareable package created successfully.
echo.
pause
endlocal
