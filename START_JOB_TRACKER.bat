@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem ============================================================
rem Job Tracker - single entry-point launcher
rem IMPORTANT: every path is resolved relative to this BAT file.
rem ============================================================
cd /d "%~dp0"
set "ROOT=%~dp0"
set "TOOLS_DIR=%ROOT%tools"
set "PROGRAM_DIR=%ROOT%program"
set "VENV_DIR=%ROOT%.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "VENV_PYW=%VENV_DIR%\Scripts\pythonw.exe"
set "LOG_DIR=%ROOT%data"
set "LOG_FILE=%LOG_DIR%\jobsync_launcher.log"
set "PYTHON_EXE="

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1
>>"%LOG_FILE%" echo ============================================================
>>"%LOG_FILE%" echo [%date% %time%] Launcher started
>>"%LOG_FILE%" echo Root: %ROOT%

title Job Tracker - Launcher
cls
echo ============================================================
echo   Job Tracker - Start
echo ============================================================
echo.
echo Application root:
echo   %ROOT%
echo.

rem ------------------------------------------------------------
rem 0. Always use the tools folder from THIS launcher directory.
rem ------------------------------------------------------------
echo Checking bundled tools...
if not exist "%TOOLS_DIR%\INSTALL_INTEGRATIONS.ps1" (
    echo ERROR: Missing tools\INSTALL_INTEGRATIONS.ps1
    >>"%LOG_FILE%" echo ERROR: Missing %TOOLS_DIR%\INSTALL_INTEGRATIONS.ps1
    pause
    exit /b 1
)
if not exist "%TOOLS_DIR%\JobSync.ico" echo WARNING: tools\JobSync.ico is missing.
if not exist "%TOOLS_DIR%\START_JOB_MONITOR.bat" echo WARNING: tools\START_JOB_MONITOR.bat is missing.
if not exist "%TOOLS_DIR%\RUN_JOBSYNC.vbs" echo WARNING: tools\RUN_JOBSYNC.vbs is missing.

echo Installing/repairing desktop icon and automatic startup...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%TOOLS_DIR%\INSTALL_INTEGRATIONS.ps1" -Quiet >>"%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo WARNING: Windows integrations could not be fully installed.
    echo See data\integrations.log and data\jobsync_launcher.log
) else (
    echo Windows integrations ready.
)

rem ------------------------------------------------------------
rem 1. Find a REAL Python executable. Do not trust the Microsoft
rem    Store python.exe alias. Check py launcher, PATH, registry,
rem    and the standard python.org installation directories.
rem ------------------------------------------------------------
call :FindPython

if not defined PYTHON_EXE (
    echo Python 3 was not found.
    echo.
    echo Attempting automatic Python 3.13 installation with WinGet...
    >>"%LOG_FILE%" echo [%date% %time%] No usable Python found; invoking WinGet.

    where winget.exe >nul 2>&1
    if errorlevel 1 (
        echo ERROR: WinGet is unavailable.
        echo Please install Python 3 and run this launcher again.
        >>"%LOG_FILE%" echo ERROR: WinGet unavailable.
        pause
        exit /b 1
    )

    winget.exe install --id Python.Python.3.13 -e --source winget --accept-source-agreements --accept-package-agreements --disable-interactivity
    if errorlevel 1 (
        echo ERROR: WinGet could not install Python 3.13.
        echo See data\jobsync_launcher.log for details.
        >>"%LOG_FILE%" echo ERROR: WinGet Python installation failed.
        pause
        exit /b 1
    )

    echo.
    echo Python installation finished. Refreshing Python detection...
    call :FindPython

    if not defined PYTHON_EXE (
        echo ERROR: Python is installed, but this launcher could not locate python.exe.
        echo.
        echo The Python installation was detected by WinGet but its path was not
        echo registered in the current Windows environment yet.
        echo Please close this window and run START_JOB_TRACKER.bat again.
        >>"%LOG_FILE%" echo ERROR: Python installed by WinGet but executable not located.
        pause
        exit /b 1
    )
)

echo Using Python:
echo   %PYTHON_EXE%
echo Python version:
"%PYTHON_EXE%" --version
>>"%LOG_FILE%" echo [%date% %time%] Using Python: %PYTHON_EXE%

