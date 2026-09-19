from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

# Supabase publishable key is safe to ship in the desktop client.
# Never put the Supabase secret/service-role key in this file or the installer.
SUPABASE_REST_URL = (
    os.getenv("JOBSYNC_SUPABASE_REST_URL", "https://bldrwjsgrpbyiaowkpqs.supabase.co/rest/v1")
    .strip()
    .rstrip("/")
)
SUPABASE_PUBLISHABLE_KEY = os.getenv(
    "JOBSYNC_SUPABASE_PUBLISHABLE_KEY",
    "sb_publishable_WshU8zZeKxnGWxJLjVYSHA_FZhvQHW-",
).strip()

TABLE = "jobsync_presence"
ONLINE_SECONDS = 60
TIMEOUT = 6


def configured() -> bool:
    return bool(SUPABASE_REST_URL and SUPABASE_PUBLISHABLE_KEY)


def _headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_PUBLISHABLE_KEY,
        "Authorization": f"Bearer {SUPABASE_PUBLISHABLE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def heartbeat_presence(
    *,
    user_id: str,
    display_name: str,
    avatar_seed: str = "",
    contact_email: str = "",
) -> bool:
    if not configured() or not user_id:
        return False

    payload = {
        "presence_id": user_id[:120],
        "display_name": (display_name or "User")[:120],
        "avatar_seed": (avatar_seed or display_name or "User")[:240],
        "contact_email": (contact_email or "")[:240],
        "last_seen": datetime.now(timezone.utc).isoformat(),
    }
    response = requests.post(
        f"{SUPABASE_REST_URL}/{TABLE}",
        params={"on_conflict": "presence_id"},
        headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=payload,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return True


def list_online_users() -> list[dict[str, Any]]:
    if not configured():
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ONLINE_SECONDS)).isoformat()
    response = requests.get(
        f"{SUPABASE_REST_URL}/{TABLE}",
        params={
            "select": "presence_id,display_name,avatar_seed,contact_email,last_seen",
            "last_seen": f"gte.{cutoff}",
            "order": "display_name.asc",
        },
        headers={**_headers(), "Cache-Control": "no-cache", "Pragma": "no-cache"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []
