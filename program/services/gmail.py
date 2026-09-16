from __future__ import annotations

import base64
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from services.app_paths import BASE_DIR as _PACKAGED_BASE_DIR

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

_DEV_BASE = Path(__file__).resolve().parents[1]
BASE_DIR = _PACKAGED_BASE_DIR if _PACKAGED_BASE_DIR else _DEV_BASE
TOKEN_FILE = BASE_DIR / "data/gmail_token.json"
OAUTH_CONFIG_FILE = BASE_DIR / "config/google_oauth.json"

def gmail_configured() -> bool:
    if OAUTH_CONFIG_FILE.exists():
        try:
            data = json.loads(OAUTH_CONFIG_FILE.read_text(encoding="utf-8"))
            installed = data.get("installed", data.get("web", {}))
            return bool(str(installed.get("client_id", "")).strip() and str(installed.get("client_secret", "")).strip())
        except Exception:
            pass
    return bool(os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip() and os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip())

def _oauth_client_config() -> dict:
    if OAUTH_CONFIG_FILE.exists():
        try:
            data = json.loads(OAUTH_CONFIG_FILE.read_text(encoding="utf-8"))
            installed = data.get("installed", data.get("web", {}))
            client_id = str(installed.get("client_id", "")).strip()
            client_secret = str(installed.get("client_secret", "")).strip()
            if client_id and client_secret:
                return {"installed": {"client_id": client_id, "client_secret": client_secret, "auth_uri": installed.get("auth_uri", "https://accounts.google.com/o/oauth2/auth"), "token_uri": installed.get("token_uri", "https://oauth2.googleapis.com/token"), "redirect_uris": installed.get("redirect_uris", ["http://localhost"])}}
        except Exception:
            pass
    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError("Google sign-in is currently unavailable. Please try again later.")
    return {"installed": {"client_id": client_id, "client_secret": client_secret, "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"]}}

def _save_credentials(creds) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")


def _load_saved_credentials():
    from google.oauth2.credentials import Credentials

    if not TOKEN_FILE.exists():
        return None

    try:
        return Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    except Exception:
        try:
            TOKEN_FILE.unlink()
        except Exception:
            pass
        return None


