"""Per-account profile storage.

Phase 1 keeps this deliberately simple: one JSON file (``data/profiles.json``)
mapping account email -> profile document. The profile stands in for a full
base-CV upload/parsing pipeline, which is deferred to a later phase.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from services.paths import DATA_DIR

PROFILES_FILE: Path = DATA_DIR / "profiles.json"

EMPTY_PROFILE: dict[str, Any] = {
    "target_role": "",
    "location": "",
    "experience_level": "",
    "background": "",
}

_lock = threading.Lock()


def _load_all() -> dict:
    if not PROFILES_FILE.exists():
        return {}
    try:
        with PROFILES_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_all(profiles: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = PROFILES_FILE.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2, sort_keys=True)
    os.replace(tmp_path, PROFILES_FILE)


def get_profile(email: str) -> dict:
    """Return the stored profile for an account, or an empty template."""
    profiles = _load_all()
    profile = profiles.get(email.strip().lower(), {})
    return {**EMPTY_PROFILE, **profile}


def save_profile(email: str, profile: dict) -> None:
    """Persist the given profile fields for an account."""
    email = email.strip().lower()
    with _lock:
        profiles = _load_all()
        merged = {**EMPTY_PROFILE, **profiles.get(email, {}), **profile}
        profiles[email] = merged
        _save_all(profiles)
