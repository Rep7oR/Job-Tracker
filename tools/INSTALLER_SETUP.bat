@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem JobSync installer-only setup engine.
rem IMPORTANT: This file is embedded into the NSIS installer and extracted to a
rem temporary directory. Never assume the script directory is the install root.
if "%~1"=="" exit /b 2
set "ROOT=%~1"
cd /d "%ROOT%"
set "PROGRAM_DIR=%ROOT%\program"
set "RUNTIME_ROOT=%ROOT%\runtime"
set "DATA_ROOT=%ROOT%\data"
set "VENV_DIR=%RUNTIME_ROOT%\.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "LOG_FILE=%DATA_ROOT%\setup.log"
set "AI_MODELS_ROOT=%ROOT%\ai\models"
set "PYTHON_EXE="
set "PYTHON_PATH="
set "INSTALLER_MODE=1"
set "INSTALLER_PAYLOAD_DIR=%~dp0"
set "CONSOLE_LOG=1"

rem v1.4.0: installer status is written to BOTH setup.log and the visible
rem CMD window. Detailed command output remains in setup.log.
rem v1.3.43: validate the embedded payload before attempting PyInstaller.
if not exist "%INSTALLER_PAYLOAD_DIR%RUN_JOBSYNC_DESKTOP.py" (
    >>"%LOG_FILE%" echo Missing embedded launcher source: "%INSTALLER_PAYLOAD_DIR%RUN_JOBSYNC_DESKTOP.py"
echo Missing embedded launcher source: "%INSTALLER_PAYLOAD_DIR%RUN_JOBSYNC_DESKTOP.py"
    goto :fail_launcher_source
)
if not exist "%INSTALLER_PAYLOAD_DIR%RUN_JOBSYNC_DESKTOP.spec" (
    >>"%LOG_FILE%" echo Missing embedded launcher spec: "%INSTALLER_PAYLOAD_DIR%RUN_JOBSYNC_DESKTOP.spec"
echo Missing embedded launcher spec: "%INSTALLER_PAYLOAD_DIR%RUN_JOBSYNC_DESKTOP.spec"
    goto :fail_launcher_source
)
if not exist "%INSTALLER_PAYLOAD_DIR%JobSync.ico" (
    >>"%LOG_FILE%" echo Missing embedded launcher icon: "%INSTALLER_PAYLOAD_DIR%JobSync.ico"
echo Missing embedded launcher icon: "%INSTALLER_PAYLOAD_DIR%JobSync.ico"
    goto :fail_launcher_source
)

if not exist "%DATA_ROOT%" mkdir "%DATA_ROOT%" >nul 2>&1
if not exist "%AI_MODELS_ROOT%" mkdir "%AI_MODELS_ROOT%" >nul 2>&1
>>"%LOG_FILE%" echo.
>>"%LOG_FILE%" echo ============================================================
>>"%LOG_FILE%" echo JobSync setup started: %date% %time%
echo JobSync setup started: %date% %time%
>>"%LOG_FILE%" echo Installer setup engine: v1.4.0 EMBEDDED
echo Installer setup engine: v1.4.0 EMBEDDED
>>"%LOG_FILE%" echo Root: %ROOT%
echo Root: %ROOT%
>>"%LOG_FILE%" echo Installer mode: %INSTALLER_MODE%
echo Installer mode: %INSTALLER_MODE%
>>"%LOG_FILE%" echo ============================================================
echo ============================================================
echo JobSync installer setup is running...
echo Live status is shown below; detailed output is saved to setup.log.
echo ============================================================

