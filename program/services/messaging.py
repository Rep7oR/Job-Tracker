from __future__ import annotations

import mimetypes
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import requests

# Reuses the same Supabase project as presence.py. Publishable key is safe to
# ship in the desktop client — never put the secret/service-role key here.
SUPABASE_URL = (
    os.getenv("JOBSYNC_SUPABASE_REST_URL", "https://bldrwjsgrpbyiaowkpqs.supabase.co/rest/v1")
    .strip()
    .rstrip("/")
    .removesuffix("/rest/v1")
)
SUPABASE_REST_URL = f"{SUPABASE_URL}/rest/v1"
SUPABASE_STORAGE_URL = f"{SUPABASE_URL}/storage/v1"
SUPABASE_PUBLISHABLE_KEY = os.getenv(
    "JOBSYNC_SUPABASE_PUBLISHABLE_KEY",
    "sb_publishable_WshU8zZeKxnGWxJLjVYSHA_FZhvQHW-",
).strip()

MESSAGES_TABLE = "jobsync_messages"
ATTACHMENTS_BUCKET = "jobsync-attachments"
TIMEOUT = 12
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024  # 15 MB
ALLOWED_ATTACHMENT_TYPES = {"pdf", "jpg", "jpeg", "png"}

URL_PATTERN = re.compile(r"(https?://[^\s<>\"]+)", re.IGNORECASE)


def configured() -> bool:
    return bool(SUPABASE_REST_URL and SUPABASE_PUBLISHABLE_KEY)


def _headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_PUBLISHABLE_KEY,
        "Authorization": f"Bearer {SUPABASE_PUBLISHABLE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _thread_id(email_a: str, email_b: str) -> str:
    """Stable, order-independent id for a 1:1 conversation between two accounts."""
    a, b = sorted([email_a.strip().lower(), email_b.strip().lower()])
    return f"{a}|{b}"


def linkify(text: str) -> str:
    """Wrap bare URLs in an anchor tag. Caller must otherwise html.escape the text first."""
    return URL_PATTERN.sub(r'<a href="\1" target="_blank" rel="noopener">\1</a>', text)


def send_message(
    *,
    sender_email: str,
    sender_name: str,
    recipient_email: str,
    text: str = "",
    attachment_url: str = "",
    attachment_name: str = "",
    attachment_type: str = "",
) -> bool:
    if not configured() or not sender_email or not recipient_email:
        return False
    payload = {
        "thread_id": _thread_id(sender_email, recipient_email),
        "sender_email": sender_email.strip().lower()[:240],
        "sender_name": (sender_name or "User")[:120],
        "recipient_email": recipient_email.strip().lower()[:240],
        "body": (text or "")[:4000],
        "attachment_url": (attachment_url or "")[:2000],
        "attachment_name": (attachment_name or "")[:200],
        "attachment_type": (attachment_type or "")[:20],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    response = requests.post(
        f"{SUPABASE_REST_URL}/{MESSAGES_TABLE}",
        headers={**_headers(), "Prefer": "return=minimal"},
        json=payload,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return True


def fetch_thread(email_a: str, email_b: str, limit: int = 300) -> list[dict[str, Any]]:
    if not configured() or not email_a or not email_b:
        return []
    response = requests.get(
        f"{SUPABASE_REST_URL}/{MESSAGES_TABLE}",
        params={
            "select": "sender_email,sender_name,recipient_email,body,attachment_url,attachment_name,attachment_type,created_at",
            "thread_id": f"eq.{_thread_id(email_a, email_b)}",
            "order": "created_at.asc",
            "limit": str(limit),
        },
        headers={**_headers(), "Cache-Control": "no-cache", "Pragma": "no-cache"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def fetch_conversations(my_email: str, limit: int = 500) -> list[dict[str, Any]]:
    """Recent messages involving this account, newest first — group client-side into threads."""
    if not configured() or not my_email:
        return []
    email = my_email.strip().lower()
    response = requests.get(
        f"{SUPABASE_REST_URL}/{MESSAGES_TABLE}",
        params={
            "select": "sender_email,sender_name,recipient_email,body,attachment_name,created_at",
            "or": f"(sender_email.eq.{email},recipient_email.eq.{email})",
            "order": "created_at.desc",
            "limit": str(limit),
        },
        headers={**_headers(), "Cache-Control": "no-cache", "Pragma": "no-cache"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        return []

    threads: dict[str, dict[str, Any]] = {}
    for row in rows:
        sender = str(row.get("sender_email") or "").strip().lower()
        recipient = str(row.get("recipient_email") or "").strip().lower()
        other = recipient if sender == email else sender
        if not other or other in threads:
            continue
        preview = str(row.get("body") or "").strip() or (
            f"📎 {row.get('attachment_name')}" if row.get("attachment_name") else ""
        )
        threads[other] = {
            "other_email": other,
            "other_name": str(row.get("sender_name") or other) if sender != email else other,
            "preview": preview[:140],
            "created_at": row.get("created_at") or "",
        }
    return list(threads.values())


def upload_attachment(file_bytes: bytes, filename: str, content_type: str = "") -> tuple[str, str]:
    """Upload to the shared Supabase Storage bucket. Returns (public_url, error)."""
    if not configured():
        return "", "Messaging is not configured."
    if len(file_bytes) > MAX_ATTACHMENT_BYTES:
        return "", "File is larger than 15 MB."
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "bin").lower()
    if ext not in ALLOWED_ATTACHMENT_TYPES:
        return "", "Only PDF, JPG and PNG files can be shared."
    guessed_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    object_path = f"{uuid.uuid4().hex}.{ext}"
    response = requests.post(
        f"{SUPABASE_STORAGE_URL}/object/{ATTACHMENTS_BUCKET}/{object_path}",
        headers={
            "apikey": SUPABASE_PUBLISHABLE_KEY,
            "Authorization": f"Bearer {SUPABASE_PUBLISHABLE_KEY}",
            "Content-Type": guessed_type,
        },
        data=file_bytes,
        timeout=TIMEOUT,
    )
    if response.status_code >= 400:
        return "", f"Upload failed: {response.text[:200]}"
    public_url = f"{SUPABASE_STORAGE_URL}/object/public/{ATTACHMENTS_BUCKET}/{object_path}"
    return public_url, ""
