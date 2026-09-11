from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parents[1]
PRESENCE_ID_FILE = BASE_DIR / "data" / "presence_id.json"
ONLINE_SECONDS = 90
TIMEOUT = 8

# JobSync shared presence backend. This is a Supabase PUBLISHABLE key,
# which is specifically designed to be shipped in desktop/client applications.
# Access is controlled by the user_presence table's RLS policies.
DEFAULT_SUPABASE_URL = "https://xmydytxgvkniyoboyayn.supabase.co"
DEFAULT_SUPABASE_PUBLISHABLE_KEY = "sb_publishable_4OrKeWLKR2WMCHAdnKuThw_uvI1T6OP"


def _settings() -> tuple[str, str]:
    url = (os.getenv("SUPABASE_URL", "").strip().rstrip("/") or DEFAULT_SUPABASE_URL)
    key = (
        os.getenv("SUPABASE_PUBLISHABLE_KEY", "").strip()
        or os.getenv("SUPABASE_ANON_KEY", "").strip()
        or DEFAULT_SUPABASE_PUBLISHABLE_KEY
    )
    return url, key


def configured() -> bool:
    url, key = _settings()
    return bool(url and key)


def _presence_id() -> str:
    if PRESENCE_ID_FILE.exists():
        try:
            value = json.loads(PRESENCE_ID_FILE.read_text(encoding="utf-8"))
            if isinstance(value, dict) and value.get("presence_id"):
                return str(value["presence_id"])
        except Exception:
            pass
    value = secrets.token_urlsafe(24)
    PRESENCE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PRESENCE_ID_FILE.write_text(json.dumps({"presence_id": value}, indent=2), encoding="utf-8")
    return value


def _headers() -> dict[str, str]:
    _, key = _settings()
    # New Supabase publishable keys are not JWTs. Send them via `apikey` only.
    return {"apikey": key, "Content-Type": "application/json", "Accept": "application/json"}


def heartbeat_presence(display_name: str, avatar_seed: str, email: str = "", role: str = "member") -> bool:
    url, key = _settings()
    if not url or not key:
        return False
    payload = {
        "presence_id": _presence_id(),
        "display_name": display_name[:120],
        "avatar_seed": avatar_seed[:240],
        "email": str(email or "").strip().lower()[:240],
        "role": str(role or "member").strip().lower()[:40],
        "last_seen": datetime.now(timezone.utc).isoformat(),
    }
    response = requests.post(
        f"{url}/rest/v1/user_presence?on_conflict=presence_id",
        headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=payload,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return True


def list_online_users() -> list[dict]:
    url, key = _settings()
    if not url or not key:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ONLINE_SECONDS)
    response = requests.get(
        f"{url}/rest/v1/user_presence",
        headers=_headers(),
        params={
            "select": "presence_id,display_name,avatar_seed,email,role,last_seen",
            "last_seen": f"gt.{cutoff.isoformat()}",
            "order": "display_name.asc",
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def remove_presence() -> None:
    url, key = _settings()
    if not url or not key:
        return
    requests.delete(
        f"{url}/rest/v1/user_presence",
        headers={**_headers(), "Prefer": "return=minimal"},
        params={"presence_id": f"eq.{_presence_id()}"},
        timeout=TIMEOUT,
    )