rem Installer upgrades must happen with every JobSync-owned Python process stopped.
rem The native launcher can spawn a separate venv\python.exe process, so killing
rem JobSync.exe alone is not sufficient.  We match only processes whose command
rem line belongs to this JobSync installation.
if "%INSTALLER_MODE%"=="1" (
    >>"%LOG_FILE%" echo [0/6] Stopping JobSync processes before upgrade...
echo [0/6] Stopping JobSync processes before upgrade...
    rem v1.3.42: do not embed PowerShell code containing $ variables in this BAT.
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
    echo Migrating legacy JobSyncData...
echo Migrating legacy JobSyncData...
    robocopy "%LOCALAPPDATA%\JobSyncData" "%DATA_ROOT%" /E /COPY:DAT /R:1 /W:1 >>"%LOG_FILE%" 2>&1
    if not errorlevel 8 echo migrated>"%DATA_ROOT%\.migration_done"
    if exist "%DATA_ROOT%\.migration_done" rmdir /s /q "%LOCALAPPDATA%\JobSyncData" >nul 2>&1
)
if exist "%LOCALAPPDATA%\JobSyncRuntime" if not exist "%RUNTIME_ROOT%\.migration_done" (
    >>"%LOG_FILE%" echo Migrating legacy JobSyncRuntime...
    echo Migrating legacy JobSyncRuntime...
echo Migrating legacy JobSyncRuntime...
    robocopy "%LOCALAPPDATA%\JobSyncRuntime" "%RUNTIME_ROOT%" /E /COPY:DAT /R:1 /W:1 >>"%LOG_FILE%" 2>&1
    if not errorlevel 8 echo migrated>"%RUNTIME_ROOT%\.migration_done"
    if exist "%RUNTIME_ROOT%\.migration_done" rmdir /s /q "%LOCALAPPDATA%\JobSyncRuntime" >nul 2>&1
)

>>"%LOG_FILE%" echo [1/5] Detecting Python 3.13...
echo [1/5] Detecting Python 3.13...
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
    echo Python 3.13 not found; attempting winget installation...
echo Python 3.13 not found; attempting winget installation...
    where winget >nul 2>&1
    if errorlevel 1 goto :fail_python
    winget install --id Python.Python.3.13 -e --source winget --accept-source-agreements --accept-package-agreements --disable-interactivity >>"%LOG_FILE%" 2>&1
    if errorlevel 1 goto :fail_python
    if exist "%LocalAppData%\Programs\Python\Python313\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python313\python.exe"
    if not defined PYTHON_EXE if exist "%ProgramFiles%\Python313\python.exe" set "PYTHON_EXE=%ProgramFiles%\Python313\python.exe"
)
if not defined PYTHON_EXE goto :fail_python
>>"%LOG_FILE%" echo Python selected: %PYTHON_EXE%
echo Python selected: %PYTHON_EXE%

