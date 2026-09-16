@echo off
setlocal
cd /d "%~dp0"
set "TOOLS_DIR=%~dp0tools"
set "VBS=%TOOLS_DIR%\RUN_JOBSYNC.vbs"
if not exist "%VBS%" exit /b 1
wscript.exe "%VBS%"
exit /b 0