rem ------------------------------------------------------------
rem 2. Create the virtual environment.
rem ------------------------------------------------------------
if not exist "%VENV_PY%" (
    echo.
    echo Creating virtual environment...
    >>"%LOG_FILE%" echo [%date% %time%] Creating %VENV_DIR%
    "%PYTHON_EXE%" -m venv "%VENV_DIR%" >>"%LOG_FILE%" 2>&1
    if errorlevel 1 (
        echo ERROR: Could not create .venv.
        echo Check data\jobsync_launcher.log for details.
        pause
        exit /b 1
    )
    if not exist "%VENV_PY%" (
        echo ERROR: .venv was created but Scripts\python.exe is missing.
        >>"%LOG_FILE%" echo ERROR: .venv python.exe missing after venv creation.
        pause
        exit /b 1
    )
    echo Virtual environment created successfully.
) else (
    echo Existing virtual environment found.
)
set "PYTHON_EXE=%VENV_PY%"

rem ------------------------------------------------------------
rem 3. Validate project files before installing packages.
rem ------------------------------------------------------------
if not exist "%PROGRAM_DIR%\app.py" (
    echo ERROR: program\app.py was not found.
    >>"%LOG_FILE%" echo ERROR: %PROGRAM_DIR%\app.py missing.
    pause
    exit /b 1
)
if not exist "%PROGRAM_DIR%\requirements.txt" (
    echo ERROR: program\requirements.txt was not found.
    >>"%LOG_FILE%" echo ERROR: %PROGRAM_DIR%\requirements.txt missing.
    pause
    exit /b 1
)

rem ------------------------------------------------------------
rem 4. Install core dependencies.
rem ------------------------------------------------------------
"%PYTHON_EXE%" -c "import streamlit" >nul 2>&1
if errorlevel 1 (
    echo Installing Job Tracker dependencies...
    >>"%LOG_FILE%" echo [%date% %time%] Installing core requirements
    "%PYTHON_EXE%" -m pip install --upgrade pip >>"%LOG_FILE%" 2>&1
    if errorlevel 1 (
        echo ERROR: pip upgrade failed.
        pause
        exit /b 1
    )
    "%PYTHON_EXE%" -m pip install --prefer-binary -r "%PROGRAM_DIR%\requirements.txt" >>"%LOG_FILE%" 2>&1
    if errorlevel 1 (
        echo ERROR: Job Tracker dependencies could not be installed.
        echo Check data\jobsync_launcher.log for details.
        pause
        exit /b 1
    )
)

if exist "%PROGRAM_DIR%\requirements-browser.txt" (
    "%PYTHON_EXE%" -c "import playwright,greenlet" >nul 2>&1
    if errorlevel 1 (
        echo Installing optional browser dependencies...
        "%PYTHON_EXE%" -m pip install --prefer-binary --only-binary=:all: -r "%PROGRAM_DIR%\requirements-browser.txt" >>"%LOG_FILE%" 2>&1
    )
)

rem ------------------------------------------------------------
rem 5. Repair integrations again now that .venv exists, then start
rem    the hidden 24-hour monitor.
rem ------------------------------------------------------------
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%TOOLS_DIR%\INSTALL_INTEGRATIONS.ps1" -Quiet >>"%LOG_FILE%" 2>&1
if exist "%TOOLS_DIR%\START_JOB_MONITOR.bat" (
    call "%TOOLS_DIR%\START_JOB_MONITOR.bat" >>"%LOG_FILE%" 2>&1
    if errorlevel 1 echo WARNING: Background monitor could not be started. See data\monitor_launcher.log
)

set "JOBSYNC_ROOT=%ROOT%"
set "STREAMLIT_CONFIG_DIR=%PROGRAM_DIR%\.streamlit"
cd /d "%PROGRAM_DIR%"
set "APP_VERSION=0.0.0"
if exist "%ROOT%VERSION.txt" for /f "usebackq delims=" %%V in ("%ROOT%VERSION.txt") do set "APP_VERSION=%%V"

echo.
echo ============================================================
echo Starting Job Tracker v%APP_VERSION%
echo Update checks are manual from Settings.
echo ============================================================
echo.
rem ------------------------------------------------------------
rem 6. Select a usable Streamlit port. If Job Tracker is already
rem    running on 8501, open that instance instead of starting a
rem    duplicate. If another program owns 8501, use the next free
rem    port automatically.
rem ------------------------------------------------------------
set "STREAMLIT_PORT="
set "EXISTING_JOB_TRACKER="

for /f "delims=" %%P in ('powershell.exe -NoLogo -NoProfile -Command "$p=Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'streamlit' -and $_.CommandLine -match 'app\.py' -and $_.CommandLine -match '--server\.port\s+8501' }; if($p){'YES'}" 2^>nul') do set "EXISTING_JOB_TRACKER=%%P"

