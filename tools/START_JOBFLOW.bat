@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
call "%CD%\START_JOB_TRACKER.bat"
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
