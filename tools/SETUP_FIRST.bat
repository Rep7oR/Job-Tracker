@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0.."
set "ROOT=%CD%"
set "PROGRAM_DIR=%ROOT%\program"
set "RUNTIME_ROOT=%ROOT%\runtime"
set "DATA_ROOT=%ROOT%\data"
set "VENV_DIR=%RUNTIME_ROOT%\.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "LOG_FILE=%DATA_ROOT%\setup.log"
set "AI_MODELS_ROOT=%ROOT%\ai\models"
set "PYTHON_EXE="
set "PYTHON_PATH="
set "INSTALLER_MODE=0"
if /I "%JOBSYNC_INSTALLER%"=="1" set "INSTALLER_MODE=1"

if not exist "%DATA_ROOT%" mkdir "%DATA_ROOT%" >nul 2>&1
if not exist "%AI_MODELS_ROOT%" mkdir "%AI_MODELS_ROOT%" >nul 2>&1
>>"%LOG_FILE%" echo.
>>"%LOG_FILE%" echo ============================================================
>>"%LOG_FILE%" echo JobSync setup started: %date% %time%
>>"%LOG_FILE%" echo Setup engine: v1.4.0
>>"%LOG_FILE%" echo Root: %ROOT%
>>"%LOG_FILE%" echo Installer mode: %INSTALLER_MODE%
>>"%LOG_FILE%" echo ============================================================

rem Installer upgrades must happen with every JobSync-owned Python process stopped.
rem The native launcher can spawn a separate venv\python.exe process, so killing
rem JobSync.exe alone is not sufficient.  We match only processes whose command
rem line belongs to this JobSync installation.
if "%INSTALLER_MODE%"=="1" (
    >>"%LOG_FILE%" echo [0/6] Stopping JobSync processes before upgrade...
    rem v1.3.38: do not embed PowerShell code containing $ variables in this BAT.
    rem Earlier installers could transform those characters and produce $$root/$$_.
    rem The updater already stops JobSync-owned Python. Here we only stop the named
    rem JobSync/Streamlit processes and then verify that the stable launcher can stay.
    for /L %%R in (1,1,8) do (
        taskkill /F /IM JobSync.exe /T >>"%LOG_FILE%" 2>&1
        taskkill /F /IM streamlit.exe /T >>"%LOG_FILE%" 2>&1
        timeout /t 1 /nobreak >nul
    )
)

rem Migrate legacy per-user data/runtime into the single JobSync folder.
if exist "%LOCALAPPDATA%\JobSyncData" if not exist "%DATA_ROOT%\.migration_done" (
    >>"%LOG_FILE%" echo Migrating legacy JobSyncData...
    robocopy "%LOCALAPPDATA%\JobSyncData" "%DATA_ROOT%" /E /COPY:DAT /R:1 /W:1 >>"%LOG_FILE%" 2>&1
    if not errorlevel 8 echo migrated>"%DATA_ROOT%\.migration_done"
    if exist "%DATA_ROOT%\.migration_done" rmdir /s /q "%LOCALAPPDATA%\JobSyncData" >nul 2>&1
)
if exist "%LOCALAPPDATA%\JobSyncRuntime" if not exist "%RUNTIME_ROOT%\.migration_done" (
    >>"%LOG_FILE%" echo Migrating legacy JobSyncRuntime...
    robocopy "%LOCALAPPDATA%\JobSyncRuntime" "%RUNTIME_ROOT%" /E /COPY:DAT /R:1 /W:1 >>"%LOG_FILE%" 2>&1
    if not errorlevel 8 echo migrated>"%RUNTIME_ROOT%\.migration_done"
    if exist "%RUNTIME_ROOT%\.migration_done" rmdir /s /q "%LOCALAPPDATA%\JobSyncRuntime" >nul 2>&1
)

>>"%LOG_FILE%" echo [1/5] Detecting Python 3.13...
where py >nul 2>&1
if not errorlevel 1 (
    py -3.13 --version >>"%LOG_FILE%" 2>&1
    if not errorlevel 1 set "PYTHON_EXE=py -3.13"
)
if not defined PYTHON_EXE (
    for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYTHON_PATH set "PYTHON_PATH=%%P"
    if defined PYTHON_PATH (
        echo "%PYTHON_PATH%" | findstr /i "WindowsApps" >nul
        if errorlevel 1 (
            "%PYTHON_PATH%" --version >>"%LOG_FILE%" 2>&1
            if not errorlevel 1 set "PYTHON_EXE=%PYTHON_PATH%"
        )
    )
)
if not defined PYTHON_EXE (
    >>"%LOG_FILE%" echo Python 3.13 not found; attempting winget installation...
    where winget >nul 2>&1
    if errorlevel 1 goto :fail_python
    winget install --id Python.Python.3.13 -e --source winget --accept-source-agreements --accept-package-agreements --disable-interactivity >>"%LOG_FILE%" 2>&1
    if errorlevel 1 goto :fail_python
    if exist "%LocalAppData%\Programs\Python\Python313\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python313\python.exe"
    if not defined PYTHON_EXE if exist "%ProgramFiles%\Python313\python.exe" set "PYTHON_EXE=%ProgramFiles%\Python313\python.exe"
)
if not defined PYTHON_EXE goto :fail_python
>>"%LOG_FILE%" echo Python selected: %PYTHON_EXE%

