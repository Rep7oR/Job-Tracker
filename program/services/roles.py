from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
ROLES_FILE = BASE_DIR / "data" / "roles.json"
ROLE_OPTIONS = ("admin", "moderator", "member")
ROLE_ACCESS = {"admin": 3, "moderator": 2, "member": 1}


def _load() -> dict:
    if not ROLES_FILE.exists():
        return {}
    try:
        data = json.loads(ROLES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    ROLES_FILE.parent.mkdir(parents=True, exist_ok=True)
    ROLES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def normalize_role(role: str) -> str:
    role = str(role or "member").strip().lower()
    return role if role in ROLE_OPTIONS else "member"


def ensure_user(email: str, display_name: str = "", default_role: str = "member") -> dict:
    email = str(email or "").strip().lower()
    if not email:
        return {"email": "", "display_name": display_name or "User", "role": normalize_role(default_role)}
    data = _load()
    record = data.get(email)
    if not isinstance(record, dict):
        record = {"email": email, "display_name": display_name or "User", "role": normalize_role(default_role), "last_seen": ""}
    else:
        record["email"] = email
        if display_name:
            record["display_name"] = display_name
        record["role"] = normalize_role(record.get("role", default_role))
    data[email] = record
    _save(data)
    return record


def get_user_role(email: str, default: str = "member") -> str:
    email = str(email or "").strip().lower()
    if not email:
        return normalize_role(default)
    record = _load().get(email)
    if not isinstance(record, dict):
        return normalize_role(default)
    return normalize_role(record.get("role", default))


def set_user_role(email: str, role: str, display_name: str = "") -> dict:
    email = str(email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("Enter a valid user email address.")
    normalized = normalize_role(role)
    data = _load()
    existing = data.get(email) if isinstance(data.get(email), dict) else {}
    existing.update({"email": email, "display_name": display_name or existing.get("display_name") or "User", "role": normalized})
    existing.setdefault("last_seen", "")
    data[email] = existing
    _save(data)
    return existing


def touch_user(email: str, display_name: str = "") -> dict:
    from datetime import datetime
    record = ensure_user(email, display_name)
    record["last_seen"] = datetime.now().isoformat(timespec="seconds")
    data = _load()
    data[record["email"]] = record
    _save(data)
    return record


def list_users() -> list[dict]:
    data = _load()
    rows = [v for v in data.values() if isinstance(v, dict) and v.get("email")]
    rows.sort(key=lambda x: (str(x.get("display_name") or x.get("email") or "").lower()))
    return rows


def update_user(email: str, *, display_name: str | None = None, role: str | None = None, blocked: bool | None = None) -> dict:
    email = str(email or "").strip().lower()
    data = _load()
    record = data.get(email) if isinstance(data.get(email), dict) else None
    if not record:
        raise ValueError("User is not present in the JobSync roster.")
    if display_name is not None:
        name = str(display_name).strip()
        record["display_name"] = name or record.get("display_name") or "User"
    if role is not None:
        record["role"] = normalize_role(role)
    if blocked is not None:
        record["blocked"] = bool(blocked)
        if blocked:
            record["blocked_at"] = __import__('datetime').datetime.now().isoformat(timespec='seconds')
        else:
            record.pop("blocked_at", None)
    data[email] = record
    _save(data)
    return record


def remove_user(email: str) -> None:
    email = str(email or "").strip().lower()
    data = _load()
    if email in data:
        del data[email]
        _save(data)


def is_user_blocked(email: str) -> bool:
    email = str(email or "").strip().lower()
    record = _load().get(email)
    return bool(isinstance(record, dict) and record.get("blocked"))
