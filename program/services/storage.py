from __future__ import annotations

import json
from pathlib import Path

from services.app_paths import BASE_DIR as _PACKAGED_BASE_DIR

_DEV_BASE = Path(__file__).resolve().parents[1]
_BASE = _PACKAGED_BASE_DIR if _PACKAGED_BASE_DIR else _DEV_BASE
STATE_FILE = _BASE / "data" / "state.json"
USERS_ROOT = _BASE / "data" / "users"

# Set by app after authentication so existing save_state()/load_state()
# calls automatically target the logged-in user's state.
ACTIVE_USER_ID: str | None = None

DEFAULT_STATE = {
    "profile": {
        "name": "",
        "email": "",
        "phone": "",
        "city": "",
        "field": "",
        "industry": "",
        "location": "",
        "experience": "Any",
        "language": "Any",
        "target_titles": [],
    },
    "jobs": [],
    "applied": [],
    "bookmarks": [],
    "documents": [],
    "gmail_updates": [],
    "linkedin_updates": [],
    "search_history": [],
    "settings": {"profile_completed": False, "actor_ids": [], "job_search_mode": "free", "live_monitor_enabled": True, "monitor_interval_hours": 24, "monitor_seeded": False, "monitor_seen_keys": [], "monitor_last_check": "", "monitor_last_new_count": 0, "free_sources": ["Bundesagentur für Arbeit", "LinkedIn", "Indeed", "StepStone", "Monster", "Glassdoor", "Arbeitnow", "Remote OK", "Remotive"], "ats_urls": [], "custom_sections": []},
}







def _merge_defaults(state: dict) -> dict:
    merged = json.loads(json.dumps(DEFAULT_STATE))
    for key, value in state.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    merged.get("settings", {}).pop("ai_default_provider", None)
    return merged


def _state_file_for_user(user_id: str | None) -> Path:
    effective = user_id or ACTIVE_USER_ID
    if not effective:
        return STATE_FILE
    return (USERS_ROOT / str(effective) / "state.json")


def load_state(user_id: str | None = None) -> dict:
    sf = _state_file_for_user(user_id)
    sf.parent.mkdir(parents=True, exist_ok=True)
    if not sf.exists():
        return json.loads(json.dumps(DEFAULT_STATE))

    try:
        return _merge_defaults(json.loads(sf.read_text(encoding="utf-8")))
    except Exception:
        return json.loads(json.dumps(DEFAULT_STATE))


def set_active_user(user_id: str | None) -> None:
    global ACTIVE_USER_ID
    ACTIVE_USER_ID = user_id


def save_state(state: dict, user_id: str | None = None) -> None:
    sf = _state_file_for_user(user_id)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def session_user_roots_exist(user_id: str) -> None:
    # Placeholder for future per-user folder allocation (uploads are shared right now).
    USERS_ROOT.mkdir(parents=True, exist_ok=True)
    (USERS_ROOT / str(user_id)).mkdir(parents=True, exist_ok=True)


def user_state_exists(user_id: str) -> bool:
    return _state_file_for_user(user_id).exists()


def delete_user_state(user_id: str) -> None:
    sf = _state_file_for_user(user_id)
    try:
        if sf.exists():
            sf.unlink()
    except Exception:
        pass


def get_all_user_state_files() -> list[Path]:
    if not USERS_ROOT.exists():
        return []
    return sorted(USERS_ROOT.glob("*/state.json"))