rem v1.3.38: NEVER delete the private venv during an in-place upgrade.
rem Reusing the existing environment avoids Windows locks on Python/DLL files and
rem makes upgrades faster. Dependencies are reconciled below with pip.
if "%INSTALLER_MODE%"=="1" if exist "%VENV_PY%" (
    >>"%LOG_FILE%" echo [upgrade] Preserving existing Python virtual environment.
)
rem Repair only an incomplete environment. A complete venv is always preserved.
if not exist "%VENV_DIR%\pyvenv.cfg" if exist "%VENV_DIR%" (
    >>"%LOG_FILE%" echo Removing incomplete virtual environment...
    rmdir /s /q "%VENV_DIR%" >>"%LOG_FILE%" 2>&1
    if exist "%VENV_DIR%" goto :fail_venv_lock
)
if not exist "%VENV_PY%" (
    >>"%LOG_FILE%" echo [2/5] Creating Python virtual environment...
    if "%PYTHON_EXE%"=="py -3.13" (
        py -3.13 -m venv "%VENV_DIR%" >>"%LOG_FILE%" 2>&1
    ) else (
        "%PYTHON_EXE%" -m venv "%VENV_DIR%" >>"%LOG_FILE%" 2>&1
    )
    if errorlevel 1 goto :fail_venv
)
if not exist "%VENV_DIR%\pyvenv.cfg" goto :fail_venv
if not exist "%VENV_PY%" goto :fail_venv

>>"%LOG_FILE%" echo [3/5] Installing Python libraries...
"%VENV_PY%" -m pip install --upgrade pip >>"%LOG_FILE%" 2>&1
if errorlevel 1 goto :fail_pip
"%VENV_PY%" -m pip install --prefer-binary -r "%PROGRAM_DIR%\requirements.txt" >>"%LOG_FILE%" 2>&1
if errorlevel 1 goto :fail_pip

rem v1.3.38: the native desktop launcher is a stable bootstrap component.
rem Do NOT remove/rebuild it on every upgrade. Replacing a PyInstaller OneDir tree
rem while Windows still has one of its DLLs mapped is the source of recurring
rem installer-lock failures. Only build it when it does not exist (fresh install
rem or repair). Existing launcher files are deliberately preserved by the NSIS
rem installer.
set "DESKTOP_DIR=%ROOT%\tools\JobSync"
set "DESKTOP_EXE=%DESKTOP_DIR%\JobSync.exe"
if exist "%DESKTOP_EXE%" (
    >>"%LOG_FILE%" echo [4/6] Existing native JobSync launcher found; preserving it across upgrade.
) else (
    >>"%LOG_FILE%" echo [4/6] Building native JobSync desktop launcher (first install/repair)...
    set "LAUNCHER_TEMP=%TEMP%\JobSync-launcher-build-%RANDOM%-%RANDOM%"
    set "LAUNCHER_DIST=%LAUNCHER_TEMP%\dist"
    set "LAUNCHER_BUILD=%LAUNCHER_TEMP%\build"
    mkdir "%LAUNCHER_DIST%" >nul 2>&1
    mkdir "%LAUNCHER_BUILD%" >nul 2>&1
    "%VENV_PY%" -m pip install --prefer-binary pyinstaller >>"%LOG_FILE%" 2>&1
    if errorlevel 1 goto :fail_pyinstaller
    "%VENV_PY%" -m PyInstaller --noconfirm --clean --onedir --windowed --name JobSync --icon "%ROOT%\tools\JobSync.ico" --collect-all webview --collect-submodules webview --distpath "%LAUNCHER_DIST%" --workpath "%LAUNCHER_BUILD%" --specpath "%LAUNCHER_TEMP%" "%ROOT%\tools\RUN_JOBSYNC_DESKTOP.py" >>"%LOG_FILE%" 2>&1
    if errorlevel 1 goto :fail_pyinstaller
    if not exist "%LAUNCHER_DIST%\JobSync\JobSync.exe" goto :fail_launcher_missing
    mkdir "%DESKTOP_DIR%" >nul 2>&1
    robocopy "%LAUNCHER_DIST%\JobSync" "%DESKTOP_DIR%" /E /COPY:DAT /R:1 /W:1 >>"%LOG_FILE%" 2>&1
    if errorlevel 8 goto :fail_launcher_copy
    rmdir /s /q "%LAUNCHER_TEMP%" >>"%LOG_FILE%" 2>&1
    if not exist "%DESKTOP_EXE%" goto :fail_launcher_missing
)

