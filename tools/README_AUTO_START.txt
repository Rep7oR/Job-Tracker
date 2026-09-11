Job Tracker Windows integrations

The root START_JOB_TRACKER.bat is the only launcher intended to be used directly.
It automatically ensures:
  - a desktop shortcut named "Job Tracker.lnk" exists
  - a per-user Scheduled Task named "Job Tracker - Auto Start" starts the app at Windows logon
  - the background job monitor is running

The startup task does not check GitHub. GitHub updates remain manual from Settings.

To remove automatic startup:
  run tools\UNINSTALL_STARTUP.bat

To recreate the desktop shortcut:
  run tools\INSTALL_DESKTOP.bat
