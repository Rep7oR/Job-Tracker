from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
AUTH_FILE = BASE_DIR / "data" / "account.json"
ITERATIONS = 310_000


def account_exists() -> bool:
    return AUTH_FILE.exists()


def _read_account() -> dict | None:
    if not AUTH_FILE.exists():
        return None
    try:
        data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def create_account(email: str, password: str) -> None:
    email = email.strip().lower()
    if not email or "@" not in email:
        raise ValueError("Enter a valid email address.")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    if AUTH_FILE.exists():
        raise ValueError("A local JobSync account already exists on this laptop.")

    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, ITERATIONS
    )
    record = {
        "email": email,
        "salt": base64.b64encode(salt).decode("ascii"),
        "password_hash": base64.b64encode(digest).decode("ascii"),
        "iterations": ITERATIONS,
    }
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps(record, indent=2), encoding="utf-8")


def verify_login(email: str, password: str) -> bool:
    account = _read_account()
    if not account:
        return False

    try:
        salt = base64.b64decode(account["salt"])
        expected = base64.b64decode(account["password_hash"])
        iterations = int(account.get("iterations", ITERATIONS))
    except Exception:
        return False

    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return (
        email.strip().lower() == str(account.get("email", "")).lower()
        and hmac.compare_digest(actual, expected)
    )


def account_email() -> str:
    account = _read_account() or {}
    return str(account.get("email", ""))


def reset_password(email: str, new_password: str) -> None:
    """Replace the local account password without ever storing plaintext credentials."""
    account = _read_account()
    email = email.strip().lower()
    if not account or email != str(account.get("email", "")).strip().lower():
        raise ValueError("No local account matches that email address.")
    if len(new_password) < 8:
        raise ValueError("Password must be at least 8 characters.")

    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", new_password.encode("utf-8"), salt, ITERATIONS
    )
    record = {
        "email": email,
        "salt": base64.b64encode(salt).decode("ascii"),
        "password_hash": base64.b64encode(digest).decode("ascii"),
        "iterations": ITERATIONS,
    }
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps(record, indent=2), encoding="utf-8")


def update_account_email(new_email: str) -> None:
    account = _read_account()
    new_email = str(new_email or "").strip().lower()
    if not account:
        raise ValueError("No local account exists.")
    if not new_email or "@" not in new_email:
        raise ValueError("Enter a valid email address.")
    account["email"] = new_email
    AUTH_FILE.write_text(json.dumps(account, indent=2), encoding="utf-8")
