from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from services.app_paths import BASE_DIR as _PACKAGED_BASE_DIR

_DEV_BASE = Path(__file__).resolve().parents[2]
BASE_DIR = _PACKAGED_BASE_DIR if _PACKAGED_BASE_DIR else _DEV_BASE
SESSIONS_FILE = BASE_DIR / "data" / "apply_sessions.json"

_lock = threading.Lock()

# Default window: large enough to comfortably fill out an application,
# positioned near the top-left so it never opens off-screen on a smaller
# display. The user can freely resize/move/minimize it afterwards -- this
# is only the initial placement.
_WINDOW_SIZE = "1280,900"
_WINDOW_POSITION = "80,40"


def _load() -> dict:
    try:
        if SESSIONS_FILE.exists():
            data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save(sessions: dict) -> None:
    try:
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSIONS_FILE.write_text(json.dumps(sessions, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _update_session(session_id: str, **fields) -> None:
    with _lock:
        sessions = _load()
        if session_id in sessions:
            sessions[session_id].update(fields)
            _save(sessions)


def _run_browser(session_id: str, url: str) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _update_session(session_id, status="failed", error=f"Playwright unavailable: {exc}", closed_at=datetime.now().isoformat(timespec="seconds"))
        return

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=False,
                args=[f"--window-size={_WINDOW_SIZE}", f"--window-position={_WINDOW_POSITION}"],
            )
            page = browser.new_page(no_viewport=True)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass  # even if the initial navigation times out, leave the window open for the user
            _update_session(session_id, status="open")
            # Poll until the user closes the window -- this is the confirmation
            # signal that the application is considered done.
            while browser.is_connected():
                time.sleep(1)
    except Exception as exc:
        _update_session(session_id, status="failed", error=str(exc), closed_at=datetime.now().isoformat(timespec="seconds"))
        return

    _update_session(session_id, status="closed", closed_at=datetime.now().isoformat(timespec="seconds"))


def start_apply_session(job: dict) -> str:
    """Launch a real, resizable Chrome window for the user to apply in.

    Returns a session_id. The session's status ("open" -> "closed") is
    tracked in SESSIONS_FILE by a background daemon thread; the caller polls
    get_session()/pop_closed_sessions() on subsequent Streamlit reruns to
    react once the user closes the window.
    """
    url = (job.get("url") or "").strip()
    session_id = f"apply_{int(time.time() * 1000)}"
    with _lock:
        sessions = _load()
        sessions[session_id] = {
            "job": job,
            "url": url,
            "status": "opening",
            "opened_at": datetime.now().isoformat(timespec="seconds"),
            "closed_at": None,
            "error": None,
        }
        _save(sessions)

    thread = threading.Thread(target=_run_browser, args=(session_id, url), daemon=True, name=f"apply_session_{session_id}")
    thread.start()
    return session_id


def get_session(session_id: str) -> dict | None:
    return _load().get(session_id)


def list_sessions() -> dict:
    return _load()


def pop_closed_sessions() -> list[dict]:
    """Return and remove every session that finished (closed or failed)
    since the last call, so the caller can react to each exactly once.
    """
    with _lock:
        sessions = _load()
        done = [s for s in sessions.values() if s.get("status") in ("closed", "failed")]
        remaining = {k: v for k, v in sessions.items() if v.get("status") not in ("closed", "failed")}
        if done:
            _save(remaining)
    return done


def discard_session(session_id: str) -> None:
    with _lock:
        sessions = _load()
        if session_id in sessions:
            del sessions[session_id]
            _save(sessions)