if exist "%PROGRAM_DIR%\requirements-browser.txt" (
    >>"%LOG_FILE%" echo [5/6] Installing browser automation components...
    "%VENV_PY%" -m pip install --prefer-binary --only-binary=:all: -r "%PROGRAM_DIR%\requirements-browser.txt" >>"%LOG_FILE%" 2>&1
    if errorlevel 1 goto :fail_browser
    "%VENV_PY%" -m playwright install chromium >>"%LOG_FILE%" 2>&1
    if errorlevel 1 goto :fail_browser
)

for %%D in ("%DATA_ROOT%" "%DATA_ROOT%\data" "%DATA_ROOT%\uploads\cv" "%DATA_ROOT%\uploads\coverletters" "%DATA_ROOT%\uploads\references" "%DATA_ROOT%\output\cv" "%DATA_ROOT%\output\coverletters" "%DATA_ROOT%\output\cv_library" "%DATA_ROOT%\output\backups\cv_library" "%DATA_ROOT%\user_blueprints" "%DATA_ROOT%\config") do if not exist "%%~D" mkdir "%%~D" >>"%LOG_FILE%" 2>&1

rem JobSync no longer installs a local TeX editor/compiler.
rem Generated LaTeX is opened in Overleaf using an in-app snip_uri data URL.
>>"%LOG_FILE%" echo [6/6] Overleaf integration ready - no local TeX engine required.
if not exist "%DATA_ROOT%\.env" if exist "%ROOT%\.env.example" copy /y "%ROOT%\.env.example" "%DATA_ROOT%\.env" >>"%LOG_FILE%" 2>&1
for %%T in (cv_base.tex cover_letter_base.tex master_cv.tex) do if exist "%ROOT%\blueprint\%%T" if not exist "%DATA_ROOT%\user_blueprints\%%T" copy /y "%ROOT%\blueprint\%%T" "%DATA_ROOT%\user_blueprints\%%T" >>"%LOG_FILE%" 2>&1
if exist "%ROOT%\config\google_oauth.json.example" if not exist "%DATA_ROOT%\config\google_oauth.json" copy /y "%ROOT%\config\google_oauth.json.example" "%DATA_ROOT%\config\google_oauth.json" >>"%LOG_FILE%" 2>&1

>>"%LOG_FILE%" echo JobSync setup completed successfully: %date% %time%
if "%INSTALLER_MODE%"=="1" echo JobSync setup completed successfully.
exit /b 0

:fail_python
>>"%LOG_FILE%" echo SETUP FAILED: Python 3.13 is unavailable. Errorlevel=%ERRORLEVEL%
goto :fail
:fail_venv_lock
>>"%LOG_FILE%" echo SETUP FAILED: Previous JobSync virtual environment is still locked after process shutdown.
goto :fail

:fail_venv
>>"%LOG_FILE%" echo SETUP FAILED: Could not create a valid Python virtual environment. Errorlevel=%ERRORLEVEL%
goto :fail
:fail_pip
>>"%LOG_FILE%" echo SETUP FAILED: Python dependency installation failed. Errorlevel=%ERRORLEVEL%
goto :fail
:fail_pyinstaller
>>"%LOG_FILE%" echo SETUP FAILED: PyInstaller launcher build failed. Errorlevel=%ERRORLEVEL%
goto :fail
:fail_launcher_missing
>>"%LOG_FILE%" echo SETUP FAILED: PyInstaller did not produce JobSync.exe. Errorlevel=%ERRORLEVEL%
goto :fail
:fail_launcher_copy
>>"%LOG_FILE%" echo SETUP FAILED: Could not copy the native launcher. Errorlevel=%ERRORLEVEL%
goto :fail
:fail_browser
>>"%LOG_FILE%" echo SETUP FAILED: Browser automation components could not be installed. Errorlevel=%ERRORLEVEL%
goto :fail
:fail
>>"%LOG_FILE%" echo ============================================================
>>"%LOG_FILE%" echo Setup failed: %date% %time%
>>"%LOG_FILE%" echo ============================================================
if "%INSTALLER_MODE%"=="1" echo JobSync setup failed. See "%LOG_FILE%".
exit /b 1
