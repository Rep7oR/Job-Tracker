from __future__ import annotations

import json
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parents[1] / "data" / "state.json"

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
    "documents": [],
    "gmail_updates": [],
    "linkedin_updates": [],
    "search_history": [],
    "settings": {"actor_ids": [], "job_search_mode": "free", "live_monitor_enabled": True, "monitor_interval_hours": 24, "monitor_seeded": False, "monitor_seen_keys": [], "monitor_last_check": "", "monitor_last_new_count": 0, "free_sources": ["Bundesagentur für Arbeit", "LinkedIn", "Indeed", "StepStone", "Monster", "Glassdoor", "Arbeitnow", "Remote OK", "Remotive"], "ats_urls": [], "custom_sections": []},
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


def load_state() -> dict:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return json.loads(json.dumps(DEFAULT_STATE))
    try:
        return _merge_defaults(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except Exception:
        return json.loads(json.dumps(DEFAULT_STATE))


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
