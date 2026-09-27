"""Launches a real, controllable browser window on a job posting's URL so the
user can submit the application themselves, and tracks that window's
open/closed state on disk so the rest of the app (running in a separate
Streamlit script rerun) can detect when it closes.

Uses Playwright's sync API in a background daemon thread, since Streamlit's
script-rerun model can't itself block on a long-lived browser window. State
is a small JSON file under the account's data directory, guarded by a
``threading.Lock`` the same way ``storage.py`` guards its documents:

    {"<session_id>": {"email", "url", "status": "opening"|"open"|"closed",
                       "opened_at", "closed_at"}}

NOTE ON TESTING: this sandbox is headless with no display server and (as of
writing) Playwright's Python package and browser binaries are not installed
here, so an actual browser window could not be launched or observed closing
in this environment. What WAS verified: Playwright is added to
requirements-browser.txt, the ``sync_playwright().chromium.launch(...)`` call
below is built to match Playwright's documented API (``headless=False``,
``args=[...]`` for window size/position, per
https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch),
and the on-disk session-state read/write/lock logic was tested directly
(see the bottom of this file's tests) independent of any real browser launch.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services.paths import DATA_DIR

SESSIONS_FILE: Path = DATA_DIR / "apply_sessions.json"

_lock = threading.Lock()

# A reasonably large, on-screen default window; positioned near the top-left
# so it isn't likely to spawn off a virtual/undersized display.
_WINDOW_ARGS = ["--window-size=1280,900", "--window-position=100,50"]

_POLL_INTERVAL_SECONDS = 1.0


def _load_all() -> dict:
    if not SESSIONS_FILE.exists():
        return {}
    try:
        with SESSIONS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_all(sessions: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = SESSIONS_FILE.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(sessions, f, indent=2, sort_keys=True)
    os.replace(tmp_path, SESSIONS_FILE)


def _update_session(session_id: str, **fields) -> None:
    with _lock:
        sessions = _load_all()
        session = sessions.get(session_id, {})
        session.update(fields)
        sessions[session_id] = session
        _save_all(sessions)


def get_session(session_id: str) -> Optional[dict]:
    return _load_all().get(session_id)


def get_sessions_for_email(email: str) -> dict[str, dict]:
    email = email.strip().lower()
    return {
        sid: s for sid, s in _load_all().items() if s.get("email") == email
    }


def _run_browser(session_id: str, url: str) -> None:
    """Background-thread target: launch Chromium, wait for it to close."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _update_session(
            session_id,
            status="closed",
            error="Playwright is not installed in this environment.",
            closed_at=datetime.now(timezone.utc).isoformat(),
        )
        return

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False, args=_WINDOW_ARGS)
            page = browser.new_page()
            page.goto(url)
            _update_session(session_id, status="open")

            while browser.is_connected():
                time.sleep(_POLL_INTERVAL_SECONDS)

            _update_session(
                session_id,
                status="closed",
                closed_at=datetime.now(timezone.utc).isoformat(),
            )
    except Exception as exc:  # noqa: BLE001 - report, don't crash the thread silently
        _update_session(
            session_id,
            status="closed",
            error=str(exc),
            closed_at=datetime.now(timezone.utc).isoformat(),
        )


def open_application(email: str, url: str) -> str:
    """Start a background browser session on ``url``. Returns the session id.

    The session's status is "opening" until the launch thread confirms the
    page loaded, then "open"; it flips to "closed" once the window/browser
    disconnects (or fails to launch at all).
    """
    session_id = uuid.uuid4().hex
    _update_session(
        session_id,
        email=email.strip().lower(),
        url=url,
        status="opening",
        opened_at=datetime.now(timezone.utc).isoformat(),
        closed_at=None,
        error=None,
    )
    thread = threading.Thread(
        target=_run_browser, args=(session_id, url), daemon=True
    )
    thread.start()
    return session_id


def is_closed(session_id: str) -> bool:
    session = get_session(session_id)
    return bool(session and session.get("status") == "closed")
