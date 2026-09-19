; JobSync Windows Installer
; Built with NSIS (Nullsoft Scriptable Install System)
; Download NSIS: https://nsis.sourceforge.io/Download
;
; To compile:
;   makensis JobSync.nsi
; Or use BUILD_INSTALLER.ps1 which does this automatically.

!include "MUI2.nsh"
!include "LogicLib.nsh"

; ── Version ────────────────────────────────────────────────────────────────────
!define APP_NAME     "JobSync"
!define APP_VERSION  "1.4.0"
!define PUBLISHER    "JobSync"
!define APP_URL      "https://github.com/Rep7oR/Job-Tracker"
!define INSTALL_DIR  "$PROGRAMFILES64\${APP_NAME}"
!define UNINST_KEY   "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
!define SETUP_SCRIPT "tools\INSTALLER_SETUP.bat"
!define LAUNCHER_VBS "tools\RUN_JOBSYNC.vbs"
!define LAUNCHER_EXE "tools\JobSync\JobSync.exe"
!define APP_ICON     "tools\JobSync.ico"

; ── Installer metadata ─────────────────────────────────────────────────────────
Name          "${APP_NAME} ${APP_VERSION}"
OutFile       "..\..\dist\JobSync-Setup-${APP_VERSION}.exe"
InstallDir    "${INSTALL_DIR}"
RequestExecutionLevel admin  ; Machine-wide install under Program Files
SetCompressor /SOLID lzma
Unicode True

