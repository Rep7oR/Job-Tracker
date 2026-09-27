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


# --- Queue (phase 2: agent-staged match candidates) -------------------------
#
# Kept in the same per-account profiles.json document, under a "queue" list,
# rather than a separate file, since it's still small per-account state tied
# 1:1 to the account like the profile is.

QUEUE_FIELD = "queue"


def get_queue(email: str) -> list[dict]:
    """Return the staged queue entries for an account, newest-first."""
    profiles = _load_all()
    profile = profiles.get(email.strip().lower(), {})
    return list(profile.get(QUEUE_FIELD, []))


def add_to_queue(email: str, entries: list[dict]) -> int:
    """Merge new match entries into the account's queue, deduped by URL.

    Each entry is expected to be a normalized posting plus a "score" key;
    a "status" of "staged" is added if not already present. Returns the
    number of genuinely new entries added (existing URLs are left as-is,
    not overwritten, so a re-run doesn't clobber a dismissed/updated entry).
    """
    email = email.strip().lower()
    added = 0
    with _lock:
        profiles = _load_all()
        profile = {**EMPTY_PROFILE, **profiles.get(email, {})}
        queue = list(profile.get(QUEUE_FIELD, []))
        existing_urls = {entry.get("url") for entry in queue if entry.get("url")}

        for entry in entries:
            url = entry.get("url")
            if not url or url in existing_urls:
                continue
            queue.append({**entry, "status": entry.get("status", "staged")})
            existing_urls.add(url)
            added += 1

        profile[QUEUE_FIELD] = queue
        profiles[email] = profile
        _save_all(profiles)
    return added


def remove_from_queue(email: str, url: str) -> None:
    """Remove one queue entry by its posting URL (used by "Dismiss")."""
    email = email.strip().lower()
    with _lock:
        profiles = _load_all()
        profile = {**EMPTY_PROFILE, **profiles.get(email, {})}
        queue = [e for e in profile.get(QUEUE_FIELD, []) if e.get("url") != url]
        profile[QUEUE_FIELD] = queue
        profiles[email] = profile
        _save_all(profiles)


# --- History (phase 3: recorded applications) --------------------------------
#
# Same per-account profiles.json document, under a "history" list, mirroring
# the queue's storage convention.

HISTORY_FIELD = "history"


def get_history(email: str) -> list[dict]:
    """Return the recorded application history for an account."""
    profiles = _load_all()
    profile = profiles.get(email.strip().lower(), {})
    return list(profile.get(HISTORY_FIELD, []))


def add_to_history(email: str, entry: dict) -> None:
    """Append one recorded application to the account's history."""
    email = email.strip().lower()
    with _lock:
        profiles = _load_all()
        profile = {**EMPTY_PROFILE, **profiles.get(email, {})}
        history = list(profile.get(HISTORY_FIELD, []))
        history.append(entry)
        profile[HISTORY_FIELD] = history
        profiles[email] = profile
        _save_all(profiles)
