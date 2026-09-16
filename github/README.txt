JobSync GitHub updater

The app checks GitHub releases only when requested (plus its existing lightweight availability check).
The updater reads the installed application version from VERSION.txt / UPDATE_VERSION.json, downloads the latest ZIP release asset into github\updates, and reports the real error in Settings if a check fails.
It does not contain or use any private GitHub token.


Update state and downloaded installer files are stored under %LOCALAPPDATA%\JobSync, not inside Program Files, because normal users cannot write to the installed application directory.
