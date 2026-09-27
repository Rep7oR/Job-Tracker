"""Local account authentication and session handling.

Accounts are stored as a single JSON file under ``data/accounts.json``
(email -> salted/hashed password + metadata). Passwords are hashed with
stdlib ``hashlib.pbkdf2_hmac`` — no third-party crypto dependency needed for
phase 1. Session state (who is currently signed in) lives in
``st.session_state``, scoped to the running Streamlit session.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import streamlit as st

from services.paths import DATA_DIR

ACCOUNTS_FILE: Path = DATA_DIR / "accounts.json"

_PBKDF2_ALGORITHM = "sha256"
_PBKDF2_ITERATIONS = 260_000
_SALT_BYTES = 16

_MIN_PASSWORD_LENGTH = 8
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_SESSION_KEY = "auth_user_email"

_lock = threading.Lock()


def _load_accounts() -> dict:
    if not ACCOUNTS_FILE.exists():
        return {}
    try:
        with ACCOUNTS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_accounts(accounts: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = ACCOUNTS_FILE.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2, sort_keys=True)
    os.replace(tmp_path, ACCOUNTS_FILE)


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        _PBKDF2_ALGORITHM, password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    ).hex()


def account_exists(email: str) -> bool:
    return _normalize_email(email) in _load_accounts()


def create_account(email: str, password: str) -> None:
    """Create a new local account. Raises ValueError on invalid input."""
    email = _normalize_email(email)
    if not _EMAIL_RE.match(email):
        raise ValueError("Enter a valid email address.")
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {_MIN_PASSWORD_LENGTH} characters.")

    with _lock:
        accounts = _load_accounts()
        if email in accounts:
            raise ValueError("An account with that email already exists.")

        salt = os.urandom(_SALT_BYTES)
        accounts[email] = {
            "salt": salt.hex(),
            "hash": _hash_password(password, salt),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_accounts(accounts)


def authenticate(email: str, password: str) -> bool:
    """Return True if the email/password combination is valid."""
    email = _normalize_email(email)
    accounts = _load_accounts()
    record = accounts.get(email)
    if not record:
        return False
    try:
        salt = bytes.fromhex(record["salt"])
        expected = record["hash"]
    except (KeyError, ValueError):
        return False
    return hmac.compare_digest(_hash_password(password, salt), expected)


def login(email: str) -> None:
    """Mark the given (already-authenticated) email as signed in for this session."""
    st.session_state[_SESSION_KEY] = _normalize_email(email)


def logout() -> None:
    st.session_state.pop(_SESSION_KEY, None)


def current_user() -> Optional[str]:
    """Return the signed-in account's email, or None if logged out."""
    return st.session_state.get(_SESSION_KEY)


def is_authenticated() -> bool:
    return current_user() is not None
