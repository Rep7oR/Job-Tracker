@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================================
echo JobSync - First Time Setup
echo ============================================================
echo.

REM ------------------------------------------------------------
REM 1. Make sure Python 3.14 is available.
REM    If WinGet is available and Python is missing, install it.
REM ------------------------------------------------------------
set "PYTHON_EXE="

where py >nul 2>nul
if not errorlevel 1 (
    py -3.14 --version >nul 2>nul
    if not errorlevel 1 set "PYTHON_EXE=py -3.14"
)

if not defined PYTHON_EXE (
    echo Python 3.14 was not found.
    echo.

    where winget >nul 2>nul
    if errorlevel 1 (
        echo WinGet is not available on this Windows installation.
        echo Please install Python 3.14 manually, then run this setup again.
        echo.
        echo Download: https://www.python.org/downloads/
        echo.
        pause
        exit /b 1
    )

    echo Installing Python 3.14 with WinGet...
    echo A normal Windows installer window may appear.
    echo.
    winget install --id Python.Python.3.14 -e --source winget --accept-source-agreements --accept-package-agreements

    if errorlevel 1 (
        echo.
        echo Python installation did not complete successfully.
        echo Run SETUP_FIRST.bat again after checking the installer message.
        pause
        exit /b 1
    )

    echo.
    echo Python installation finished. Looking for python.exe...

    REM WinGet-installed python.org builds commonly use one of these locations.
    if exist "%LocalAppData%\Programs\Python\Python314\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python314\python.exe"
    if not defined PYTHON_EXE if exist "%ProgramFiles%\Python314\python.exe" set "PYTHON_EXE=%ProgramFiles%\Python314\python.exe"
    if not defined PYTHON_EXE if exist "%LocalAppData%\Programs\Python\Python313\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python313\python.exe"

    if not defined PYTHON_EXE (
        echo.
        echo Python was installed but this setup window cannot find it yet.
        echo Please close this window, open a new Command Prompt, and run
        echo SETUP_FIRST.bat again.
        echo.
        pause
        exit /b 1
    )
)

echo Using Python:
if "%PYTHON_EXE%"=="py -3.14" (
    py -3.14 --version
) else (
    "%PYTHON_EXE%" --version
)
echo.

REM ------------------------------------------------------------
REM 2. Create the project virtual environment.
REM ------------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo Creating JobSync virtual environment...
    if "%PYTHON_EXE%"=="py -3.14" (
        py -3.14 -m venv .venv
    ) else (
        "%PYTHON_EXE%" -m venv .venv
    )

    if errorlevel 1 (
        echo.
        echo Could not create the virtual environment.
        pause
        exit /b 1
    )
) else (
    echo Existing virtual environment found.
)

REM ------------------------------------------------------------
REM 3. Install/update dependencies.
REM ------------------------------------------------------------
echo.
echo Installing JobSync packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
    echo.
    echo Pip upgrade failed.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m pip install --prefer-binary -r requirements.txt
if errorlevel 1 (
    echo.
    echo Core package installation failed.
    pause
    exit /b 1
)

REM ------------------------------------------------------------
REM 3b. Install optional browser stack using binary wheels only.
REM     This avoids compiling greenlet with MSVC and never blocks
REM     the main application if a compatible wheel is unavailable.
REM ------------------------------------------------------------
echo.
echo Installing optional browser features...
".venv\Scripts\python.exe" -m pip install --prefer-binary --only-binary=:all: -r requirements-browser.txt
if errorlevel 1 (
    echo.
    echo Optional browser packages could not be installed.
    echo JobSync itself will still run; LinkedIn/browser features can be
    echo enabled later from a compatible Python environment.
) else (
    echo Browser Python packages installed.
    ".venv\Scripts\python.exe" -m playwright install chromium
    if errorlevel 1 echo Chromium install failed; browser features can be installed later.
)

REM ------------------------------------------------------------
REM 4. Create local folders/config.
REM ------------------------------------------------------------
if not exist .env copy .env.example .env >nul
if not exist uploads\cv mkdir uploads\cv
if not exist uploads\coverletters mkdir uploads\coverletters
if not exist output\cv mkdir output\cv
if not exist output\coverletters mkdir output\coverletters
if not exist data mkdir data

echo.
echo ============================================================
echo JobSync setup is complete.
echo ============================================================
echo.
echo Installing Windows desktop shortcut and automatic startup...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0INSTALL_INTEGRATIONS.ps1"
echo.
echo Next:
echo   1. Double-click START_JOB_TRACKER.bat to launch now.
echo   2. The dashboard and 24-hour monitor will start automatically at Windows sign-in.
echo   3. Configure your profile and job-search method in the dashboard.
echo.
pause
