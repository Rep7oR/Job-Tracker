
## Build 1.2.2.6 changes
- Logged-out Home no longer shows Login/Create Account cards; authentication remains in the left navigation.
- Home centers the JobSync description and Discord/WhatsApp links.
- CV/reference LaTeX uploads remain optional.
- Latest CV and cover letter can be uploaded together from one menu.
- Role/presence foundation and responsive sidebar behavior retained.



## Windows dependency note
The core JobSync application does not require Playwright/greenlet to start. Browser automation is an optional feature installed from `requirements-browser.txt` using binary wheels only, so the launcher does not try to compile greenlet with Microsoft Visual C++. If no compatible browser wheel exists for the installed Python, the main dashboard still starts.


### Build 1.2.2.6 sidebar toggle
- Replaced reliance on Streamlit's remembered collapsed-sidebar state with a deterministic JobSync toggle.
- Sidebar always opens on launch.
- The hide button collapses the menu visually.
- A permanent left-edge ☰ button reopens the menu.

### Build 1.2.2.6 fixes
- Presence no longer reports the current authenticated session as offline when shared Supabase presence is unavailable; the current session remains shown as online.
- Sidebar collapse control is a full-width labelled `« Hide navigation` button so the text/icon is not clipped.


## Final structured layout
- `START_JOB_TRACKER.bat` is the only launcher kept at the installation root.
- `program/` contains the application code and dependencies.
- `config/`, `blueprint/`, `data/`, `uploads/`, and `output/` contain categorized workspace files.
- `github/` contains a universal GitHub release checker/downloader only; it is never run automatically at startup.
- Run **Settings → Software updates → Check GitHub for updates** to check and download a newer release.