; ── MUI pages ─────────────────────────────────────────────────────────────────
!define MUI_ICON                     "..\..\tools\JobSync.ico"
!define MUI_UNICON                   "..\..\tools\JobSync.ico"
!define MUI_WELCOMEPAGE_TITLE        "Welcome to ${APP_NAME} Setup"
!define MUI_WELCOMEPAGE_TEXT         "This wizard will install ${APP_NAME} ${APP_VERSION} on your computer.$\r$\n$\r$\nJobSync is your local job-search workspace — job search, CV generation, Gmail & LinkedIn sync, all in one place.$\r$\n$\r$\nClick Install to continue."
!define MUI_FINISHPAGE_RUN           "$INSTDIR\${LAUNCHER_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT      "Launch ${APP_NAME} now"
!define MUI_FINISHPAGE_RUN_NOTCHECKED
!define MUI_FINISHPAGE_SHOWREADME    ""
!define MUI_ABORTWARNING

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; ── Install section ────────────────────────────────────────────────────────────
Section "Install" SecMain

  ; -- IN-PLACE UPDATE ---------------------------------------------------------
  ; Earlier releases wiped the ENTIRE $INSTDIR tree on every update (including
  ; the Python venv under runtime\, which then had to be rebuilt from scratch
  ; every single time — the main reason updates were slow) and moved user data
  ; out to a temp backup and back in. Since staging only ever contains the
  ; explicit application-file allow-list built by BUILD_INSTALLER.ps1 (never
  ; data/uploads/output/config/user_blueprints/ai/.env/runtime), copying it
  ; straight over an existing $INSTDIR already only touches files that
  ; actually changed and never disturbs anything else — no backup/restore
  ; dance needed, and nothing but Uninstall ever removes user data or the venv.
  DetailPrint "Stopping JobSync before updating..."
  nsExec::ExecToLog `taskkill /F /IM JobSync.exe /T`
  nsExec::ExecToLog `taskkill /F /IM streamlit.exe /T`
  nsExec::ExecToLog `taskkill /F /IM ollama.exe /T`
  Sleep 2000

  CreateDirectory "$INSTDIR"
  SetOutPath "$INSTDIR"

  ; -- Copy only the application payload — overwrites files that changed and
  ;    adds new ones; anything already in $INSTDIR that isn't part of staging
  ;    (user data, the venv, model cache) is left completely untouched.
  File /r "staging\*"
  ; These two are installer-only helpers from the old clean-upgrade flow and
  ; must never remain in the installed app; harmless if already absent.
  Delete "$INSTDIR\tools\INSTALLER_CLEANUP.ps1"
  Delete "$INSTDIR\tools\INSTALLER_SETUP.bat"

  ; -- Run setup through an installer-only script embedded in THIS installer.
  ;    The script is extracted to $PLUGINSDIR and receives the real install root
  ;    explicitly. This means an upgrade can never execute a stale SETUP_FIRST.bat
  ;    or any missing legacy helper from an older installation.
  DetailPrint "Setting up JobSync (this may take a few minutes)..."
  FileOpen $9 "$INSTDIR\data\setup.log" a
  FileWrite $9 "$\r$\n[INSTALLER] Launching embedded INSTALLER_SETUP.bat v${APP_VERSION}$\r$\n"
  FileClose $9

  ; v1.3.43: explicitly initialize the NSIS plug-in directory before
  ; extracting installer-only build inputs. Do not rely on SetOutPath alone.
  ; Each file gets an explicit output name so NSIS cannot resolve the runtime
  ; destination ambiguously.
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=INSTALLER_SETUP.bat "staging\tools\INSTALLER_SETUP.bat"
  File /oname=RUN_JOBSYNC_DESKTOP.py "staging\tools\RUN_JOBSYNC_DESKTOP.py"
  File /oname=RUN_JOBSYNC_DESKTOP.spec "staging\tools\RUN_JOBSYNC_DESKTOP.spec"
  File /oname=JobSync.ico "staging\tools\JobSync.ico"
  FileOpen $8 "$PLUGINSDIR\JobSync-Setup-Wrapper.cmd" w
  FileWrite $8 "@echo off$\r$\n"
  FileWrite $8 "call $\"$PLUGINSDIR\INSTALLER_SETUP.bat$\" $\"$INSTDIR$\"$\r$\n"
  FileWrite $8 "set $\"RC=%ERRORLEVEL%$\"$\r$\n"
  FileWrite $8 "exit /b %RC%$\r$\n"
  FileClose $8

  ExecWait '"$SYSDIR\cmd.exe" /D /S /C ""$PLUGINSDIR\JobSync-Setup-Wrapper.cmd""' $0
  FileOpen $9 "$INSTDIR\data\setup.log" a
  FileWrite $9 "$\r$\n[INSTALLER] Embedded INSTALLER_SETUP.bat returned exit code $0$\r$\n"
  FileClose $9
  ${If} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "JobSync setup failed (exit code $0).$\r$\n$\r$\nDiagnostic log:$\r$\n$INSTDIR\data\setup.log"
    Abort
  ${EndIf}

  ; -- Verify setup output before creating shortcuts.
  ${IfNot} ${FileExists} "$INSTDIR\runtime\.venv\Scripts\python.exe"
    MessageBox MB_OK|MB_ICONSTOP "JobSync setup reported success, but the Python runtime was not created.$\r$\n$\r$\nCheck:$\r$\n$INSTDIR\data\setup.log"
    Abort
  ${EndIf}
  ${IfNot} ${FileExists} "$INSTDIR\tools\JobSync\JobSync.exe"
    MessageBox MB_OK|MB_ICONSTOP "JobSync setup reported success, but JobSync.exe was not created.$\r$\n$\r$\nCheck:$\r$\n$INSTDIR\data\setup.log"
    Abort
  ${EndIf}

  ; -- Program Files is protected by Windows. JobSync deliberately keeps its
  ;    writable workspace beside the installation, so grant the local Users
  ;    group Modify access to EACH writable tree. (icacls accepts one path per
  ;    invocation; passing several paths in one call is not reliable.)
  DetailPrint "Configuring local JobSync workspace permissions..."
  CreateDirectory "$INSTDIR\data"
  CreateDirectory "$INSTDIR\uploads"
  CreateDirectory "$INSTDIR\output"
  CreateDirectory "$INSTDIR\config"
  CreateDirectory "$INSTDIR\user_blueprints"
  CreateDirectory "$INSTDIR\runtime"
  CreateDirectory "$INSTDIR\ai"
  CreateDirectory "$INSTDIR\ai\models"
  nsExec::ExecToLog `"$SYSDIR\icacls.exe" "$INSTDIR\data" /grant *S-1-5-32-545:(OI)(CI)M /T /C`
  nsExec::ExecToLog `"$SYSDIR\icacls.exe" "$INSTDIR\uploads" /grant *S-1-5-32-545:(OI)(CI)M /T /C`
  nsExec::ExecToLog `"$SYSDIR\icacls.exe" "$INSTDIR\output" /grant *S-1-5-32-545:(OI)(CI)M /T /C`
  nsExec::ExecToLog `"$SYSDIR\icacls.exe" "$INSTDIR\config" /grant *S-1-5-32-545:(OI)(CI)M /T /C`
  nsExec::ExecToLog `"$SYSDIR\icacls.exe" "$INSTDIR\user_blueprints" /grant *S-1-5-32-545:(OI)(CI)M /T /C`
  nsExec::ExecToLog `"$SYSDIR\icacls.exe" "$INSTDIR\runtime" /grant *S-1-5-32-545:(OI)(CI)M /T /C`
  nsExec::ExecToLog `"$SYSDIR\icacls.exe" "$INSTDIR\ai" /grant *S-1-5-32-545:(OI)(CI)M /T /C`

  ; -- Desktop shortcut
  CreateShortcut "$DESKTOP\${APP_NAME}.lnk" \
    "$INSTDIR\${LAUNCHER_EXE}" \
    "" \
    "$INSTDIR\${APP_ICON}" 0 \
    SW_SHOWMINIMIZED "" "Open ${APP_NAME}"

  ; -- Start Menu shortcut
  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" \
    "$INSTDIR\${LAUNCHER_EXE}" \
    "" \
    "$INSTDIR\${APP_ICON}" 0 \
    SW_SHOWMINIMIZED "" "Open ${APP_NAME}"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk" \
    "$INSTDIR\Uninstall.exe" "" \
    "$INSTDIR\${APP_ICON}" 0

  ; -- Register with Add/Remove Programs
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr   HKLM "${UNINST_KEY}" "DisplayName"      "${APP_NAME}"
  WriteRegStr   HKLM "${UNINST_KEY}" "DisplayVersion"   "${APP_VERSION}"
  WriteRegStr   HKLM "${UNINST_KEY}" "Publisher"        "${PUBLISHER}"
  WriteRegStr   HKLM "${UNINST_KEY}" "URLInfoAbout"     "${APP_URL}"
  WriteRegStr   HKLM "${UNINST_KEY}" "InstallLocation"  "$INSTDIR"
  WriteRegStr   HKLM "${UNINST_KEY}" "UninstallString"  '"$INSTDIR\Uninstall.exe"'
  WriteRegStr   HKLM "${UNINST_KEY}" "DisplayIcon"      "$INSTDIR\${APP_ICON},0"
  WriteRegDWORD HKLM "${UNINST_KEY}" "NoModify"         1
  WriteRegDWORD HKLM "${UNINST_KEY}" "NoRepair"         1

  ; -- Startup task (auto-launch on Windows sign-in)
  nsExec::ExecToLog /TIMEOUT=30000 \
    `powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass \
      -File "$INSTDIR\tools\INSTALL_INTEGRATIONS.ps1" -Quiet`

