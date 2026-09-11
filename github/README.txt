Universal GitHub updater

This folder is intentionally isolated from the application code. The updater:
- runs only when the user chooses Settings > Software updates > Check GitHub for updates
- reads the generic repository from update-config.json
- detects the installed version from UPDATE_VERSION.json, VERSION.txt, or the installation folder name
- checks published GitHub releases using the GitHub REST API
- downloads a packaged ZIP release into github/downloads
- never starts automatically and never modifies the running installation
- does not contain application-specific import or module dependencies

Install the downloaded release manually when ready, or replace this folder in a future packaged release.


Release convention

The official release workflow uses:
- VERSION.txt = MAJOR.MINOR.PATCH
- UPDATE_VERSION.json = the same version
- Git tag = vMAJOR.MINOR.PATCH
- ZIP asset = Job-Tracker-vMAJOR.MINOR.PATCH.zip

Run GITRELEASE.bat from the repository root. It removes .venv, creates a clean
release ZIP without runtime/personal data, commits the release state, creates
and pushes the matching Git tag, and publishes the GitHub release automatically
when GitHub CLI (gh) is installed and authenticated.
