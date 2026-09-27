# Packaging and install chain

## Build (developer machine)
`tools/BUILD_INSTALLER.ps1`:
1. Reads the version from `VERSION.txt` / `UPDATE_VERSION.json`.
2. Stages an explicit allow-list of application files into `tools/installer/staging/` (never `.venv`, `.git`, `data`, `output`, `uploads`, keys). `Copy-Required` fails the build if any listed file is missing.
3. Fetches `tectonic.exe` for staging (`DOWNLOAD_TECTONIC.ps1`); build still succeeds if that download fails.
4. Patches `!define APP_VERSION` and the `OutFile` name into `tools/installer/JobSync.nsi`.
5. Compiles the patched script with `makensis` into `dist/JobSync-Setup-vX.X.X.exe`.

## Install / upgrade (`tools/installer/JobSync.nsi`)
- Copies staging straight over `$INSTDIR` (in-place update — never wipes the tree, so an existing venv/model cache survives).
- Deletes any `tools/INSTALLER_CLEANUP.ps1` / `tools/INSTALLER_SETUP.bat` left in `$INSTDIR` from the old pre-in-place-update flow.
- Extracts the embedded `tools/INSTALLER_SETUP.bat` (plus the launcher source/spec/icon) to `$PLUGINSDIR` and runs it via a cmd wrapper. It detects/installs Python 3.13, creates `runtime\.venv` (reused on upgrade), installs `program/requirements*.txt`, builds `tools\JobSync\JobSync.exe` with PyInstaller on first install only, and installs Playwright's Chromium if `requirements-browser.txt` is staged.
- Grants the local Users group write access to the writable workspace dirs (`data`, `uploads`, `output`, `config`, `user_blueprints`, `runtime`, `ai`).
- Creates the Desktop and Start Menu shortcuts, pointing straight at `tools\JobSync\JobSync.exe`.
- Runs `tools/INSTALL_INTEGRATIONS.ps1`, which creates the per-user "JobSync - Auto Start" scheduled task (`schtasks`, `ONLOGON`) that runs `tools/START_JOB_TRACKER_STARTUP.vbs`.
- Uninstall stops JobSync processes, deletes the "JobSync - Auto Start" task, removes shortcuts/registry, and deletes the entire `$INSTDIR` tree (all user data included) via a helper `.cmd` that runs after `Uninstall.exe` exits.

## Runtime launch chain
`tools\JobSync\JobSync.exe` (built from `tools/RUN_JOBSYNC_DESKTOP.py` / `.spec`) is the normal launch target and does not touch the scripts below.

The scheduled auto-start task instead goes through:
`START_JOB_TRACKER_STARTUP.vbs` → root `START_JOB_TRACKER.bat` → `tools/RUN_JOBSYNC.vbs` → `tools/RUN_JOBSYNC.ps1`, which starts `JobSync.exe` if present, otherwise falls back to the venv Python running `RUN_JOBSYNC_DESKTOP.py`, and — if `runtime\.venv` is missing entirely — runs `tools/SETUP_FIRST.bat` first to rebuild it. `tools/START_JOB_MONITOR.bat` is started the same way to keep the background job monitor running.

`github/updater.ps1` (Settings → Software updates) checks GitHub Releases and requires a `.exe` release asset — i.e. the file `BUILD_INSTALLER.ps1` produces above.

## Superseded/removed
An older manual-install flow (`INSTALL_DESKTOP.bat`, `INSTALL_STARTUP.bat`, `UNINSTALL_STARTUP.bat`, a stale "Job Tracker - Auto Start" task name) and an older wipe-and-reinstall/source-zip release flow (`INSTALLER_CLEANUP.ps1`, `CREATE_SHAREABLE.ps1`, `GITRELEASE.ps1`) predate the pipeline above and have been removed; none of them were staged by `BUILD_INSTALLER.ps1` or invoked by the current `.nsi`.