SectionEnd

; ── Uninstall section ──────────────────────────────────────────────────────────
Section "Uninstall"

  ; Stop JobSync processes. The app and its updater are per-user, so no admin
  ; service is installed. Remove the scheduled startup task as well.
  nsExec::ExecToLog `taskkill /F /IM streamlit.exe`
  nsExec::ExecToLog `schtasks.exe /Delete /TN "JobSync - Auto Start" /F`

  ; Remove shortcuts and Add/Remove Programs entry.
  Delete "$DESKTOP\${APP_NAME}.lnk"
  Delete "$DESKTOP\Job Tracker.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk"
  RMDir  "$SMPROGRAMS\${APP_NAME}"
  DeleteRegKey HKLM "${UNINST_KEY}"

  ; Everything is intentionally contained inside %ProgramFiles%\JobSync.
  ; Uninstall therefore removes the complete application AND all local user data,
  ; including CVs, jobs, profiles, generated files, runtime, logs and settings.
  ;
  ; The uninstaller itself is inside $INSTDIR and Windows keeps that EXE locked
  ; while it is running.  Therefore schedule the final directory removal for
  ; immediately after this uninstaller exits.  This prevents the common case
  ; where the uninstall wizard finishes but the JobSync folder remains.
  RMDir /r "$INSTDIR\program"
  RMDir /r "$INSTDIR\blueprint"
  RMDir /r "$INSTDIR\github"
  RMDir /r "$INSTDIR\tools"
  RMDir /r "$INSTDIR\runtime"
  RMDir /r "$INSTDIR\data"
  RMDir /r "$INSTDIR\uploads"
  RMDir /r "$INSTDIR\output"
  RMDir /r "$INSTDIR\config"
  RMDir /r "$INSTDIR\user_blueprints"
  Delete "$INSTDIR\.env"
  Delete "$INSTDIR\VERSION.txt"
  Delete "$INSTDIR\UPDATE_VERSION.json"
  Delete "$INSTDIR\START_JOB_TRACKER.bat"

  ; Final cleanup must happen after Uninstall.exe exits.
  ; The helper is created outside $INSTDIR because Uninstall.exe is locked
  ; until the process exits. It waits for this process to finish, removes the
  ; complete JobSync directory, and then deletes itself.
  FileOpen $0 "$TEMP\JobSync-Uninstall-Cleanup.cmd" w
  FileWrite $0 "@echo off$\r$\n"
  FileWrite $0 "timeout /t 2 /nobreak >nul$\r$\n"
  FileWrite $0 "rmdir /s /q $\"$INSTDIR$\" >nul 2>&1$\r$\n"
  FileWrite $0 "del /f /q $\"%~f0$\" >nul 2>&1$\r$\n"
  FileClose $0
  Exec '"$SYSDIR\cmd.exe" /c "start "" /min "$TEMP\JobSync-Uninstall-Cleanup.cmd""'

SectionEnd