def get_gmail_service():
    """Authenticate the local JobSync app with Google and return read-only Gmail API access.

    The user-facing flow is intentionally simple:
      1. Click Connect Gmail.
      2. Google's login/consent page opens in the browser.
      3. Google redirects to a temporary localhost callback.
      4. The token is stored locally so the user stays connected until they disconnect.

    The OAuth client configuration is application-level and never shown in the Gmail UI.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)

    creds = _load_saved_credentials()

    # Reuse an existing connection whenever possible.
    if creds:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                _save_credentials(creds)
            except Exception:
                creds = None

        if creds and creds.valid:
            return build("gmail", "v1", credentials=creds, cache_discovery=False)

    # No usable local token: start Google's native installed-app OAuth flow.
    oauth_config = _oauth_client_config()
    flow = InstalledAppFlow.from_client_config(oauth_config, SCOPES)

    # 127.0.0.1 avoids localhost IPv4/IPv6 resolution differences on Windows.
    # Port 0 lets the OS choose a free callback port.
    creds = flow.run_local_server(
        host="127.0.0.1",
        bind_addr="127.0.0.1",
        port=0,
        open_browser=True,
        authorization_prompt_message="Opening Google login in your browser…",
        success_message="Gmail is connected. You can return to JobSync.",
        access_type="offline",
        prompt="consent",
    )

    if not creds or not creds.valid:
        raise RuntimeError("Google authorization finished without a valid Gmail token.")

    _save_credentials(creds)

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def disconnect_gmail() -> None:
    """Remove only the local Gmail OAuth token. Does not change Gmail."""
    try:
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
    except Exception:
        pass


def _header(headers: list[dict], name: str) -> str:
    wanted = name.lower()
    for h in headers or []:
        if str(h.get("name", "")).lower() == wanted:
            return str(h.get("value", ""))
    return ""


def _decode_body(data: str) -> str:
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _payload_text(payload: dict) -> str:
    parts: list[str] = []
    body = payload.get("body") or {}
    if body.get("data"):
        parts.append(_decode_body(body["data"]))
    for part in payload.get("parts") or []:
        parts.append(_payload_text(part))

    text = "\n".join(parts)
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


STATUS_RULES = [
    ("Interview", [
        r"\binterview\b", r"\bvideo interview\b", r"\bphone interview\b",
        r"\binterview invitation\b", r"\binvited to (an|a) interview\b",
    ]),
    ("Offer", [
        r"\bjob offer\b", r"\boffer letter\b", r"\boffer of employment\b",
        r"\bwe are pleased to offer\b",
    ]),
    ("Rejected", [
        r"\bunfortunately\b.{0,120}\bapplication\b",
        r"\bnot (?:be )?moving forward\b",
        r"\bdecided not to (?:move forward|proceed)\b",
        r"\bnot been selected\b", r"\bapplication (?:was )?unsuccessful\b",
        r"\brejection\b",
    ]),
    ("Application received", [
        r"\bthank you for (?:your )?application\b",
        r"\bapplication (?:has been|was) received\b",
        r"\bwe have received your application\b",
        r"\bapplication confirmation\b",
    ]),
    ("Assessment", [
        r"\bassessment\b", r"\btest invitation\b", r"\bcoding test\b",
        r"\bonline assessment\b",
    ]),
]


def classify_email(subject: str, body: str) -> str:
    text = f"{subject}\n{body}".lower()
    for status, patterns in STATUS_RULES:
        for pattern in patterns:
            if re.search(pattern, text, flags=re.IGNORECASE):
                return status
    return "Recruitment update"


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[A-Za-zÄÖÜäöüß]{3,}", (text or "").lower())
    stop = {
        "the","and","for","with","from","your","you","our","are","this","that",
        "application","position","role","career","jobs","job","team","thank",
        "have","been","will","please","about","into","their","they","company",
    }
    return {w for w in words if w not in stop}


def match_application(email: dict, applications: list[dict]) -> tuple[int | None, float]:
    subject = email.get("subject", "")
    body = email.get("body", "")
    email_tokens = _tokenize(f"{subject} {body}")
    best_idx, best_score = None, 0.0

    for idx, app in enumerate(applications):
        company = _tokenize(app.get("company", ""))
        title = _tokenize(app.get("title", ""))
        score = 0.0
        if company:
            score += 0.65 * (len(company & email_tokens) / max(1, len(company)))
        if title:
            score += 0.35 * (len(title & email_tokens) / max(1, min(4, len(title))))

        sender = (email.get("sender") or "").lower()
        company_text = (app.get("company") or "").lower().replace(" ", "")
        if company_text and company_text in sender:
            score = min(1.0, score + 0.25)

        if score > best_score:
            best_idx, best_score = idx, score

    return best_idx, round(best_score, 2)


def sync_gmail(applications: list[dict], days: int = 30, max_messages: int = 50) -> list[dict]:
    """Read recent recruitment-related emails and return local update candidates."""
    service = get_gmail_service()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y/%m/%d")
    query = (
        f"after:{since} (application OR interview OR recruitment OR recruiter "
        f"OR candidate OR assessment OR offer)"
    )

    response = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=max_messages,
        includeSpamTrash=False,
    ).execute()

    results: list[dict] = []
    for meta in response.get("messages", []):
        message = service.users().messages().get(
            userId="me", id=meta["id"], format="full"
        ).execute()

        payload = message.get("payload", {})
        headers = payload.get("headers", [])
        subject = _header(headers, "Subject")
        sender = _header(headers, "From")
        date_raw = _header(headers, "Date")
        body = _payload_text(payload)

        try:
            dt = parsedate_to_datetime(date_raw).astimezone()
            received_at = dt.isoformat(timespec="seconds")
        except Exception:
            received_at = date_raw

        if not subject and not body:
            continue

        email = {
            "id": message.get("id", ""),
            "thread_id": message.get("threadId", ""),
            "subject": subject,
            "sender": sender,
            "received_at": received_at,
            "status": classify_email(subject, body),
            "body": body[:8000],
        }

        app_idx, score = match_application(email, applications)
        email["matched_application_index"] = app_idx
        email["match_confidence"] = score
        results.append(email)

    return results