rem NSIS now updates in place (only overwrites application files that
rem changed) instead of wiping the whole install tree, so an existing venv
rem from a previous install/update is reused here rather than rebuilt from
rem scratch every time - this is what makes updates fast. pip below still
rem runs on every update so a changed requirements.txt is always picked up.
if not exist "%VENV_PY%" (
    >>"%LOG_FILE%" echo [2/5] Creating Python virtual environment...
echo [2/5] Creating Python virtual environment...
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
echo Please wait - pip is installing the application dependencies...
echo [3/5] Installing Python libraries...
"%VENV_PY%" -m pip install --upgrade pip >>"%LOG_FILE%" 2>&1
if errorlevel 1 goto :fail_pip
"%VENV_PY%" -m pip install --prefer-binary -r "%PROGRAM_DIR%\requirements.txt" >>"%LOG_FILE%" 2>&1
if errorlevel 1 goto :fail_pip

rem The native launcher (tools\JobSync\JobSync.exe) is never touched by an
rem in-place update either, so it's only built once on first install and
rem reused after that — the guard below just skips rebuilding when it's
rem already there.
set "DESKTOP_DIR=%ROOT%\tools\JobSync"
set "DESKTOP_EXE=%DESKTOP_DIR%\JobSync.exe"
>>"!LOG_FILE!" echo [4/6] Building fresh native JobSync desktop launcher...
echo [4/6] Building fresh native JobSync desktop launcher...
if not exist "%DESKTOP_EXE%" (
    set "LAUNCHER_TEMP=%TEMP%\JobSync-launcher-build-%RANDOM%-%RANDOM%"
    set "LAUNCHER_DIST=!LAUNCHER_TEMP!\dist"
    set "LAUNCHER_BUILD=!LAUNCHER_TEMP!\build"
    set "LAUNCHER_SCRIPT=!LAUNCHER_TEMP!\RUN_JOBSYNC_DESKTOP.py"
    set "LAUNCHER_ICON=!LAUNCHER_TEMP!\JobSync.ico"
    set "LAUNCHER_SPEC=!LAUNCHER_TEMP!\RUN_JOBSYNC_DESKTOP.spec"
    mkdir "!LAUNCHER_TEMP!" >nul 2>&1
    mkdir "!LAUNCHER_DIST!" >nul 2>&1
    mkdir "!LAUNCHER_BUILD!" >nul 2>&1
    rem v1.3.42: launcher build inputs are embedded beside this installer-only
    rem script in $PLUGINSDIR. Never depend on files that NSIS just removed/recreated
    rem under Program Files. This also makes the source preparation independent of
    rem NSIS recursive File behavior.
    >>"!LOG_FILE!" echo Launcher payload directory: !INSTALLER_PAYLOAD_DIR!
    echo Launcher payload prepared. Building native launcher...
    echo Please wait - PyInstaller is building the native JobSync launcher...
    copy /y "!INSTALLER_PAYLOAD_DIR!RUN_JOBSYNC_DESKTOP.py" "!LAUNCHER_SCRIPT!" >>"!LOG_FILE!" 2>&1
    copy /y "!INSTALLER_PAYLOAD_DIR!JobSync.ico" "!LAUNCHER_ICON!" >>"!LOG_FILE!" 2>&1
    copy /y "!INSTALLER_PAYLOAD_DIR!RUN_JOBSYNC_DESKTOP.spec" "!LAUNCHER_SPEC!" >>"!LOG_FILE!" 2>&1
    if not exist "!LAUNCHER_SCRIPT!" (>>"!LOG_FILE!" echo Missing temporary launcher script: "!LAUNCHER_SCRIPT!") & goto :fail_launcher_source
    if not exist "!LAUNCHER_ICON!" (>>"!LOG_FILE!" echo Missing temporary launcher icon: "!LAUNCHER_ICON!") & goto :fail_launcher_source
    if not exist "!LAUNCHER_SPEC!" (>>"!LOG_FILE!" echo Missing temporary launcher spec: "!LAUNCHER_SPEC!") & goto :fail_launcher_source
    "%VENV_PY%" -m pip install --prefer-binary pyinstaller >>"!LOG_FILE!" 2>&1
    if errorlevel 1 goto :fail_pyinstaller
    rem v1.3.42: invoke a pre-written .spec file directly. This completely
    rem bypasses PyInstaller's makespec phase, which was calling os.makedirs('')
    rem and failing with WinError 3 under the elevated installer.
    pushd "!LAUNCHER_TEMP!" >nul 2>&1
    if errorlevel 1 goto :fail_launcher_source
    "%VENV_PY%" -m PyInstaller --noconfirm --clean --distpath "!LAUNCHER_DIST!" --workpath "!LAUNCHER_BUILD!" "!LAUNCHER_SPEC!" >>"!LOG_FILE!" 2>&1
    set "PYI_RC=!ERRORLEVEL!"
    popd >nul 2>&1
    if not "!PYI_RC!"=="0" goto :fail_pyinstaller
    if not exist "!LAUNCHER_DIST!\JobSync\JobSync.exe" goto :fail_launcher_missing
    mkdir "%DESKTOP_DIR%" >nul 2>&1
    robocopy "!LAUNCHER_DIST!\JobSync" "%DESKTOP_DIR%" /E /COPY:DAT /R:1 /W:1 >>"!LOG_FILE!" 2>&1
    if errorlevel 8 goto :fail_launcher_copy
    rmdir /s /q "!LAUNCHER_TEMP!" >>"!LOG_FILE!" 2>&1
    if not exist "%DESKTOP_EXE%" goto :fail_launcher_missing
)

if exist "%PROGRAM_DIR%\requirements-browser.txt" (
    >>"%LOG_FILE%" echo [5/6] Installing browser automation components...
echo [5/6] Installing browser automation components...
    >>"%LOG_FILE%" echo Installing Playwright Python package...
echo Installing Playwright Python package...
    "%VENV_PY%" -m pip install --prefer-binary --only-binary=:all: -r "%PROGRAM_DIR%\requirements-browser.txt" >>"%LOG_FILE%" 2>&1
    set "BROWSER_PIP_RC=!ERRORLEVEL!"
    >>"%LOG_FILE%" echo Playwright package installation exit code: !BROWSER_PIP_RC!
echo Playwright package installation exit code: !BROWSER_PIP_RC!
    if not "!BROWSER_PIP_RC!"=="0" goto :fail_browser

    rem v1.3.47: Chromium installation can take several minutes and can appear
    rem frozen while the Playwright browser archive is downloaded/extracted.
    rem Run it through a helper PowerShell process with a hard 20-minute timeout,
    rem and log explicit start/end markers so the installer can never silently
    rem disappear at this point.
    set "PLAYWRIGHT_LOG=%DATA_ROOT%\playwright-install.log"
    >>"%LOG_FILE%" echo Installing Playwright Chromium browser...
echo Please wait - Chromium may take several minutes to download and extract...
echo Installing Playwright Chromium browser...
    >>"%LOG_FILE%" echo Playwright browser log: !PLAYWRIGHT_LOG!
echo Playwright browser log: !PLAYWRIGHT_LOG!
    del /q "!PLAYWRIGHT_LOG!" >nul 2>&1
    rem v1.3.47: Start-Process requires DIFFERENT files for stdout and stderr.
    rem v1.3.45 redirected both streams to one file, so Playwright never started.
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "& '%VENV_PY%' -m playwright install chromium; exit $LASTEXITCODE" >>"%LOG_FILE%" 2>&1
    set "BROWSER_RC=!ERRORLEVEL!"
    >>"%LOG_FILE%" echo Playwright Chromium installation exit code: !BROWSER_RC!
echo Playwright Chromium installation exit code: !BROWSER_RC!
    if exist "!PLAYWRIGHT_LOG!" type "!PLAYWRIGHT_LOG!" >>"%LOG_FILE%"
    if "!BROWSER_RC!"=="124" (
        >>"%LOG_FILE%" echo SETUP FAILED: Playwright Chromium installation timed out after 20 minutes.
        echo SETUP FAILED: Playwright Chromium installation timed out after 20 minutes.
        goto :fail_browser
    )
    if not "!BROWSER_RC!"=="0" goto :fail_browser
    >>"%LOG_FILE%" echo Playwright Chromium installation completed successfully.
echo Playwright Chromium installation completed successfully.
) else (
    >>"%LOG_FILE%" echo [5/6] Browser automation requirements not present; skipping browser installation.
echo [5/6] Browser automation requirements not present; skipping browser installation.
)

for %%D in ("%DATA_ROOT%" "%DATA_ROOT%\data" "%DATA_ROOT%\uploads\cv" "%DATA_ROOT%\uploads\coverletters" "%DATA_ROOT%\uploads\references" "%DATA_ROOT%\output\cv" "%DATA_ROOT%\output\coverletters" "%DATA_ROOT%\output\cv_library" "%DATA_ROOT%\output\backups\cv_library" "%DATA_ROOT%\user_blueprints" "%DATA_ROOT%\config") do if not exist "%%~D" mkdir "%%~D" >>"%LOG_FILE%" 2>&1

rem JobSync no longer installs a local TeX editor/compiler.
rem Generated LaTeX is saved to the user's Folders and opened in Overleaf via
rem an in-app snip_uri data URL. This keeps the installer offline from TeX
rem tooling and avoids shipping/downloading a LaTeX distribution.
>>"%LOG_FILE%" echo [6/6] Overleaf integration ready - no local TeX engine required.
echo [6/6] Overleaf integration ready - no local TeX engine required.
if not exist "%DATA_ROOT%\.env" if exist "%ROOT%\.env.example" copy /y "%ROOT%\.env.example" "%DATA_ROOT%\.env" >>"%LOG_FILE%" 2>&1
for %%T in (cv_base.tex cover_letter_base.tex master_cv.tex) do if exist "%ROOT%\blueprint\%%T" if not exist "%DATA_ROOT%\user_blueprints\%%T" copy /y "%ROOT%\blueprint\%%T" "%DATA_ROOT%\user_blueprints\%%T" >>"%LOG_FILE%" 2>&1
if exist "%ROOT%\config\google_oauth.json.example" if not exist "%DATA_ROOT%\config\google_oauth.json" copy /y "%ROOT%\config\google_oauth.json.example" "%DATA_ROOT%\config\google_oauth.json" >>"%LOG_FILE%" 2>&1

>>"%LOG_FILE%" echo JobSync setup completed successfully: %date% %time%
echo JobSync setup completed successfully: %date% %time%
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
:fail_launcher_source
>>"%LOG_FILE%" echo SETUP FAILED: Could not prepare temporary PyInstaller launcher source files.
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
