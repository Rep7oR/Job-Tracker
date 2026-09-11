@echo off
setlocal
set "TASK_NAME=Job Tracker - Auto Start"
schtasks.exe /Delete /TN "%TASK_NAME%" /F >nul 2>&1
echo Job Tracker automatic startup task removed.
pause