if /i "%EXISTING_JOB_TRACKER%"=="YES" (
    set "STREAMLIT_PORT=8501"
    echo Job Tracker is already running on port 8501.
    echo Opening the existing Job Tracker window...
    start "" "http://127.0.0.1:8501"
    >>"%LOG_FILE%" echo [%date% %time%] Existing Job Tracker detected on port 8501; opened existing instance.
    exit /b 0
)

for %%P in (8501 8502 8503 8504 8505 8506 8507 8508 8509 8510) do (
    if not defined STREAMLIT_PORT (
        netstat -ano | findstr /R /C:":%%P .*LISTENING" >nul 2>&1
        if errorlevel 1 set "STREAMLIT_PORT=%%P"
    )
)

if not defined STREAMLIT_PORT (
    echo ERROR: No free local port was found in the range 8501-8510.
    >>"%LOG_FILE%" echo [%date% %time%] ERROR: No free Streamlit port in 8501-8510.
    pause
    exit /b 1
)

echo Using Streamlit port: %STREAMLIT_PORT%
>>"%LOG_FILE%" echo [%date% %time%] Starting Streamlit on port %STREAMLIT_PORT%

"%PYTHON_EXE%" -m streamlit run "%PROGRAM_DIR%\app.py" --server.headless false --server.address 127.0.0.1 --server.port %STREAMLIT_PORT% --browser.serverAddress 127.0.0.1 --browser.serverPort %STREAMLIT_PORT%
set "RC=%ERRORLEVEL%"
>>"%LOG_FILE%" echo [%date% %time%] Streamlit exited with code %RC%

echo.
echo Job Tracker stopped with code %RC%.
pause
exit /b %RC%

rem ============================================================
rem FindPython - returns an absolute path in PYTHON_EXE.
rem ============================================================
:FindPython
set "PYTHON_EXE="
set "_PY="

rem A real py launcher is preferred.
for /f "delims=" %%P in ('where.exe py.exe 2^>nul') do if not defined _PY set "_PY=%%P"
if defined _PY (
    "%_PY%" -3 --version >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%P in ('"%_PY%" -3 -c "import sys; print(sys.executable)" 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
    )
)

rem PATH python.exe, but reject the Microsoft Store alias.
if not defined PYTHON_EXE (
    for /f "delims=" %%P in ('where.exe python.exe 2^>nul') do if not defined _PATHPY set "_PATHPY=%%P"
    if defined _PATHPY (
        echo "%_PATHPY%" | findstr /i /c:"WindowsApps" >nul
        if errorlevel 1 (
            "%_PATHPY%" --version >nul 2>&1
            if not errorlevel 1 set "PYTHON_EXE=%_PATHPY%"
        )
    )
)

rem Standard per-user/system Python.org install paths.
if not defined PYTHON_EXE if exist "%LocalAppData%\Programs\Python\Python313\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python313\python.exe"
if not defined PYTHON_EXE if exist "%LocalAppData%\Programs\Python\Python314\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python314\python.exe"
if not defined PYTHON_EXE if exist "%ProgramFiles%\Python313\python.exe" set "PYTHON_EXE=%ProgramFiles%\Python313\python.exe"
if not defined PYTHON_EXE if exist "%ProgramFiles%\Python314\python.exe" set "PYTHON_EXE=%ProgramFiles%\Python314\python.exe"
if not defined PYTHON_EXE if exist "%ProgramFiles%\Python311\python.exe" set "PYTHON_EXE=%ProgramFiles%\Python311\python.exe"
if not defined PYTHON_EXE if exist "%ProgramFiles%\Python312\python.exe" set "PYTHON_EXE=%ProgramFiles%\Python312\python.exe"

rem Registry lookup handles custom Python.org install locations.
if not defined PYTHON_EXE (
    for /f "usebackq delims=" %%P in (`powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$roots=@('HKCU:\Software\Python\PythonCore','HKLM:\Software\Python\PythonCore','HKLM:\Software\WOW6432Node\Python\PythonCore'); foreach($r in $roots){if(Test-Path $r){Get-ChildItem $r -ErrorAction SilentlyContinue | Sort-Object PSChildName -Descending | ForEach-Object {$p=(Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).InstallPath; if($p){$e=Join-Path $p 'python.exe'; if(Test-Path $e){$e; break}}}}}" 2^>nul`) do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
)

rem Validate result and clear a bogus Store shim.
if defined PYTHON_EXE (
    "%PYTHON_EXE%" --version >nul 2>&1
    if errorlevel 1 set "PYTHON_EXE="
)
exit /b 0
