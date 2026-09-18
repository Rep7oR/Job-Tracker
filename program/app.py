from __future__ import annotations

import html
import hashlib
import base64
import hmac
import uuid
import inspect
import json
import os
import re
import shutil
import secrets
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
import time
import subprocess
from urllib.parse import urlencode

import requests
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

from services.cv_engine import (
    build_reference_context,
    extract_text,
    build_external_ai_prompt,
    load_ai_cv_generation_prompt,
    extract_latex_code,
    validate_external_latex,
    render_cv_from_blueprint,
    _json_from_output,
)
from services.excel_export import export_applied_jobs_xlsx
from services.jobs import ACTOR_CATALOG, ACTOR_ID_TO_NAME, DEFAULT_ACTOR_NAMES, search_jobs
from services.storage import (
    DEFAULT_STATE,
    load_state,
    save_state,
    set_active_user,
    delete_user_state,
    user_state_exists,
)
from services.gmail import get_gmail_service, disconnect_gmail, sync_gmail
from services.linkedin_browser import sync_linkedin_notifications, connect_linkedin
from services.free_job_sources import FREE_SOURCE_NAMES
from services.notifications import desktop_notify
from services.presence import (
    configured as presence_configured,
    heartbeat_presence,
    list_online_users,
)
PROGRAM_DIR = Path(__file__).resolve().parent
PACKAGE_DIR = PROGRAM_DIR.parent.resolve()

# Single source of truth for the application version. The release script updates
# VERSION.txt and UPDATE_VERSION.json automatically before packaging a release.
def _read_app_version() -> str:
    for version_file in (PACKAGE_DIR / "VERSION.txt", PACKAGE_DIR / "UPDATE_VERSION.json"):
        try:
            raw = version_file.read_text(encoding="utf-8-sig").strip()
            if version_file.name.endswith(".json"):
                value = json.loads(raw).get("version", "")
            else:
                value = raw
            match = re.search(r"(?<!\d)(\d+\.\d+(?:\.\d+)?)(?!\d)", str(value))
            if match:
                return match.group(1)
        except Exception:
            pass
    return "0.0.0"

APP_VERSION = _read_app_version()

# Resolve the persistent data root. In the packaged app the launcher sets
# JOBSYNC_DATA_DIR (or PyInstaller freezes us) so all user data lives beside the
# .exe instead of the temporary extraction folder. In development the project
# root is used, matching the original behaviour.
from services.app_paths import BASE_DIR as _PACKAGED_BASE_DIR
if _PACKAGED_BASE_DIR:
    BASE_DIR = _PACKAGED_BASE_DIR
else:
    # Resolve the project from the actual app.py location. JOBSYNC_ROOT is
    # accepted only when it points back to this exact application, preventing
    # stale startup variables from redirecting to an older installation.
    _env_root_raw = os.getenv("JOBSYNC_ROOT", "").strip()
    _env_root = Path(_env_root_raw).expanduser().resolve() if _env_root_raw else None
    if _env_root and (_env_root / "program" / "app.py").resolve() == Path(__file__).resolve():
        BASE_DIR = _env_root
    else:
        BASE_DIR = PACKAGE_DIR
ENV_FILE = BASE_DIR / ".env"
UPLOAD_CV = BASE_DIR / "uploads" / "cv"
UPLOAD_CL = BASE_DIR / "uploads" / "coverletters"
UPLOAD_REFERENCES = BASE_DIR / "uploads" / "references"
OUTPUT_CV = BASE_DIR / "output" / "cv"
CV_LIBRARY_DIR = BASE_DIR / "output" / "cv_library"
CV_BACKUP_DIR = BASE_DIR / "output" / "backups" / "cv_library"
OUTPUT_CL = BASE_DIR / "output" / "coverletters"
USER_BLUEPRINT_DIR = BASE_DIR / "user_blueprints"
CV_BASE_TEMPLATE_PATH = USER_BLUEPRINT_DIR / "cv_base.tex"
COVER_LETTER_BASE_TEMPLATE_PATH = USER_BLUEPRINT_DIR / "cover_letter_base.tex"
TRACKER = BASE_DIR / "output" / "applied_jobs.xlsx"
OAUTH_CONFIG_FILE = BASE_DIR / "config" / "google_oauth.json"

# Contact links shown on Home. Replace with the final community/support links when ready.
WHATSAPP_URL = os.getenv("JOBSYNC_WHATSAPP_URL", "https://wa.me/")
DISCORD_URL = os.getenv("JOBSYNC_DISCORD_URL", "https://discord.com/")

for folder in (UPLOAD_CV, UPLOAD_CL, UPLOAD_REFERENCES, OUTPUT_CV, OUTPUT_CL, USER_BLUEPRINT_DIR, CV_LIBRARY_DIR, CV_BACKUP_DIR):
    folder.mkdir(parents=True, exist_ok=True)


# ── Local JobSync accounts ───────────────────────────────────────────────
# Account credentials and user workspaces stay on this computer.
# Accounts, passwords, profiles, and application data stay local.
LOCAL_ACCOUNTS_FILE = BASE_DIR / "data" / "accounts.json"
REMEMBERED_LOGIN_FILE = BASE_DIR / "data" / "remembered_login.json"
AUTH_SESSION_SECONDS = 3 * 60 * 60
REMEMBERED_LOGIN_SECONDS = 30 * 24 * 60 * 60
ACCOUNT_RECOVERY_CODE_LENGTH = 12


def _load_local_accounts() -> dict:
    try:
        if LOCAL_ACCOUNTS_FILE.exists():
            raw = json.loads(LOCAL_ACCOUNTS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except Exception:
        pass
    return {}


def _save_local_accounts(accounts: dict) -> None:
    LOCAL_ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_ACCOUNTS_FILE.write_text(
        json.dumps(accounts, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, 200_000
    )
    return f"pbkdf2_sha256$200000${salt.hex()}${digest.hex()}"


def _password_matches(password: str, stored: str) -> bool:
    try:
        scheme, rounds, salt_hex, digest_hex = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(rounds),
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def _clear_remembered_login() -> None:
    """Revoke the local remembered-login token on this device."""
    try:
        if REMEMBERED_LOGIN_FILE.exists():
            REMEMBERED_LOGIN_FILE.unlink()
    except Exception:
        pass


def _remember_local_login(user_id: str, email: str) -> None:
    """Create a revocable, non-password persistent login token.

    The actual password is never stored for this feature. Only a random token
    is stored locally, with its hash recorded in the local account database.
    """
    token = secrets.token_urlsafe(48)
    accounts = _load_local_accounts()
    account = accounts.get(email.strip().lower())
    if not account:
        return
    account["remember_token_hash"] = hashlib.sha256(token.encode("utf-8")).hexdigest()
    account["remember_token_created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _save_local_accounts(accounts)
    REMEMBERED_LOGIN_FILE.parent.mkdir(parents=True, exist_ok=True)
    REMEMBERED_LOGIN_FILE.write_text(
        json.dumps({
            "user_id": user_id,
            "email": email.strip().lower(),
            "token": token,
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=REMEMBERED_LOGIN_SECONDS)).isoformat(timespec="seconds"),
        }, indent=2),
        encoding="utf-8",
    )


def _restore_remembered_login() -> tuple[str, str] | None:
    """Restore a valid remembered login, if one exists on this computer."""
    try:
        if not REMEMBERED_LOGIN_FILE.exists():
            return None
        saved = json.loads(REMEMBERED_LOGIN_FILE.read_text(encoding="utf-8"))
        email = str(saved.get("email") or "").strip().lower()
        token = str(saved.get("token") or "")
        expires_raw = str(saved.get("expires_at") or "")
        if not email or not token or not expires_raw:
            _clear_remembered_login()
            return None
        expires_at = datetime.fromisoformat(expires_raw)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= expires_at:
            _clear_remembered_login()
            return None
        account = _load_local_accounts().get(email)
        expected = str(account.get("remember_token_hash") or "") if account else ""
        actual = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not expected or not hmac.compare_digest(actual, expected):
            _clear_remembered_login()
            return None
        user_id = str(account.get("user_id") or saved.get("user_id") or "")
        if not user_id:
            _clear_remembered_login()
            return None
        return user_id, email
    except Exception:
        _clear_remembered_login()
        return None


def _local_user_id(email: str) -> str:
    # Stable local ID for this account.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"jobsync-local:{email.lower().strip()}"))


def _new_recovery_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(ACCOUNT_RECOVERY_CODE_LENGTH))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def _local_sign_up(email: str, password: str) -> tuple[str, str]:
    email = email.strip().lower()
    if not email or "@" not in email:
        raise ValueError("Enter a valid email address.")
    if len(password) < 6:
        raise ValueError("Password must be at least 6 characters.")

    accounts = _load_local_accounts()
    if email in accounts:
        raise ValueError("An account with this email already exists. Please log in.")

    user_id = _local_user_id(email)
    recovery_code = _new_recovery_code()
    accounts[email] = {
        "user_id": user_id,
        "email": email,
        "password": _password_hash(password),
        "recovery_code_hash": hashlib.sha256(recovery_code.replace("-", "").encode("utf-8")).hexdigest(),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save_local_accounts(accounts)
    return user_id, recovery_code


def _local_sign_in(email: str, password: str) -> tuple[str, str]:
    email = email.strip().lower()
    accounts = _load_local_accounts()
    account = accounts.get(email)
    if not account or not _password_matches(password, str(account.get("password") or "")):
        raise ValueError("Incorrect email or password.")
    return str(account["user_id"]), str(account.get("email") or email)




def _local_change_password(email: str, current_password: str, new_password: str) -> None:
    email = email.strip().lower()
    if len(new_password) < 6:
        raise ValueError("New password must be at least 6 characters.")
    accounts = _load_local_accounts()
    account = accounts.get(email)
    if not account or not _password_matches(current_password, str(account.get("password") or "")):
        raise ValueError("Current password is incorrect.")
    account["password"] = _password_hash(new_password)
    account.pop("remember_token_hash", None)
    account.pop("remember_token_created_at", None)
    _save_local_accounts(accounts)
    _clear_remembered_login()


def _local_reset_password(email: str, recovery_code: str, new_password: str) -> None:
    email = email.strip().lower()
    normalized = re.sub(r"[^A-Za-z0-9]", "", recovery_code or "").upper()
    if len(new_password) < 6:
        raise ValueError("New password must be at least 6 characters.")
    accounts = _load_local_accounts()
    account = accounts.get(email)
    expected = str(account.get("recovery_code_hash") or "") if account else ""
    actual = hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""
    if not account or not expected or not hmac.compare_digest(actual, expected):
        raise ValueError("The email or recovery code is incorrect.")
    account["password"] = _password_hash(new_password)
    account.pop("remember_token_hash", None)
    account.pop("remember_token_created_at", None)
    new_recovery = _new_recovery_code()
    account["recovery_code_hash"] = hashlib.sha256(new_recovery.replace("-", "").encode("utf-8")).hexdigest()
    _save_local_accounts(accounts)
    _clear_remembered_login()


def _ensure_recovery_code(email: str) -> str:
    email = email.strip().lower()
    accounts = _load_local_accounts()
    account = accounts.get(email)
    if not account:
        raise ValueError("Local account not found.")
    if account.get("recovery_code_hash"):
        return ""
    code = _new_recovery_code()
    account["recovery_code_hash"] = hashlib.sha256(code.replace("-", "").encode("utf-8")).hexdigest()
    _save_local_accounts(accounts)
    return code

def _github_update_state_path() -> Path:
    """Return the per-user writable location used by the GitHub updater."""
    # JobSync is installed under Program Files, so updater state must never be
    # written beside the bundled updater. Keep it in the user's LocalAppData.
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        root = Path(local_app_data)
    else:
        root = Path.home() / "AppData" / "Local"
    state_dir = root / "JobSync" / "update-state"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return state_dir / "last-update-check.json"


def _find_github_updater_root() -> Path | None:
    """Locate the bundled updater relative to the running application."""
    candidates = []
    for root in (PROGRAM_DIR.parent, BASE_DIR):
        root = Path(root).resolve()
        if root not in candidates:
            candidates.append(root)
    for root in PROGRAM_DIR.resolve().parents:
        root = Path(root)
        if root not in candidates:
            candidates.append(root)
        if len(candidates) >= 6:
            break
    for root in candidates:
        updater_dir = root / "github"
        if (updater_dir / "updater.ps1").is_file() and (updater_dir / "update-config.json").is_file():
            return updater_dir
    return None

# Legacy compatibility marker only. JobSync never reads from or creates this

load_dotenv(ENV_FILE, override=True)

st.set_page_config(
    page_title=f"Job Tracker v{APP_VERSION}",
    page_icon=str(PACKAGE_DIR / "tools" / "JobSync.ico"),
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    :root {
        --jf-bg:#050505;
        --jf-surface:#0d0f12;
        --jf-card:#111418;
        --jf-card-2:#15181d;
        --jf-text:#f5f7fa;
        --jf-muted:#a7afb9;
        --jf-border:#252a31;
        --jf-red:#ef4444;
        --jf-red-hover:#dc2626;
        --jf-green:#22c55e;
        --jf-green-hover:#16a34a;
        --jf-yellow:#f59e0b;
    }

    html, body, [data-testid="stAppViewContainer"], .stApp {
        background:#050505 !important;
        color:var(--jf-text) !important;
    }
    .stApp { background:var(--jf-bg) !important; }
    .block-container { width:100% !important; max-width:none !important; box-sizing:border-box !important; padding-top:.05rem !important; padding-bottom:3rem; padding-left:clamp(.75rem,2vw,2.5rem) !important; padding-right:clamp(.75rem,2vw,2.5rem) !important; }

    /* Hide Streamlit chrome (Deploy/menu/header) so JobSync controls the top bar. */
    header[data-testid="stHeader"],
    div[data-testid="stToolbar"],
    div[data-testid="stDecoration"],
    div[data-testid="stAppDeployButton"],
    .stAppDeployButton,
    .stDeployButton,
    #MainMenu,
    footer { display:none !important; visibility:hidden !important; height:0 !important; }
    div[data-testid="stAppViewContainer"] > .main { padding-top:0 !important; }

    /* JobSync owns sidebar visibility; do not depend on Streamlit's native collapsed-sidebar control. */
    /* Streamlit's native collapse button is hidden. JobSync uses its own
       deterministic toggle so the reopen control can never disappear. */
    div[data-testid="stSidebarCollapsedControl"],
    div[data-testid*="SidebarCollapsedControl"],
    button[aria-label="Close sidebar"],
    button[aria-label="Open sidebar"],
    button[data-testid="stBaseButton-headerNoPadding"] {
        display:none !important;
        visibility:hidden !important;
    }

    /* Sidebar */
    section[data-testid="stSidebar"] {
        background:#0a0b0d !important;
        border-right:1px solid #1d2127;
        box-shadow:8px 0 28px rgba(0,0,0,.14);
    }
    section[data-testid="stSidebar"] * { color:#f5f7fa !important; }
    section[data-testid="stSidebar"] .stButton { margin:.08rem 0 !important; }
    section[data-testid="stSidebar"] .stButton button {
        background:rgba(18,21,26,.86) !important;
        color:#f5f7fa !important;
        border:1px solid #262c34 !important;
        position:relative !important;
        overflow:hidden !important;
        transition:transform .16s ease, background .16s ease, border-color .16s ease, box-shadow .16s ease !important;
    }
    section[data-testid="stSidebar"] .stButton button:hover {
        background:linear-gradient(90deg,rgba(34,197,94,.13),rgba(18,21,26,.95)) !important;
        border-color:rgba(34,197,94,.62) !important;
        box-shadow:inset 3px 0 0 #22c55e, 0 8px 22px rgba(34,197,94,.10) !important;
        transform:translateX(2px) !important;
    }
    section[data-testid="stSidebar"] .stButton button[kind="primary"] {
        background:linear-gradient(90deg,rgba(239,68,68,.20),rgba(18,21,26,.95)) !important;
        border-color:rgba(239,68,68,.52) !important;
        box-shadow:inset 3px 0 0 #ef4444, 0 7px 20px rgba(239,68,68,.09) !important;
    }
    section[data-testid="stSidebar"] .stButton button[kind="primary"]:hover {
        background:linear-gradient(90deg,rgba(239,68,68,.27),rgba(34,197,94,.08)) !important;
        border-color:#22c55e !important;
        box-shadow:inset 3px 0 0 #22c55e, 0 9px 24px rgba(34,197,94,.12) !important;
    }

    .sidebar-userbar { display:flex; align-items:center; gap:10px; padding:.75rem .65rem; border:1px solid #232832; border-radius:14px; background:linear-gradient(135deg,rgba(239,68,68,.08),rgba(34,197,94,.05)); }
    .sidebar-usercopy { min-width:0; }
    .sidebar-username { font-weight:800; font-size:.82rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .sidebar-useremail { color:#8e99a7; font-size:.64rem; margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .sidebar-role { margin-top:5px; }
    .sidebar-footnote { color:#65707e; font-size:.57rem; letter-spacing:.08em; line-height:1.5; margin-top:.7rem; text-align:center; }
    /* Animated JobSync identity: CSS/SVG so it stays crisp and lightweight. */
    .jobsync-logo-mark {
        position:relative; display:inline-grid; place-items:center; width:42px; height:42px; flex:0 0 42px;
        border-radius:14px; background:linear-gradient(145deg,#0c1728,#171033);
        border:1px solid rgba(115,224,255,.24); overflow:visible;
        box-shadow:0 10px 28px rgba(74,93,255,.20), inset 0 1px 0 rgba(255,255,255,.12);
    }
    .jobsync-logo-mark::before {
        content:""; position:absolute; inset:-3px; border-radius:16px;
        border:1px solid rgba(92,219,255,.0); animation:jobsyncLogoPulse 2.8s ease-in-out infinite;
    }
    .jobsync-logo-mark svg { width:31px; height:31px; overflow:visible; }
    .jobsync-logo-j { fill:url(#jobsyncJGradient); filter:drop-shadow(0 0 5px rgba(53,216,255,.34)); }
    .jobsync-logo-orbit { fill:none; stroke:url(#jobsyncOrbitGradient); stroke-width:2.6; stroke-linecap:round;
        stroke-dasharray:74 18; transform-origin:50% 50%; animation:jobsyncOrbit 3.4s linear infinite; }
    .jobsync-logo-dot { fill:#ef7be8; filter:drop-shadow(0 0 4px rgba(239,123,232,.75)); animation:jobsyncDot 1.7s ease-in-out infinite; }
    .jobsync-logo-case { fill:#d78bff; stroke:#24143f; stroke-width:1.2; animation:jobsyncCase 2.8s ease-in-out infinite; transform-origin:67% 62%; }
    .jobsync-logo-wordmark { font-weight:950; letter-spacing:-.055em; color:#f7f9ff; }
    .jobsync-logo-wordmark .sync { color:#35d8ff; }
    @keyframes jobsyncOrbit { to { transform:rotate(360deg); } }
    @keyframes jobsyncDot { 0%,100% { opacity:.55; transform:scale(.82); } 50% { opacity:1; transform:scale(1.15); } }
    @keyframes jobsyncCase { 0%,100% { transform:rotate(-2deg) translateY(0); } 50% { transform:rotate(3deg) translateY(-1px); } }
    @keyframes jobsyncLogoPulse { 0%,100% { opacity:0; box-shadow:0 0 0 0 rgba(53,216,255,0); } 50% { opacity:1; box-shadow:0 0 0 6px rgba(53,216,255,.055), 0 0 22px rgba(124,92,255,.12); } }
    @media (prefers-reduced-motion: reduce) {
        .jobsync-logo-mark::before, .jobsync-logo-orbit, .jobsync-logo-dot, .jobsync-logo-case { animation:none !important; }
    }
    .jobsync-brand-row { display:flex; align-items:center; gap:10px; }
    .jobsync-brand-row .brand-copy { min-width:0; }
    .jobsync-brand-row .brand-name { line-height:1; }

    /* Global text */
    .page-title, .section-title, .hero h1, .metric-value,
    .job-title, .profile-value { color:var(--jf-text) !important; }
    .page-subtitle, .quick-note, .muted, .metric-title, .metric-note,
    .brand-sub, .job-company, .profile-label { color:var(--jf-muted) !important; }

    .home-center-brand { text-align:center; padding:3.2rem 1rem 2.4rem; margin:2rem auto 2.2rem; max-width:900px; }
    .home-center-kicker { color:#ff7d84; font-size:.68rem; font-weight:900; letter-spacing:.2em; text-transform:uppercase; }
    .home-center-title { font-size:clamp(3rem,8vw,6rem); font-weight:950; letter-spacing:-.075em; line-height:.95; margin:.35rem 0 .8rem; background:linear-gradient(90deg,#f8fafc 0%,#ff6971 55%,#77e6a2 100%); -webkit-background-clip:text; background-clip:text; color:transparent; }
    .home-center-copy { color:#a6b0bd; font-size:1rem; line-height:1.7; max-width:780px; margin:0 auto; }

    /* Modern authentication */
    .jobsync-login-shell {
        width: min(100%, 760px);
        margin: clamp(2rem, 9vh, 6rem) auto 1.5rem;
        text-align: center;
    }
    .jobsync-login-brand {
        display:flex;
        align-items:center;
        justify-content:center;
        gap:13px;
        margin-bottom:2.4rem;
    }
    .jobsync-login-logo {
        width:48px;
        height:48px;
        border-radius:14px;
        display:flex;
        align-items:center;
        justify-content:center;
        background:linear-gradient(145deg,#ff5d67,#d93642);
        color:#fff;
        font-weight:950;
        font-size:15px;
        letter-spacing:-.04em;
        box-shadow:0 12px 30px rgba(255,82,94,.18);
    }
    .jobsync-login-name {
        color:#f5f7fa;
        font-size:1.25rem;
        font-weight:900;
        text-align:left;
        letter-spacing:-.025em;
    }
    .jobsync-login-tagline {
        color:#7f8997;
        font-size:.7rem;
        text-align:left;
        margin-top:2px;
    }
    .jobsync-login-heading {
        color:#f7f8fa;
        font-size:clamp(2rem,5vw,3.25rem);
        line-height:1;
        font-weight:950;
        letter-spacing:-.055em;
    }
    .jobsync-login-copy {
        color:#8f99a7;
        margin-top:.8rem;
        font-size:.95rem;
    }
    .jobsync-login-footnote {
        color:#687382;
        font-size:.72rem;
        text-align:center;
        margin-top:1.2rem;
    }
    .jobsync-landing-shell {
        margin-top:clamp(4rem,18vh,10rem);
    }

    /* v1.3.64 — public landing identity */
    .jobsync-public-landing{min-height:calc(100vh - 30px);display:flex;align-items:center;justify-content:center;position:relative;overflow:hidden;padding:18px 24px 40px;box-sizing:border-box}
    .jobsync-public-landing::before{content:"";position:absolute;width:720px;height:720px;border-radius:50%;background:radial-gradient(circle,rgba(70,210,255,.13),rgba(103,77,255,.09) 34%,rgba(225,72,211,.05) 52%,transparent 70%);filter:blur(8px);animation:landingAura 8s ease-in-out infinite alternate;pointer-events:none}
    .jobsync-public-card{width:min(980px,92vw);text-align:center;position:relative;z-index:2;padding:22px 20px 30px}
    .jobsync-big-logo{width:210px;height:210px;margin:0 auto 26px;position:relative;display:grid;place-items:center;border-radius:54px;background:radial-gradient(circle at 32% 25%,rgba(65,223,255,.26),rgba(86,64,255,.18) 38%,rgba(21,18,50,.9) 72%);border:1px solid rgba(111,215,255,.26);box-shadow:0 0 35px rgba(54,190,255,.16),0 0 90px rgba(119,77,255,.14),inset 0 1px 0 rgba(255,255,255,.14);animation:bigLogoFloat 4.2s ease-in-out infinite}
    .jobsync-big-logo::before,.jobsync-big-logo::after{content:"";position:absolute;inset:-15px;border-radius:66px;border:1px solid rgba(55,216,255,.24);animation:bigLogoRing 3.8s linear infinite}.jobsync-big-logo::after{inset:-31px;border-color:rgba(219,75,209,.14);animation-duration:6s;animation-direction:reverse}
    .jobsync-big-logo svg{width:168px;height:168px;overflow:visible;filter:drop-shadow(0 12px 22px rgba(0,0,0,.25))}.jobsync-big-logo .jobsync-logo-orbit{stroke-width:2.8;stroke-dasharray:95 22;animation:bigOrbit 2.2s linear infinite}.jobsync-big-logo .jobsync-logo-dot{animation:bigDot 1.15s ease-in-out infinite}.jobsync-big-logo .jobsync-logo-case{animation:bigCase 1.8s ease-in-out infinite}
    .jobsync-public-kicker{color:#5de3ff;font-size:.66rem;font-weight:950;letter-spacing:.28em;text-transform:uppercase}.jobsync-public-title{margin-top:10px;font-size:clamp(2.7rem,6vw,5.5rem);font-weight:950;line-height:.94;letter-spacing:-.075em;background:linear-gradient(90deg,#f8fbff 5%,#9cecff 36%,#8a73ff 65%,#ef72d8 96%);-webkit-background-clip:text;background-clip:text;color:transparent}.jobsync-public-copy{max-width:700px;margin:17px auto 0;color:#8493aa;font-size:.86rem;line-height:1.7}
    .jobsync-public-feature-row{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;max-width:760px;margin:28px auto 0}.jobsync-public-feature{padding:12px 14px;border-radius:15px;border:1px solid rgba(255,255,255,.065);background:linear-gradient(145deg,rgba(14,26,49,.72),rgba(7,14,28,.82));text-align:left;box-shadow:inset 0 1px 0 rgba(255,255,255,.035)}.jobsync-public-feature b{display:block;color:#e8f1ff;font-size:.65rem}.jobsync-public-feature span{display:block;color:#64758d;font-size:.49rem;margin-top:4px}
    @keyframes landingAura{from{transform:scale(.9) translate3d(-2%,1%,0);opacity:.65}to{transform:scale(1.08) translate3d(2%,-1%,0);opacity:1}}@keyframes bigLogoFloat{0%,100%{transform:translateY(0) rotate(-1deg)}50%{transform:translateY(-9px) rotate(1deg)}}@keyframes bigLogoRing{0%{transform:scale(.9) rotate(0);opacity:.1}45%{opacity:.8}100%{transform:scale(1.12) rotate(360deg);opacity:0}}@keyframes bigOrbit{to{transform:rotate(360deg)}}@keyframes bigDot{0%,100%{opacity:.35;transform:scale(.75)}50%{opacity:1;transform:scale(1.35)}}@keyframes bigCase{0%,100%{transform:rotate(-3deg) translateY(0)}50%{transform:rotate(4deg) translateY(-3px)}}
    @media(max-width:700px){.jobsync-public-landing{padding:10px 12px 30px}.jobsync-big-logo{width:165px;height:165px;border-radius:44px}.jobsync-big-logo svg{width:132px;height:132px}.jobsync-public-feature-row{grid-template-columns:1fr}}

    /* Minimal authenticated Home */
    .jobsync-home-minimal {
        min-height:clamp(520px,76vh,760px);
        width:100%;
        display:flex;
        flex-direction:column;
        box-sizing:border-box;
        padding:clamp(4px,1vw,14px) clamp(4px,1vw,14px) 40px;
    }
    .jobsync-home-user {
        display:flex;
        align-items:center;
        gap:12px;
        align-self:flex-start;
        max-width:min(100%,420px);
        padding:10px 14px 10px 10px;
        border:1px solid rgba(255,255,255,.065);
        border-radius:16px;
        background:rgba(14,17,22,.72);
        box-shadow:0 10px 30px rgba(0,0,0,.14);
    }
    .jobsync-home-avatar {
        width:42px;
        height:42px;
        flex:0 0 42px;
        display:flex;
        align-items:center;
        justify-content:center;
        border-radius:13px;
        background:linear-gradient(145deg,#ff6872,#e33e49);
        color:#fff;
        font-weight:900;
        font-size:.82rem;
        letter-spacing:.02em;
    }
    .jobsync-home-usercopy { min-width:0; text-align:left; }
    .jobsync-home-username {
        color:#f4f6f8;
        font-size:.88rem;
        font-weight:850;
        white-space:nowrap;
        overflow:hidden;
        text-overflow:ellipsis;
    }
    .jobsync-home-useremail {
        color:#778290;
        font-size:.68rem;
        margin-top:2px;
        white-space:nowrap;
        overflow:hidden;
        text-overflow:ellipsis;
    }
    .jobsync-home-greeting {
        margin:auto;
        text-align:center;
        transform:translateY(-5%);
        padding:1rem;
    }
    .jobsync-home-greeting-kicker {
        color:#ff6972;
        font-size:.67rem;
        font-weight:900;
        letter-spacing:.22em;
        text-transform:uppercase;
        margin-bottom:.65rem;
    }
    .jobsync-home-greeting-title {
        color:#f5f7fa;
        font-size:clamp(2.1rem,5.8vw,4.6rem);
        line-height:1.02;
        font-weight:950;
        letter-spacing:-.065em;
    }
    .jobsync-home-greeting-subtitle {
        color:#8993a1;
        font-size:clamp(.9rem,1.6vw,1rem);
        margin-top:1rem;
    }
    @media (max-width:700px) {
        .jobsync-home-minimal { min-height:clamp(480px,70vh,680px); }
        .jobsync-home-user { max-width:calc(100vw - 40px); }
        .jobsync-home-greeting { transform:none; }
    }

    /* Cards */
    .card, .job-card {
        background:var(--jf-card) !important;
        border:1px solid var(--jf-border) !important;
        color:var(--jf-text) !important;
        border-radius:18px;
        box-shadow:0 8px 24px rgba(0,0,0,.20);
    }
    .hero {
        background:linear-gradient(135deg,#14171b 0%,#0e1013 100%) !important;
        border:1px solid #272c33 !important;
        color:var(--jf-text) !important;
        border-radius:20px;
    }
    .hero p { color:#b7bec8 !important; }

    /* Inputs / select boxes / file uploads */
    div[data-testid="stAppViewContainer"] { color-scheme:dark; }
    div[data-testid="stWidgetLabel"] p,
    div[data-testid="stWidgetLabel"] span,
    label, label p, label span { color:#e9edf2 !important; }
    input, textarea,
    div[data-baseweb="input"] input,
    div[data-baseweb="textarea"] textarea,
    div[data-baseweb="select"] > div,
    div[data-baseweb="select"] input,
    div[data-baseweb="select"] span {
        color:#f5f7fa !important;
        background:#111418 !important;
        border-color:#303640 !important;
    }
    div[data-baseweb="input"],
    div[data-baseweb="textarea"],
    div[data-baseweb="select"] > div {
        border-color:#303640 !important;
        background:#111418 !important;
    }
    div[data-baseweb="popover"], div[data-baseweb="popover"] *,
    ul[role="listbox"], ul[role="listbox"] *, li[role="option"] * {
        background:#111418 !important;
        color:#f5f7fa !important;
    }
    .stTextInput input::placeholder,
    .stTextArea textarea::placeholder { color:#737d89 !important; opacity:1; }

    /* Buttons: primary = red action, normal = green action */
    .stButton > button, .stFormSubmitButton > button,
    .stLinkButton > a, .stDownloadButton > button {
        border-radius:10px !important;
        font-weight:700 !important;
        transition:all .15s ease-in-out !important;
    }
    .stButton > button {
        background:var(--jf-green) !important;
        color:#06120a !important;
        border:1px solid var(--jf-green) !important;
    }
    .stButton > button:hover {
        background:var(--jf-green-hover) !important;
        color:#fff !important;
        border-color:var(--jf-green-hover) !important;
    }
    .stButton > button:focus:not(:active) { box-shadow:0 0 0 .15rem rgba(34,197,94,.25) !important; }
    .stButton > button[kind="primary"],
    .stFormSubmitButton > button[kind="primary"],
    button[kind="primaryFormSubmit"] {
        background:var(--jf-red) !important;
        color:#fff !important;
        border-color:var(--jf-red) !important;
    }
    .stButton > button[kind="primary"]:hover,
    .stFormSubmitButton > button[kind="primary"]:hover,
    button[kind="primaryFormSubmit"]:hover {
        background:var(--jf-red-hover) !important;
        border-color:var(--jf-red-hover) !important;
    }
    .stLinkButton > a {
        background:var(--jf-green) !important;
        color:#06120a !important;
        border:1px solid var(--jf-green) !important;
    }
    .stDownloadButton > button {
        background:#20252c !important;
        color:#f5f7fa !important;
        border:1px solid #343b45 !important;
    }

    /* Tabs, expanders, info boxes */
    [data-baseweb="tab-list"] { background:#0b0d10 !important; }
    [data-baseweb="tab"] { color:#aab2bd !important; }
    [aria-selected="true"][data-baseweb="tab"] { color:#ffffff !important; }
    div[data-testid="stExpander"] { background:#111418 !important; border:1px solid #252a31 !important; }
    div[data-testid="stAlert"] { border-radius:12px !important; }

    .brand { padding:.3rem 0 1rem; }
    .brand-name { font-size:1.45rem; font-weight:800; letter-spacing:-.02em; }
    .brand-sub { font-size:.82rem; margin-top:.15rem; }
    .page-title { font-size:2rem; font-weight:800; margin-bottom:.2rem; letter-spacing:-.03em; }
    .page-subtitle { margin-bottom:1.15rem; }
    .metric-title { font-size:.8rem; font-weight:600; }
    .metric-value { font-size:1.75rem; font-weight:800; margin-top:.15rem; }
    .metric-note { font-size:.76rem; margin-top:.2rem; }
    .section-title { font-size:1.12rem; font-weight:750; margin:.2rem 0 .7rem; }
    .job-title { font-weight:750; font-size:1.03rem; }
    .job-company { margin-top:.12rem; }
    .pill { display:inline-block; padding:3px 9px; border-radius:999px; background:#20252c; color:#d4dae2 !important; margin:4px 4px 0 0; font-size:.72rem; }
    .status-pill { display:inline-block; padding:4px 9px; border-radius:999px; font-size:.72rem; font-weight:700; }
    .status-applied { background:#3b2224; color:#ffb4b8 !important; }
    .status-interview { background:#12311f; color:#7ee2a5 !important; }
    .status-offer { background:#392b12; color:#ffd36a !important; }
    .status-rejected { background:#35171a; color:#ff9da5 !important; }
    .update-row, .profile-line { padding:.7rem 0; border-bottom:1px solid #242930; }
    .update-row:last-child, .profile-line:last-child { border-bottom:0; }
    .update-dot { width:9px; height:9px; border-radius:50%; display:inline-block; margin-right:9px; background:var(--jf-red); }
    div[data-testid="stMetric"] { background:transparent !important; }

    .hero { padding:1.65rem 1.75rem !important; min-height:150px; display:flex; flex-direction:column; justify-content:center; }
    .hero-eyebrow { color:#8f99a7; font-size:.72rem; font-weight:800; letter-spacing:.14em; margin-bottom:.35rem; }
    .hero h1 { font-size:2.55rem !important; line-height:1.08 !important; margin:.2rem 0 .65rem !important; }
    .hero p { max-width:920px; font-size:1.02rem; line-height:1.55; margin:0 !important; }
    .section-kicker { margin:1.2rem 0 .55rem; color:#7e8996; font-size:.72rem; font-weight:800; letter-spacing:.15em; }
    .metric-card { min-height:126px; padding:1.05rem 1.05rem .95rem; border:1px solid #272c33; border-radius:16px; background:#0f1216; box-shadow:0 8px 22px rgba(0,0,0,.18); }
    .metric-card.red { box-shadow:inset 0 2px 0 #ef4444, 0 8px 22px rgba(0,0,0,.18); }
    .metric-card.green { box-shadow:inset 0 2px 0 #22c55e, 0 8px 22px rgba(0,0,0,.18); }
    .metric-top { display:flex; justify-content:space-between; align-items:center; color:#a7b0bb; font-size:.78rem; font-weight:700; }
    .metric-dot { width:8px; height:8px; border-radius:50%; background:#ef4444; }
    .metric-card.green .metric-dot { background:#22c55e; }
    .action-card { min-height:158px; padding:1.1rem 1.15rem 1rem; border:1px solid #272c33; border-radius:16px 16px 0 0; background:#101317; }
    .action-icon { font-size:1.45rem; margin-bottom:.35rem; }
    .action-title { color:#f5f7fa; font-size:1.08rem; font-weight:800; margin-bottom:.3rem; }
    .action-desc { color:#a7b0bb; font-size:.88rem; line-height:1.45; min-height:52px; }
    .action-card + div button { border-radius:0 0 12px 12px !important; }
    .chart-header { padding:1rem 1.15rem .35rem; background:#0f1216; border:1px solid #272c33; border-bottom:0; border-radius:16px 16px 0 0; }
    .chart-header .chart-title { font-size:1.05rem; font-weight:800; color:#eef2f7; }
    .chart-header .chart-subtitle { margin-top:.25rem; color:#8fa0b3; font-size:.82rem; }
    .chart-card { min-height:355px; padding:1rem 1rem .8rem; background:#0f1216; border:1px solid #272c33; border-radius:16px; }
    .chart-title { color:#f4f7fb; font-weight:800; font-size:1rem; }
    .chart-subtitle { color:#788391; font-size:.78rem; margin:.2rem 0 .45rem; }
    .chart-empty { min-height:275px; display:flex; align-items:center; justify-content:center; text-align:center; color:#6f7985; padding:1.5rem; }
    .chart-foot { color:#929ba7; font-size:.78rem; text-align:center; margin-top:-.2rem; }
    .chart-foot strong { color:#f4f7fb; }
    .info-card { background:#0f1216; border:1px solid #272c33; border-radius:16px; padding:1rem 1.15rem; min-height:245px; height:100%; box-sizing:border-box; }
    .card-body { margin-top:.15rem; }
    .card-heading { color:#f5f7fa; font-weight:800; font-size:1rem; margin-bottom:.7rem; }
    .update-row { display:flex; align-items:flex-start; gap:.65rem; color:#c5ccd5; font-size:.87rem; line-height:1.45; padding:.66rem 0; border-bottom:1px solid #20252b; }
    .update-row:last-child { border-bottom:0; }
    .empty-state { min-height:150px; display:flex; align-items:center; justify-content:center; text-align:center; color:#6f7985; padding:1rem; line-height:1.5; }
    .update-dot { width:8px; height:8px; border-radius:50%; display:inline-block; margin-top:.38rem; flex:0 0 auto; }
    .profile-line { display:flex; justify-content:space-between; gap:1rem; padding:.63rem 0; border-bottom:1px solid #20252b; }
    .profile-line:last-child { border-bottom:0; }
    .profile-label { color:#7f8995 !important; font-size:.79rem; }
    .profile-value { color:#edf1f5 !important; font-size:.85rem; font-weight:650; text-align:right; }
    .jobs-panel { background:#0f1216; border:1px solid #272c33; border-radius:16px; padding:.55rem 1rem 1rem; }
    .table-head { color:#737d89; font-size:.68rem; font-weight:800; letter-spacing:.08em; padding:.65rem 0 .55rem; }
    .job-row-title { color:#f3f6fa; font-weight:750; font-size:.88rem; padding:.65rem 0; }
    .job-row-sub { color:#9da6b1; font-size:.78rem; padding:.62rem 0; line-height:1.5; }
    .source-pill { display:inline-block; margin-top:.55rem; padding:.28rem .52rem; border-radius:999px; background:#19201a; color:#87e0a7 !important; font-size:.68rem; font-weight:800; }
    .template-summary { background:#0f1216; border:1px solid #272c33; border-radius:16px; padding:1rem; margin:.8rem 0 1rem; }
    .template-grid { display:grid; grid-template-columns:repeat(6,1fr); gap:.6rem; }
    .template-grid div { padding:.7rem; background:#11151a; border:1px solid #222831; border-radius:10px; }
    .template-grid span { display:block; color:#737d89; font-size:.68rem; text-transform:uppercase; letter-spacing:.06em; }
    .template-grid strong { display:block; color:#eff3f7; font-size:.82rem; margin-top:.2rem; }
    @media (max-width: 1000px) { .template-grid { grid-template-columns:repeat(3,1fr); } }
    @media (max-width: 700px) { .template-grid { grid-template-columns:repeat(2,1fr); } .hero h1{font-size:2rem !important;} }

    /* ================= MODERN UI SKIN — VISUAL ONLY =================
       No navigation, state, business logic, API, storage, or workflow changes.
    */
    :root {
        --mh-bg: #07090d;
        --mh-surface: rgba(16,20,27,.82);
        --mh-surface-2: rgba(20,25,33,.9);
        --mh-border: rgba(255,255,255,.075);
        --mh-border-strong: rgba(255,255,255,.13);
        --mh-accent: #ff4d5b;
        --mh-accent-2: #ff7a59;
        --mh-green: #39e58c;
        --mh-blue: #66a6ff;
        --mh-text: #f7f9fc;
        --mh-muted: #95a0af;
        --mh-shadow: 0 24px 70px rgba(0,0,0,.34);
    }

    /* Soft animated background glow */
    div[data-testid="stAppViewContainer"]::before {
        content:"";
        position:fixed;
        inset:-22%;
        pointer-events:none;
        z-index:0;
        background:
          radial-gradient(circle at 12% 10%, rgba(255,77,91,.11), transparent 26%),
          radial-gradient(circle at 82% 8%, rgba(102,166,255,.09), transparent 24%),
          radial-gradient(circle at 72% 88%, rgba(57,229,140,.06), transparent 26%);
        filter: blur(34px);
        animation: mhFloatGlow 16s ease-in-out infinite alternate;
    }
    @keyframes mhFloatGlow {
        from { transform: translate3d(-1%, -1%, 0) scale(1); }
        to   { transform: translate3d(1.5%, 1%, 0) scale(1.035); }
    }

    /* Make content sit above ambient glow */
    div[data-testid="stAppViewContainer"] > .main,
    section[data-testid="stSidebar"] { position:relative; z-index:1; }

    /* Sidebar glass / depth */
    section[data-testid="stSidebar"] {
        background:
          linear-gradient(180deg, rgba(9,11,16,.97), rgba(6,8,12,.94)) !important;
        border-right:1px solid rgba(255,255,255,.07) !important;
        box-shadow: 18px 0 45px rgba(0,0,0,.24);
        backdrop-filter: blur(18px);
    }
    section[data-testid="stSidebar"] > div:first-child {
        padding-top:.65rem !important;
    }
    .brand {
        padding:.65rem .2rem 1.15rem !important;
        margin-bottom:.35rem;
        border-bottom:1px solid rgba(255,255,255,.06);
    }
    .brand-name {
        font-size:1.52rem !important;
        letter-spacing:-.035em !important;
        background:linear-gradient(90deg,#ffffff 0%,#ff8d83 52%,#ffcfb0 100%);
        -webkit-background-clip:text;
        background-clip:text;
        color:transparent !important;
        text-shadow:0 10px 35px rgba(255,77,91,.18);
    }
    .brand-sub { color:#7f8998 !important; font-size:.74rem !important; }

    /* Sidebar navigation items */
    section[data-testid="stSidebar"] .stButton > button {
        position:relative;
        min-height:42px !important;
        padding:.55rem .75rem !important;
        margin:.14rem 0 !important;
        border-radius:12px !important;
        background:transparent !important;
        border:1px solid transparent !important;
        color:#cdd4de !important;
        text-align:left !important;
        box-shadow:none !important;
    }
    section[data-testid="stSidebar"] .stButton > button:hover {
        background:linear-gradient(90deg, rgba(255,77,91,.11), rgba(255,255,255,.025)) !important;
        border-color:rgba(255,77,91,.22) !important;
        color:#fff !important;
        transform:translateX(3px);
        box-shadow:0 8px 25px rgba(0,0,0,.18) !important;
    }
    section[data-testid="stSidebar"] .stButton > button[kind="primary"] {
        background:linear-gradient(135deg, rgba(255,77,91,.22), rgba(255,122,89,.08)) !important;
        border-color:rgba(255,100,100,.3) !important;
        color:#fff !important;
    }
    section[data-testid="stSidebar"] .stButton > button[kind="primary"]::before {
        content:"";
        position:absolute;
        left:0;
        top:9px;
        bottom:9px;
        width:3px;
        border-radius:99px;
        background:linear-gradient(180deg,var(--mh-accent),var(--mh-accent-2));
        box-shadow:0 0 18px rgba(255,77,91,.55);
    }

    /* Main page title */
    .page-title {
        font-size:2.25rem !important;
        line-height:1.02 !important;
        font-weight:850 !important;
        letter-spacing:-.05em !important;
        margin-top:.25rem !important;
        text-shadow:0 10px 35px rgba(0,0,0,.28);
    }
    .page-subtitle {
        max-width:980px;
        color:#8893a3 !important;
        font-size:.92rem !important;
        line-height:1.6 !important;
    }

    /* Cards: layered glass */
    .card, .job-card, .metric-card, .action-card, .chart-card,
    .info-card, .jobs-panel, .template-summary, .hero {
        background:
          linear-gradient(180deg, rgba(19,23,30,.92), rgba(11,14,19,.9)) !important;
        border:1px solid var(--mh-border) !important;
        box-shadow:var(--mh-shadow) !important;
        backdrop-filter:blur(14px);
    }
    .card, .job-card, .metric-card, .chart-card, .info-card, .jobs-panel, .template-summary {
        border-radius:20px !important;
    }
    .card:hover, .job-card:hover, .metric-card:hover, .action-card:hover, .info-card:hover {
        border-color:var(--mh-border-strong) !important;
        transform:translateY(-2px);
        transition:transform .18s ease, border-color .18s ease, box-shadow .18s ease;
    }

    .hero {
        position:relative;
        overflow:hidden;
        border-radius:24px !important;
        min-height:190px !important;
        padding:1.9rem 2rem !important;
        background:
          radial-gradient(circle at 85% 22%, rgba(255,77,91,.15), transparent 24%),
          radial-gradient(circle at 16% 100%, rgba(102,166,255,.08), transparent 20%),
          linear-gradient(135deg, rgba(28,31,40,.95), rgba(12,15,20,.92)) !important;
    }
    .hero::after {
        content:"JOBSYNC";
        position:absolute;
        right:-8px;
        bottom:-18px;
        font-size:4.2rem;
        font-weight:950;
        letter-spacing:-.07em;
        color:rgba(255,255,255,.025);
        pointer-events:none;
    }
    .hero-eyebrow { color:#ff8e8e !important; }
    .hero h1 {
        font-size:3rem !important;
        letter-spacing:-.06em !important;
    }

    /* Metric cards */
    .metric-card {
        position:relative;
        overflow:hidden;
    }
    .metric-card::after {
        content:"";
        position:absolute;
        width:90px;height:90px;
        right:-30px;top:-30px;
        border-radius:50%;
        background:rgba(255,255,255,.025);
        pointer-events:none;
    }
    .metric-card.red { box-shadow:inset 0 1px 0 rgba(255,77,91,.8), var(--mh-shadow) !important; }
    .metric-card.green { box-shadow:inset 0 1px 0 rgba(57,229,140,.75), var(--mh-shadow) !important; }

    /* Modern action cards */
    .action-card {
        min-height:166px !important;
        border-radius:20px 20px 10px 10px !important;
    }
    .action-icon {
        display:inline-flex;
        align-items:center;
        justify-content:center;
        width:42px;height:42px;
        border-radius:13px;
        background:linear-gradient(135deg,rgba(255,77,91,.18),rgba(255,255,255,.03));
        border:1px solid rgba(255,255,255,.08);
    }
    .action-title { letter-spacing:-.02em; }


    /* ===== PAGE STUDIO SKIN — VISUAL ONLY ===== */
    .mh-page-hero {
        position:relative; overflow:hidden; margin:.15rem 0 1.15rem; padding:1.35rem 1.5rem; border-radius:24px;
        border:1px solid rgba(255,255,255,.08);
        background:radial-gradient(circle at 88% 18%, rgba(255,77,91,.16), transparent 22%),radial-gradient(circle at 10% 90%, rgba(102,166,255,.10), transparent 24%),linear-gradient(135deg, rgba(22,26,34,.94), rgba(10,13,18,.94));
        box-shadow:0 24px 70px rgba(0,0,0,.30), inset 0 1px 0 rgba(255,255,255,.035); backdrop-filter:blur(16px);
    }
    .mh-page-hero::after { content:"JOBSYNC"; position:absolute; right:-.15rem; bottom:-1.1rem; font-size:4.6rem; font-weight:950; letter-spacing:-.08em; color:rgba(255,255,255,.025); pointer-events:none; }
    .mh-page-kicker { color:#ff8d8d; font-size:.66rem; font-weight:850; letter-spacing:.18em; text-transform:uppercase; }
    .mh-page-title { color:#f7f9fc; font-size:2rem; font-weight:900; letter-spacing:-.055em; line-height:1.05; margin-top:.3rem; }
    .mh-page-copy { color:#98a3b2; font-size:.88rem; line-height:1.55; max-width:880px; margin-top:.45rem; }
    .mh-page-meta { display:flex; flex-wrap:wrap; gap:.5rem; margin-top:.9rem; }
    .mh-meta-chip { display:inline-flex; align-items:center; gap:.38rem; padding:.38rem .62rem; border-radius:999px; color:#d8dee7; background:rgba(255,255,255,.035); border:1px solid rgba(255,255,255,.07); font-size:.68rem; font-weight:750; }
    .mh-meta-dot { width:7px; height:7px; border-radius:50%; background:#ff5a65; box-shadow:0 0 12px rgba(255,90,101,.55); }
    .mh-meta-dot.green { background:#39e58c; box-shadow:0 0 12px rgba(57,229,140,.45); }
    .mh-meta-dot.blue { background:#66a6ff; box-shadow:0 0 12px rgba(102,166,255,.45); }
    .mh-flowbar { display:grid; grid-template-columns:repeat(4,1fr); gap:.65rem; margin:0 0 1.15rem; }
    .mh-flowitem { display:flex; align-items:center; gap:.65rem; padding:.72rem .8rem; border-radius:16px; background:rgba(14,18,24,.80); border:1px solid rgba(255,255,255,.055); box-shadow:0 12px 28px rgba(0,0,0,.16); transition:all .16s ease; }
    .mh-flowitem:hover { transform:translateY(-2px); border-color:rgba(255,255,255,.12); }
    .mh-flownum { width:30px; height:30px; border-radius:10px; display:grid; place-items:center; background:linear-gradient(135deg, rgba(255,77,91,.22), rgba(255,122,89,.08)); color:#ff9a98; font-size:.67rem; font-weight:850; border:1px solid rgba(255,77,91,.22); }
    .mh-flowname { color:#edf1f6; font-size:.76rem; font-weight:800; }
    .mh-flowdesc { color:#7f8a99; font-size:.65rem; margin-top:.1rem; }
    .mh-pulse-card { display:flex; align-items:center; justify-content:space-between; gap:1rem; padding:.9rem 1rem; border-radius:17px; margin-bottom:1rem; background:linear-gradient(90deg, rgba(255,77,91,.08), rgba(255,255,255,.025)); border:1px solid rgba(255,255,255,.06); }
    .mh-pulse-left { display:flex; align-items:center; gap:.7rem; }
    .mh-pulse-orb { width:38px; height:38px; border-radius:13px; display:grid; place-items:center; color:#fff; font-weight:900; background:radial-gradient(circle at 35% 25%, #ff9b9b, #ff4d5b 48%, #7d1421 100%); box-shadow:0 0 22px rgba(255,77,91,.20); }
    .mh-pulse-title { color:#edf2f7; font-weight:800; font-size:.82rem; }
    .mh-pulse-sub { color:#7f8a99; font-size:.68rem; margin-top:.12rem; }
    .mh-pulse-status { padding:.3rem .55rem; border-radius:999px; font-size:.62rem; font-weight:850; color:#bdf3d2; background:rgba(57,229,140,.08); border:1px solid rgba(57,229,140,.16); }
    @media (max-width:900px){.mh-flowbar{grid-template-columns:repeat(2,1fr)}}
    @media (max-width:560px){.mh-flowbar{grid-template-columns:1fr}.mh-page-title{font-size:1.65rem}}

    /* ===== NEW SEARCH — ANIMATED COMMAND DECK ===== */
    .jobsync-search-command { position:relative; margin:.05rem 0 .85rem; padding:1rem 1.05rem .9rem; border-radius:24px; border:1px solid rgba(255,255,255,.08); background:radial-gradient(circle at 12% 18%, rgba(53,216,255,.10), transparent 22%),radial-gradient(circle at 88% 22%, rgba(180,72,255,.13), transparent 25%),radial-gradient(circle at 72% 100%, rgba(53,216,255,.05), transparent 28%),linear-gradient(135deg,#091221,#10172d 54%,#21132f); box-shadow:0 24px 70px rgba(0,0,0,.28),inset 0 1px 0 rgba(255,255,255,.05); overflow:hidden; }
    .jobsync-search-command:before { content:""; position:absolute; inset:-45% 42% -45% -8%; border-radius:50%; background:conic-gradient(from 0deg,transparent 0 68%,rgba(74,220,255,.12) 74%,rgba(143,92,255,.22) 80%,transparent 88%); filter:blur(18px); animation:searchCommandSweep 7s linear infinite; pointer-events:none; }
    .jobsync-search-command:after { content:""; position:absolute; left:0; right:0; bottom:0; height:1px; background:linear-gradient(90deg,transparent,rgba(55,221,255,.55),rgba(169,85,255,.55),transparent); opacity:.65; }
    @keyframes searchCommandSweep { to { transform:rotate(360deg); } }
    .jobsync-search-command-head { display:grid; grid-template-columns:minmax(0,1fr) 280px; align-items:center; gap:1rem; position:relative; z-index:2; }
    .jobsync-search-command-title { color:#f7f9fc; font-size:clamp(1.45rem,2.8vw,2.05rem); font-weight:920; letter-spacing:-.055em; line-height:1; margin-top:.2rem; text-shadow:0 0 24px rgba(113,155,255,.10); }
    .jobsync-search-command-copy { color:#8794a7; font-size:.7rem; line-height:1.45; margin-top:.42rem; max-width:680px; }
    .jobsync-search-command-visual { position:relative; height:102px; border-radius:18px; border:1px solid rgba(255,255,255,.07); background:rgba(3,8,18,.34); overflow:hidden; display:flex; align-items:center; justify-content:center; box-shadow:inset 0 0 30px rgba(45,122,255,.05); }
    .search-radar { position:relative; width:74px; height:74px; border-radius:50%; border:1px solid rgba(83,218,255,.22); background:radial-gradient(circle,rgba(53,216,255,.10) 0 18%,transparent 19% 100%); box-shadow:0 0 28px rgba(53,216,255,.10); }
    .search-radar:before,.search-radar:after { content:""; position:absolute; inset:10px; border:1px solid rgba(123,91,255,.22); border-radius:50%; }
    .search-radar:after { inset:23px; border-color:rgba(53,216,255,.25); }
    .search-radar i { position:absolute; left:50%; top:50%; width:31px; height:1px; transform-origin:0 50%; background:linear-gradient(90deg,rgba(53,216,255,.9),transparent); animation:searchRadar 2.2s linear infinite; box-shadow:0 0 9px rgba(53,216,255,.65); }
    .search-radar b { position:absolute; width:7px; height:7px; border-radius:50%; background:#6ee7ff; box-shadow:0 0 13px #6ee7ff; animation:searchDot 2.2s ease-in-out infinite; }
    .search-radar b:nth-child(2){left:55px;top:17px;animation-delay:.3s}.search-radar b:nth-child(3){left:15px;top:48px;animation-delay:.8s}.search-radar b:nth-child(4){left:45px;top:53px;animation-delay:1.2s}
    @keyframes searchRadar { to { transform:rotate(360deg); } }
    @keyframes searchDot { 50% { opacity:.25; transform:scale(.55); } }
    .search-command-caption { position:absolute; right:12px; bottom:9px; font-size:.46rem; letter-spacing:.12em; color:#718097; font-weight:900; text-transform:uppercase; }
    .search-signal-flow { position:relative; z-index:2; display:grid; grid-template-columns:repeat(4,1fr); gap:.45rem; margin-top:.72rem; padding:.55rem .6rem; border:1px solid rgba(255,255,255,.06); border-radius:15px; background:rgba(4,9,19,.32); }
    .search-signal-step { position:relative; display:flex; align-items:center; gap:.45rem; min-width:0; }
    .search-signal-node { width:24px; height:24px; flex:0 0 24px; display:grid; place-items:center; border-radius:8px; color:#eaf7ff; font-size:.62rem; font-weight:950; background:linear-gradient(135deg,rgba(53,216,255,.18),rgba(133,82,255,.24)); border:1px solid rgba(95,197,255,.20); box-shadow:0 0 14px rgba(74,160,255,.07); animation:searchNodePulse 2.8s ease-in-out infinite; }
    .search-signal-step:nth-child(2) .search-signal-node{animation-delay:.45s}.search-signal-step:nth-child(3) .search-signal-node{animation-delay:.9s}.search-signal-step:nth-child(4) .search-signal-node{animation-delay:1.35s}
    @keyframes searchNodePulse { 50% { box-shadow:0 0 22px rgba(74,160,255,.24); border-color:rgba(95,197,255,.45); transform:translateY(-1px); } }
    .search-signal-text { min-width:0 }.search-signal-name { color:#e7edf6; font-size:.57rem; font-weight:900; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }.search-signal-note { color:#69778d; font-size:.46rem; margin-top:1px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .search-signal-line { position:absolute; top:50%; right:-.32rem; width:.38rem; height:1px; background:linear-gradient(90deg,rgba(72,213,255,.55),rgba(147,84,255,.55)); overflow:hidden; }.search-signal-line:after { content:""; position:absolute; width:18px; height:2px; top:-.5px; left:-18px; background:#72eaff; box-shadow:0 0 9px #72eaff; animation:searchPacket 2.4s linear infinite; }.search-signal-step:nth-child(2) .search-signal-line:after{animation-delay:.6s}.search-signal-step:nth-child(3) .search-signal-line:after{animation-delay:1.2s}
    @keyframes searchPacket { to { left:100%; } }
    @media (max-width:900px){.jobsync-search-command-head{grid-template-columns:1fr}.jobsync-search-command-visual{height:86px}.search-signal-flow{grid-template-columns:repeat(2,1fr)}}
    @media (max-width:560px){.jobsync-search-command{padding:.75rem;border-radius:18px}.search-signal-flow{grid-template-columns:1fr}.search-signal-line{display:none}}

    /* ===== SEARCH SETTING WHEEL — RADIAL COMMAND CONTROL ===== */
    .jobsync-search-wheel-panel{position:relative;min-height:430px;margin:.25rem 0 .8rem;border-radius:28px;border:1px solid rgba(255,255,255,.065);background:radial-gradient(circle at 50% 48%,rgba(65,208,255,.10),transparent 18%),radial-gradient(circle at 50% 48%,rgba(126,74,255,.10),transparent 44%),rgba(4,9,19,.38);overflow:visible;}
    .jobsync-search-wheel-panel:before{content:"";position:absolute;left:50%;top:48%;width:300px;height:300px;transform:translate(-50%,-50%);border-radius:50%;border:1px solid rgba(70,211,255,.12);box-shadow:0 0 55px rgba(58,181,255,.08),inset 0 0 55px rgba(123,78,255,.06);animation:wheelSpin 18s linear infinite;pointer-events:none;}
    .jobsync-search-wheel-panel:after{content:"";position:absolute;left:50%;top:48%;width:210px;height:210px;transform:translate(-50%,-50%);border-radius:50%;border:1px dashed rgba(166,102,255,.20);animation:wheelSpinReverse 12s linear infinite;pointer-events:none;}
    .jobsync-wheel-heading{position:absolute;top:16px;left:0;right:0;text-align:center;z-index:5;pointer-events:none}.jobsync-wheel-heading span{display:block;color:#62ddff;font-size:.5rem;font-weight:950;letter-spacing:.16em;text-transform:uppercase}.jobsync-wheel-heading b{display:block;color:#edf4ff;font-size:.82rem;margin-top:4px;}
    .jobsync-wheel-spokes{position:absolute;left:50%;top:48%;width:100%;height:100%;transform:translate(-50%,-50%);pointer-events:none;z-index:1}.jobsync-wheel-spokes i{position:absolute;left:50%;top:48%;height:1px;width:118px;transform-origin:0 50%;background:linear-gradient(90deg,rgba(91,220,255,.55),rgba(139,92,255,.16),transparent);box-shadow:0 0 10px rgba(67,205,255,.15)}.jobsync-wheel-spokes .spoke-n{transform:rotate(-90deg)}.jobsync-wheel-spokes .spoke-e{transform:rotate(0deg)}.jobsync-wheel-spokes .spoke-s{transform:rotate(90deg)}.jobsync-wheel-spokes .spoke-w{transform:rotate(180deg)}
    .jobsync-search-wheel-core{position:absolute;left:50%;top:48%;z-index:3;width:112px;height:112px;transform:translate(-50%,-50%);border-radius:50%;display:flex;flex-direction:column;align-items:center;justify-content:center;background:radial-gradient(circle at 35% 25%,#29456f,#111b32 58%,#070d19 100%);border:1px solid rgba(99,222,255,.55);box-shadow:0 0 32px rgba(53,203,255,.22),0 0 80px rgba(120,75,255,.10),inset 0 0 28px rgba(149,86,255,.14);text-align:center;pointer-events:none;}
    .jobsync-search-wheel-core strong{position:relative;z-index:5;font-size:1.55rem;line-height:1;color:#f7fbff;text-shadow:0 0 18px rgba(93,225,255,.7);animation:signalFloat 2.8s ease-in-out infinite}.jobsync-search-wheel-core span{position:relative;z-index:5;font-size:.58rem;color:#f4f8ff;margin-top:7px;text-transform:uppercase;letter-spacing:.16em;font-weight:950}.jobsync-search-wheel-core small{position:relative;z-index:5;font-size:.34rem;color:#6fe6ff;margin-top:4px;letter-spacing:.2em;font-weight:900}.jobsync-search-wheel-core i{position:absolute;width:7px;height:7px;border-radius:50%;right:18px;top:17px;background:#69e6ff;box-shadow:0 0 16px #69e6ff;animation:corePulse 1.8s ease-in-out infinite;z-index:6}.jobsync-core-orbit{position:absolute;left:50%;top:50%;border-radius:50%;transform:translate(-50%,-50%);pointer-events:none}.jobsync-core-orbit.orbit-one{width:132px;height:132px;border:1px solid rgba(66,221,255,.34);animation:orbitSpin 7s linear infinite}.jobsync-core-orbit.orbit-two{width:154px;height:154px;border:1px dashed rgba(167,92,255,.34);animation:orbitSpinReverse 11s linear infinite}.jobsync-core-orbit:before{content:"";position:absolute;width:7px;height:7px;border-radius:50%;left:50%;top:-4px;background:#55dcff;box-shadow:0 0 16px #55dcff}.jobsync-core-orbit.orbit-two:before{left:auto;right:8%;top:50%;background:#bb72ff;box-shadow:0 0 16px #bb72ff}.jobsync-core-scan{position:absolute;left:50%;top:50%;width:100px;height:100px;transform:translate(-50%,-50%);border-radius:50%;background:conic-gradient(from 0deg,transparent 0deg,rgba(71,220,255,.22) 22deg,transparent 52deg,transparent 360deg);animation:scanSpin 2.6s linear infinite;pointer-events:none}.jobsync-core-pulse{position:absolute;left:50%;top:50%;width:72px;height:72px;transform:translate(-50%,-50%);border-radius:50%;border:1px solid rgba(91,220,255,.35);box-shadow:0 0 30px rgba(83,204,255,.14),inset 0 0 20px rgba(126,75,255,.13);animation:coreRingPulse 2.2s ease-in-out infinite;pointer-events:none}@keyframes signalFloat{50%{transform:translateY(-3px) scale(1.04)}}@keyframes orbitSpin{to{transform:translate(-50%,-50%) rotate(360deg)}}@keyframes orbitSpinReverse{to{transform:translate(-50%,-50%) rotate(-360deg)}}@keyframes scanSpin{to{transform:translate(-50%,-50%) rotate(360deg)}}@keyframes coreRingPulse{50%{width:84px;height:84px;opacity:.72;box-shadow:0 0 42px rgba(83,204,255,.24),inset 0 0 24px rgba(126,75,255,.18)}}
    @keyframes corePulse{50%{transform:scale(1.8);opacity:.3}} @keyframes wheelSpin{to{transform:translate(-50%,-50%) rotate(360deg)}} @keyframes wheelSpinReverse{to{transform:translate(-50%,-50%) rotate(-360deg)}}
    .jobsync-wheel-note{position:absolute;bottom:13px;left:50%;transform:translateX(-50%);font-size:.46rem;color:#64758d;letter-spacing:.13em;text-transform:uppercase;font-weight:900;z-index:5;white-space:nowrap}
    /* Turn the real Streamlit radio control into the radial wheel. */
    .jobsync-search-wheel-panel + div [data-testid="stRadio"]{position:relative!important;height:430px!important;margin-top:-430px!important;z-index:8!important;}
    .jobsync-search-wheel-panel + div [data-testid="stRadio"] > label{display:none!important}
    .jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"]{position:relative!important;width:100%!important;height:430px!important;min-height:430px!important;display:block!important;}
    .jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label{position:absolute!important;width:112px!important;height:112px!important;min-width:112px!important;margin:0!important;padding:0!important;border-radius:50%!important;display:flex!important;align-items:center!important;justify-content:center!important;background:linear-gradient(145deg,#111d32,#091120)!important;border:1px solid rgba(105,145,190,.30)!important;box-shadow:0 14px 35px rgba(0,0,0,.30),inset 0 1px 0 rgba(255,255,255,.045)!important;cursor:pointer!important;transition:all .25s ease!important;}
    .jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:nth-of-type(1){left:50%!important;top:58px!important;transform:translateX(-50%)!important}
    .jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:nth-of-type(2){right:7%!important;top:48%!important;transform:translateY(-50%)!important}
    .jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:nth-of-type(3){left:50%!important;bottom:44px!important;transform:translateX(-50%)!important}
    .jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:nth-of-type(4){left:7%!important;top:48%!important;transform:translateY(-50%)!important}
    .jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:hover{z-index:30!important;border-color:rgba(93,224,255,.85)!important;box-shadow:0 0 34px rgba(58,205,255,.25),0 18px 40px rgba(0,0,0,.35)!important;}
    .jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:nth-of-type(1):hover,.jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:nth-of-type(3):hover{transform:translateX(-50%) scale(1.10)!important}
    .jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:nth-of-type(2):hover,.jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:nth-of-type(4):hover{transform:translateY(-50%) scale(1.10)!important}
    .jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:has(input:checked){border-color:rgba(100,226,255,.92)!important;background:radial-gradient(circle at 35% 25%,rgba(54,218,255,.28),rgba(126,73,255,.24) 58%,#091120 100%)!important;box-shadow:0 0 34px rgba(65,211,255,.24),0 0 65px rgba(121,75,255,.12),inset 0 0 28px rgba(145,82,255,.15)!important;}
    .jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label input{position:absolute!important;opacity:0!important;width:1px!important;height:1px!important}
    .jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label p{font-size:.72rem!important;font-weight:950!important;color:#edf5ff!important;text-align:center!important;line-height:1.2!important;margin:0!important;max-width:82px!important;white-space:pre-line!important}
    .jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label p:before{display:block!important;font-size:1.35rem!important;line-height:1.15!important;margin-bottom:4px!important}
    .jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:nth-of-type(1) p:before{content:"🌐"}.jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:nth-of-type(2) p:before{content:"🎯"}.jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:nth-of-type(3) p:before{content:"📅"}.jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:nth-of-type(4) p:before{content:"🔗"}
    .jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:after{content:"";position:absolute;width:8px;height:8px;right:15px;top:14px;border-radius:50%;background:#58dcff;box-shadow:0 0 12px #58dcff;opacity:.45;animation:nodeBlink 2.4s ease-in-out infinite}.jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:nth-of-type(2):after{animation-delay:.5s}.jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:nth-of-type(3):after{animation-delay:1s}.jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:nth-of-type(4):after{animation-delay:1.5s}@keyframes nodeBlink{50%{opacity:1;transform:scale(1.35)}}
    @media(max-width:900px){.jobsync-search-wheel-panel{min-height:390px}.jobsync-search-wheel-panel + div [data-testid="stRadio"],.jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"]{height:390px!important;min-height:390px!important}.jobsync-search-wheel-panel + div [data-testid="stRadio"]{margin-top:-390px!important}.jobsync-search-wheel-panel:before{width:270px;height:270px}.jobsync-search-wheel-panel:after{width:185px;height:185px}.jobsync-search-wheel-core{width:98px;height:98px}.jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label{width:92px!important;height:92px!important;min-width:92px!important}.jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:nth-of-type(1){top:70px!important}.jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:nth-of-type(3){bottom:48px!important}}
    @media(max-width:560px){.jobsync-search-wheel-panel{min-height:350px}.jobsync-search-wheel-panel + div [data-testid="stRadio"],.jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"]{height:350px!important;min-height:350px!important}.jobsync-search-wheel-panel + div [data-testid="stRadio"]{margin-top:-350px!important}.jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:nth-of-type(2){right:1%!important}.jobsync-search-wheel-panel + div [data-testid="stRadio"] [role="radiogroup"] label:nth-of-type(4){left:1%!important}}

    /* ===== NEW SEARCH — LIVE JOB MAP (v1.6.0) ===== */
    .jobsync-live-map-header{display:flex;align-items:flex-end;justify-content:space-between;gap:.8rem;margin:.05rem 0 .45rem;padding:0 .15rem;}
    .jobsync-live-map-header div{display:flex;flex-direction:column;gap:.12rem;}
    .jobsync-live-map-header span{font-size:.48rem;font-weight:950;letter-spacing:.16em;color:#67dff8;text-transform:uppercase;}
    .jobsync-live-map-header b{font-size:1.02rem;line-height:1.1;font-weight:900;color:#f3f6fa;letter-spacing:-.02em;}
    .jobsync-live-map-header small{font-size:.53rem;line-height:1.35;color:#758297;text-align:right;max-width:240px;}
    .jobsync-live-map-header + div{border:1px solid rgba(255,255,255,.08);border-radius:20px;overflow:hidden;background:#09101a;box-shadow:inset 0 1px 0 rgba(255,255,255,.03);}
    @media(max-width:700px){.jobsync-live-map-header{align-items:flex-start;flex-direction:column}.jobsync-live-map-header small{text-align:left;max-width:none}}
    .jobsync-search-wheel-panel { min-width:0; }
    /* v1.3.75 immersive search signal tabs */
    [data-testid="stSegmentedControl"]{width:100%!important;margin:.35rem 0 .55rem!important;}
    [data-testid="stSegmentedControl"] [role="radiogroup"]{gap:.45rem!important;background:rgba(6,12,24,.52)!important;border:1px solid rgba(104,139,188,.18)!important;border-radius:16px!important;padding:.35rem!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.03),0 10px 30px rgba(0,0,0,.16)!important;}
    [data-testid="stSegmentedControl"] label{flex:1 1 0!important;min-height:42px!important;border-radius:11px!important;color:#9fb0c7!important;font-weight:950!important;letter-spacing:.09em!important;text-transform:uppercase!important;font-size:.58rem!important;transition:all .24s ease!important;border:1px solid transparent!important;background:rgba(17,28,46,.72)!important;}
    [data-testid="stSegmentedControl"] label:hover{color:#eef7ff!important;border-color:rgba(80,217,255,.35)!important;box-shadow:0 0 24px rgba(57,203,255,.10)!important;transform:translateY(-1px)!important;}
    [data-testid="stSegmentedControl"] label:has(input:checked){color:#ffffff!important;background:linear-gradient(90deg,rgba(43,193,255,.22),rgba(111,76,255,.26),rgba(197,79,203,.20))!important;border-color:rgba(102,222,255,.42)!important;box-shadow:0 0 25px rgba(82,198,255,.12),inset 0 0 18px rgba(141,83,255,.10)!important;}
    [data-testid="stSegmentedControl"] label p{font-weight:950!important;margin:0!important;}
    .jobsync-job-signal-panel{background:radial-gradient(circle at 50% 46%,rgba(52,213,255,.11),transparent 20%),radial-gradient(circle at 50% 46%,rgba(124,73,255,.13),transparent 46%),linear-gradient(180deg,rgba(7,15,29,.70),rgba(4,10,21,.90))!important;}
    .jobsync-job-signal-panel:before{width:320px!important;height:320px!important;border-color:rgba(70,211,255,.14)!important;box-shadow:0 0 70px rgba(56,196,255,.09),inset 0 0 65px rgba(123,78,255,.08)!important;}
    .jobsync-job-signal-panel:after{width:235px!important;height:235px!important;border-color:rgba(161,95,255,.24)!important;}
    .jobsync-search-wheel-core{width:126px!important;height:126px!important;background:radial-gradient(circle at 35% 25%,#294e77,#131e38 56%,#070d18 100%)!important;}
    .jobsync-search-wheel-core span{font-size:.66rem!important;letter-spacing:.18em!important;}
    .jobsync-search-wheel-core em{position:relative;z-index:5;margin-top:5px;font-style:normal;font-size:.38rem;letter-spacing:.16em;text-transform:uppercase;color:#9bc7e5;font-weight:900;opacity:.88;}
    .jobsync-core-orbit.orbit-three{width:184px;height:184px;border:1px dotted rgba(98,218,255,.16);animation:orbitSpin 15s linear infinite;}
    .jobsync-core-orbit.orbit-three:before{content:"";position:absolute;width:5px;height:5px;border-radius:50%;right:10%;bottom:8%;background:#67e6ff;box-shadow:0 0 13px #67e6ff;}
    .jobsync-job-dots{position:absolute;inset:0;z-index:4;pointer-events:none;}
    .jobsync-job-dots i{position:absolute;width:6px;height:6px;border-radius:50%;background:#6be8ff;box-shadow:0 0 14px rgba(107,232,255,.8);animation:jobSignalFloat 3.6s ease-in-out infinite;}
    .jobsync-job-dots i:nth-child(1){left:18%;top:42%;animation-delay:.2s}.jobsync-job-dots i:nth-child(2){right:16%;top:34%;background:#bb79ff;box-shadow:0 0 14px rgba(187,121,255,.8);animation-delay:1.1s}.jobsync-job-dots i:nth-child(3){left:28%;bottom:18%;animation-delay:1.8s}.jobsync-job-dots i:nth-child(4){right:20%;bottom:20%;background:#ff7cba;box-shadow:0 0 14px rgba(255,124,186,.75);animation-delay:2.5s}
    @keyframes jobSignalFloat{0%,100%{transform:translateY(0) scale(1);opacity:.65}50%{transform:translateY(-7px) scale(1.3);opacity:1}}
    .jobsync-wheel-heading b{text-transform:uppercase!important;letter-spacing:.14em!important;}
    .jobsync-wheel-note{text-transform:uppercase!important;letter-spacing:.13em!important;font-size:.45rem!important;color:#71859f!important;}
    .jobsync-search-wheel-title { color:#dfe5ed; font-size:.72rem; font-weight:850; letter-spacing:.08em; text-transform:uppercase; margin:.05rem 0 .18rem; }
    .jobsync-search-wheel-sub { color:#687384; font-size:.62rem; line-height:1.45; margin-bottom:.65rem; }
    .jobsync-search-option-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:.45rem; margin-bottom:.65rem; }
    .jobsync-search-option-grid [data-testid="stButton"] > button { min-height:42px !important; padding:.35rem .45rem !important; border-radius:12px !important; font-size:.65rem !important; }
    .jobsync-search-option-grid [data-testid="stButton"] > button[kind="primary"] { background:linear-gradient(135deg,rgba(53,216,255,.16),rgba(139,92,255,.22),rgba(236,79,209,.13)) !important; border-color:rgba(53,216,255,.30) !important; box-shadow:0 10px 28px rgba(92,78,220,.16), inset 0 1px 0 rgba(255,255,255,.08) !important; }
    .jobsync-search-setting-note { padding:.58rem .65rem; border:1px solid rgba(255,255,255,.055); border-radius:13px; background:rgba(255,255,255,.022); color:#8a95a4; font-size:.6rem; line-height:1.45; margin-bottom:.65rem; }
    .jobsync-search-setting-note b { color:#e8edf3; }
    .jobsync-search-settings-card { padding:.78rem; border-radius:15px; border:1px solid rgba(255,255,255,.06); background:rgba(255,255,255,.018); margin-bottom:.55rem; }
    .jobsync-search-settings-card h4 { color:#edf2f7; margin:0 0 .15rem; font-size:.78rem; }
    .jobsync-search-settings-card p { color:#6f7a89; margin:0 0 .55rem; font-size:.59rem; line-height:1.4; }
    .jobsync-search-settings-card [data-testid="stForm"] { border:0 !important; padding:0 !important; background:transparent !important; }
    .jobsync-search-settings-card input, .jobsync-search-settings-card textarea { border-radius:11px !important; }
    .jobsync-search-settings-card [data-testid="stRadio"] > label { display:none !important; }
    .jobsync-search-settings-card [data-testid="stRadio"] [role="radiogroup"] { gap:.4rem !important; }
    .jobsync-search-settings-card [data-testid="stRadio"] label { border:1px solid rgba(255,255,255,.065); background:rgba(255,255,255,.02); border-radius:11px; padding:.42rem .55rem !important; font-size:.62rem !important; }
    .jobsync-search-settings-card [data-testid="stRadio"] label:has(input:checked) { border-color:rgba(53,216,255,.38); background:rgba(53,216,255,.08); }
    .jobsync-search-find [data-testid="stButton"] > button { min-height:44px !important; border-radius:13px !important; }
    .jobsync-search-results-pane { min-width:0; display:flex; flex-direction:column; }
    .jobsync-search-results-top { display:flex; align-items:flex-start; justify-content:space-between; gap:.8rem; margin:.1rem 0 .7rem; }
    .jobsync-search-results-title { color:#f0f3f7; font-size:1rem; font-weight:880; letter-spacing:-.02em; }
    .jobsync-search-results-count { color:#8b96a5; font-size:.62rem; margin-top:.2rem; }
    .jobsync-search-results-hint { color:#697585; font-size:.59rem; text-align:right; max-width:190px; line-height:1.35; }
    .jobsync-search-empty { min-height:420px; border:1px dashed rgba(255,255,255,.09); border-radius:20px; display:flex; flex-direction:column; align-items:center; justify-content:center; text-align:center; padding:2rem; background:radial-gradient(circle at 50% 45%, rgba(53,216,255,.06), transparent 38%), rgba(255,255,255,.012); }
    .jobsync-search-empty-icon { width:58px; height:58px; display:grid; place-items:center; border-radius:18px; color:#6ee7ff; background:rgba(53,216,255,.07); border:1px solid rgba(53,216,255,.14); font-size:1.35rem; margin-bottom:.8rem; }
    .jobsync-search-empty-title { color:#e8edf3; font-size:1rem; font-weight:850; }
    .jobsync-search-empty-copy { color:#737e8d; font-size:.67rem; max-width:380px; line-height:1.55; margin-top:.35rem; }
    .jobsync-search-quote { color:#aeb7c4; font-size:.7rem; font-style:italic; margin-top:1rem; }
    @media (max-width:900px){ .jobsync-search-layout{grid-template-columns:1fr;} .jobsync-search-results-hint{display:none} }
    @media (max-width:560px){ .jobsync-search-command{padding:.75rem;border-radius:18px}.jobsync-search-command-head{align-items:flex-start;flex-direction:column}.jobsync-search-live{align-self:flex-start}.jobsync-search-layout{gap:.6rem}.jobsync-search-option-grid{grid-template-columns:1fr 1fr}.jobsync-search-empty{min-height:300px} }

    .st-key-jobsync-results-scroll { padding-right:.1rem; }
    /* ===== v1.3.75 SEARCH WORKSPACE — TWO-PANEL FIT-TO-VIEW ===== */
    @keyframes searchLine{to{transform:translateX(20%)}}
    .jobsync-wheel-panel{min-width:0}
    .jobsync-wheel-heading span,.jobsync-results-panel-title span{display:block;font-size:.48rem;font-weight:950;letter-spacing:.18em;color:#5fdcff;text-transform:uppercase}.jobsync-wheel-heading b{display:block;font-size:.83rem;color:#edf3fb;margin-top:3px}
    .jobsync-wheel-visual{height:112px;position:relative;display:flex;align-items:center;justify-content:center;margin:-1px 0 3px}
    .jobsync-wheel-orbit{position:absolute;border-radius:50%;border:1px solid rgba(77,213,255,.2);pointer-events:none}.orbit-a{width:108px;height:108px;animation:wheelSpin 12s linear infinite;border-left-color:rgba(92,90,255,.7)}.orbit-b{width:78px;height:78px;border-style:dashed;border-color:rgba(225,91,224,.23);animation:wheelSpinReverse 8s linear infinite}
    .jobsync-wheel-core{width:58px;height:58px;border-radius:50%;z-index:2;display:flex;flex-direction:column;align-items:center;justify-content:center;background:radial-gradient(circle at 35% 25%,#294b70,#121a30 60%,#090e19);border:1px solid rgba(92,220,255,.42);box-shadow:0 0 28px rgba(51,206,255,.16),inset 0 0 20px rgba(140,82,255,.14);animation:corePulse 2.8s ease-in-out infinite}.jobsync-wheel-core strong{font-size:.58rem;color:#f3f7ff;letter-spacing:.08em}.jobsync-wheel-core span{font-size:.37rem;color:#8190a4;letter-spacing:.14em;text-transform:uppercase;margin-top:2px}.jobsync-wheel-core i{position:absolute;width:4px;height:4px;border-radius:50%;background:#55e3ff;box-shadow:0 0 12px #55e3ff;animation:dotOrbit 3.8s linear infinite}
    @keyframes corePulse{50%{transform:scale(1.05);box-shadow:0 0 38px rgba(51,206,255,.25),inset 0 0 25px rgba(140,82,255,.2)}} @keyframes dotOrbit{to{transform:rotate(360deg) translateX(35px) rotate(-360deg)}}
    .st-key-wheel_sources button,.st-key-wheel_profile button,.st-key-wheel_ats button,.st-key-wheel_date button{height:68px!important;min-height:68px!important;border-radius:50%!important;padding:.25rem!important;font-size:.67rem!important;font-weight:900!important;line-height:1.1!important;border:1px solid rgba(102,151,205,.25)!important;background:radial-gradient(circle at 35% 25%,rgba(45,68,105,.82),rgba(10,16,29,.98) 68%)!important;box-shadow:0 10px 24px rgba(0,0,0,.24),inset 0 1px 0 rgba(255,255,255,.05)!important;transition:transform .2s ease,border-color .2s ease,box-shadow .2s ease!important}.st-key-wheel_sources button:hover,.st-key-wheel_profile button:hover,.st-key-wheel_ats button:hover,.st-key-wheel_date button:hover{transform:scale(1.07)!important;border-color:rgba(77,218,255,.72)!important;box-shadow:0 0 24px rgba(64,198,255,.18),0 12px 28px rgba(0,0,0,.32)!important}
    .jobsync-wheel-active{font-size:.5rem;letter-spacing:.15em;text-align:center;color:#6e8198;font-weight:900;margin:.15rem 0 .45rem}
    .jobsync-results-panel-title{padding:.15rem 0 .48rem;border-bottom:1px solid rgba(255,255,255,.07);margin-bottom:.45rem}.jobsync-results-panel-title b{display:block;color:#f2f6fb;font-size:1rem;margin-top:3px}.jobsync-results-summary{font-size:.57rem;color:#8390a3;margin-bottom:.5rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .st-key-wheel_sources,.st-key-wheel_profile,.st-key-wheel_ats,.st-key-wheel_date{display:flex;justify-content:center}.st-key-wheel_sources button,.st-key-wheel_profile button,.st-key-wheel_ats button,.st-key-wheel_date button{width:76px!important}

    /* ===== NEW SEARCH — TIGHT CONTROL BAR + RESULTS BESIDE IT ===== */
    .jobsync-search-settings-bar {
        display:flex; align-items:flex-end; justify-content:space-between; gap:.75rem;
        margin:.15rem 0 .38rem; padding:0 .15rem;
    }
    .jobsync-search-settings-bar-title { color:#dfe5ed; font-size:.72rem; font-weight:900; letter-spacing:.09em; text-transform:uppercase; }
    .jobsync-search-settings-bar-copy { color:#697585; font-size:.59rem; line-height:1.35; text-align:right; }
    .jobsync-search-settings-bar + div [data-testid="stButton"] > button {
        min-height:38px !important; padding:.3rem .5rem !important; border-radius:12px !important;
        font-size:.64rem !important; font-weight:800 !important; transition:all .18s ease !important;
    }
    .jobsync-search-settings-bar + div [data-testid="stButton"] > button:hover {
        transform:translateY(-1px); border-color:rgba(110,231,255,.28) !important;
    }
    .jobsync-search-controls { padding:.68rem !important; border-radius:16px !important; }
    .jobsync-search-active-label {
        color:#6ee7ff; font-size:.55rem; font-weight:900; letter-spacing:.14em;
        text-transform:uppercase; margin:0 0 .48rem .1rem; opacity:.9;
    }
    .jobsync-search-active-label span { color:#687384; margin:0 .15rem; }
    .jobsync-search-collapsed-state {
        display:flex; align-items:center; justify-content:space-between; gap:.5rem;
        min-height:34px; padding:.42rem .55rem; margin:0 0 .42rem;
        border-radius:10px; border:1px solid rgba(255,255,255,.045);
        background:rgba(255,255,255,.018); color:#7f8a99; font-size:.59rem;
    }
    .jobsync-search-collapsed-state span { color:#b5bfcb; font-weight:750; white-space:nowrap; }
    .jobsync-search-collapsed-state b { color:#6f7b8b; font-weight:650; text-align:right; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .jobsync-search-controls [data-testid="stButton"] > button {
        min-height:34px !important; padding:.42rem .55rem !important; margin:0 0 .42rem !important;
        border-radius:10px !important; border:1px solid rgba(255,255,255,.045) !important;
        background:rgba(255,255,255,.018) !important; color:#aeb7c4 !important;
        font-size:.59rem !important; text-align:left !important;
    }
    .jobsync-search-controls [data-testid="stButton"] > button:hover {
        border-color:rgba(110,231,255,.22) !important; background:rgba(53,216,255,.045) !important;
    }
    .jobsync-search-find [data-testid="stButton"] > button { text-align:center !important; }
    .jobsync-search-settings-card { padding:.68rem !important; margin-bottom:.42rem !important; border-radius:13px !important; }
    .jobsync-search-settings-card-active {
        border-color:rgba(110,231,255,.12) !important;
        background:linear-gradient(145deg,rgba(53,216,255,.035),rgba(139,92,255,.025),rgba(255,255,255,.012)) !important;
    }
    .jobsync-search-find { margin-top:.15rem; }
    .jobsync-search-find [data-testid="stButton"] > button { min-height:42px !important; }
    .jobsync-search-results-shell {
        min-width:0; margin:0; padding:.05rem 0 0;
    }
    .jobsync-search-results-shell > div:first-child { margin-top:0 !important; }
    .jobsync-search-results-head {
        display:flex; align-items:center; justify-content:space-between; gap:.75rem;
        padding:.15rem .1rem .48rem; border-bottom:1px solid rgba(255,255,255,.055); margin-bottom:.55rem;
    }
    .jobsync-search-results-shell [data-testid="stVerticalBlockBorderWrapper"] {
        margin-bottom:.55rem !important;
    }
    .jobsync-search-results-shell [data-testid="stVerticalBlockBorderWrapper"] > div {
        padding:.72rem !important;
    }
    .jobsync-search-empty { min-height:360px !important; }
    @media (max-width:900px){
        .jobsync-search-settings-bar { align-items:flex-start; flex-direction:column; gap:.18rem; }
        .jobsync-search-settings-bar-copy { text-align:left; }
    }
    @media (max-width:560px){
        .jobsync-search-settings-bar + div [data-testid="stButton"] > button { min-height:36px !important; font-size:.59rem !important; }
        .jobsync-search-collapsed-state { min-height:32px; }
    }

    /* Buttons */
    .stButton > button, .stFormSubmitButton > button,
    .stLinkButton > a, .stDownloadButton > button {
        min-height:42px !important;
        border-radius:13px !important;
        border-width:1px !important;
        box-shadow:0 7px 24px rgba(0,0,0,.16) !important;
        transition:transform .16s ease, box-shadow .16s ease, filter .16s ease !important;
    }
    .stButton > button:hover, .stFormSubmitButton > button:hover,
    .stLinkButton > a:hover, .stDownloadButton > button:hover {
        transform:translateY(-1px);
        filter:brightness(1.04);
        box-shadow:0 13px 30px rgba(0,0,0,.22) !important;
    }
    .stButton > button[kind="primary"],
    .stFormSubmitButton > button[kind="primary"],
    button[kind="primaryFormSubmit"] {
        background:linear-gradient(135deg,#ff4958,#ff6f57) !important;
        border-color:#ff5961 !important;
        box-shadow:0 12px 28px rgba(255,77,91,.22) !important;
    }
    .stLinkButton > a {
        background:linear-gradient(135deg,#18243a,#15263a) !important;
        color:#dceaff !important;
        border-color:rgba(102,166,255,.26) !important;
    }

    /* Inputs */
    div[data-baseweb="input"], div[data-baseweb="textarea"],
    div[data-baseweb="select"] > div {
        border-radius:13px !important;
        border-color:rgba(255,255,255,.09) !important;
        background:rgba(12,16,22,.88) !important;
        box-shadow:inset 0 1px 0 rgba(255,255,255,.025) !important;
    }
    div[data-baseweb="input"]:focus-within,
    div[data-baseweb="textarea"]:focus-within,
    div[data-baseweb="select"] > div:focus-within {
        border-color:rgba(255,105,110,.55) !important;
        box-shadow:0 0 0 3px rgba(255,77,91,.09), inset 0 1px 0 rgba(255,255,255,.03) !important;
    }
    textarea, input { font-size:.92rem !important; }

    /* Tabs */
    [data-baseweb="tab-list"] {
        gap:.35rem !important;
        padding:.3rem !important;
        border:1px solid rgba(255,255,255,.06);
        border-radius:14px !important;
        background:rgba(10,13,18,.72) !important;
    }
    [data-baseweb="tab"] {
        min-height:38px !important;
        padding:0 1rem !important;
        border-radius:10px !important;
        color:#8994a4 !important;
        font-weight:750 !important;
        transition:all .16s ease !important;
    }
    [data-baseweb="tab"]:hover {
        background:rgba(255,255,255,.035) !important;
        color:#e8edf4 !important;
    }
    [aria-selected="true"][data-baseweb="tab"] {
        background:linear-gradient(135deg,rgba(255,77,91,.18),rgba(255,122,89,.06)) !important;
        color:#fff !important;
        box-shadow:inset 0 0 0 1px rgba(255,99,105,.22);
    }
    [data-baseweb="tab-highlight"] {
        background:linear-gradient(90deg,#ff4d5b,#ff8b62) !important;
        height:2px !important;
        border-radius:99px !important;
    }

    /* Expanders */
    div[data-testid="stExpander"] {
        border-radius:16px !important;
        border-color:rgba(255,255,255,.07) !important;
        background:rgba(14,18,24,.75) !important;
        overflow:hidden;
    }

    /* File uploader */
    div[data-testid="stFileUploaderDropzone"] {
        border-radius:16px !important;
        border:1px dashed rgba(255,255,255,.13) !important;
        background:linear-gradient(180deg,rgba(19,23,30,.72),rgba(10,14,19,.76)) !important;
        transition:all .18s ease;
    }
    div[data-testid="stFileUploaderDropzone"]:hover {
        border-color:rgba(255,105,110,.42) !important;
        background:linear-gradient(180deg,rgba(30,23,30,.78),rgba(12,14,20,.8)) !important;
    }

    /* Alerts */
    div[data-testid="stAlert"] {
        border-radius:15px !important;
        box-shadow:0 10px 30px rgba(0,0,0,.14);
        border:1px solid rgba(255,255,255,.06) !important;
    }

    /* Home contact links */
    .contact-card { display:flex; align-items:center; justify-content:space-between; gap:1rem; padding:1.1rem 1.25rem; margin-top:.1rem; border:1px solid #252a31; border-radius:18px; background:linear-gradient(135deg,#101318,#0b0e12); box-shadow:0 14px 36px rgba(0,0,0,.20); }
    .contact-copy { min-width:0; }
    .contact-title { color:#f5f7fa; font-weight:850; font-size:1rem; }
    .contact-note { color:#98a2b3; font-size:.82rem; margin-top:.18rem; }
    .contact-actions { display:flex; gap:.65rem; flex-wrap:wrap; }
    .contact-btn { display:inline-flex; align-items:center; justify-content:center; padding:.62rem .9rem; border-radius:11px; border:1px solid #303640; color:#f5f7fa !important; text-decoration:none !important; font-weight:800; font-size:.82rem; background:#15191f; transition:transform .16s ease, border-color .16s ease, background .16s ease; }
    .contact-btn:hover { transform:translateY(-1px); border-color:#4a535f; background:#1b2027; }
    .contact-btn.whatsapp:hover { border-color:#22c55e; }
    .contact-btn.discord:hover { border-color:#7c8cff; }

    /* Job rows and pills */
    .source-pill, .pill, .status-pill {
        border:1px solid rgba(255,255,255,.06);
        box-shadow:inset 0 1px 0 rgba(255,255,255,.03);
    }
    .status-pill { letter-spacing:.01em; }

    /* Floating hover "JobSync" mark — decorative only */
    body::after {
        content:"JH";
        position:fixed;
        right:22px;
        bottom:20px;
        width:54px;
        height:54px;
        border-radius:50%;
        display:flex;
        align-items:center;
        justify-content:center;
        z-index:999;
        font-size:.88rem;
        font-weight:900;
        letter-spacing:-.03em;
        color:#fff;
        background:radial-gradient(circle at 32% 28%,#ff9b8f 0%,#ff5362 42%,#a51e36 100%);
        border:1px solid rgba(255,255,255,.18);
        box-shadow:0 14px 35px rgba(0,0,0,.32), 0 0 0 7px rgba(255,77,91,.055);
        animation:mhPulse 2.8s ease-in-out infinite;
        pointer-events:none;
    }
    @keyframes mhPulse {
        0%,100% { transform:translateY(0) scale(1); box-shadow:0 14px 35px rgba(0,0,0,.32), 0 0 0 7px rgba(255,77,91,.055); }
        50% { transform:translateY(-4px) scale(1.035); box-shadow:0 18px 42px rgba(0,0,0,.36), 0 0 0 12px rgba(255,77,91,.025); }
    }

    /* Scrollbars */
    ::-webkit-scrollbar { width:9px; height:9px; }
    ::-webkit-scrollbar-track { background:#090b0f; }
    ::-webkit-scrollbar-thumb {
        background:linear-gradient(180deg,#2a3039,#1b2028);
        border:2px solid #090b0f;
        border-radius:99px;
    }
    ::-webkit-scrollbar-thumb:hover { background:linear-gradient(180deg,#434b58,#2b333e); }

    @media (max-width: 900px) {
        .page-title { font-size:1.85rem !important; }
        .hero h1 { font-size:2.2rem !important; }
        .hero { padding:1.35rem 1.25rem !important; }
        body::after { right:12px; bottom:12px; width:48px; height:48px; }
    }


    /* ================= CV STUDIO v1.3.29 — ONE-PAGE COMMAND DECK ================= */
    .cv29-hero {
        position:relative; overflow:hidden; padding:1.05rem 1.25rem; margin:.05rem 0 .7rem;
        border:1px solid rgba(255,255,255,.085); border-radius:20px;
        background:radial-gradient(circle at 88% 10%,rgba(116,73,255,.18),transparent 30%),
                   radial-gradient(circle at 4% 90%,rgba(39,196,255,.10),transparent 26%),
                   linear-gradient(135deg,rgba(13,20,31,.97),rgba(18,13,42,.93));
        box-shadow:0 18px 50px rgba(0,0,0,.22), inset 0 1px 0 rgba(255,255,255,.025);
    }
    .cv29-hero:after { content:""; position:absolute; width:210px; height:210px; right:-72px; top:-125px; border-radius:50%; border:1px solid rgba(144,113,255,.20); box-shadow:0 0 0 28px rgba(144,113,255,.035),0 0 0 58px rgba(144,113,255,.018); pointer-events:none; }
    .cv29-kicker { color:#8aa4ff; font-size:.58rem; font-weight:900; letter-spacing:.17em; margin-bottom:.28rem; }
    .cv29-title-row { display:flex; align-items:center; gap:.72rem; }
    .cv29-orb { width:40px; height:40px; display:flex; align-items:center; justify-content:center; flex:0 0 auto; border-radius:13px; color:#fff; font-size:.72rem; font-weight:950; background:linear-gradient(135deg,#38bdf8,#7c3aed); box-shadow:0 8px 25px rgba(83,105,255,.25); }
    .cv29-title { color:#f8fafc; font-size:1.55rem; line-height:1; font-weight:900; letter-spacing:-.04em; }
    .cv29-subtitle { color:#8e9aaa; margin-top:.22rem; font-size:.72rem; }
    .cv29-status { position:absolute; right:1rem; bottom:.9rem; color:#9ba7b8; font-size:.57rem; font-weight:850; letter-spacing:.08em; }
    .cv29-dot { display:inline-block; width:6px; height:6px; margin-right:5px; border-radius:50%; background:#39e58c; box-shadow:0 0 0 4px rgba(57,229,140,.08); vertical-align:1px; }
    .cv29-modebar { display:flex; align-items:center; justify-content:space-between; gap:.8rem; margin:.15rem 0 .6rem; padding:.48rem .65rem; border:1px solid rgba(255,255,255,.065); border-radius:14px; background:rgba(9,13,19,.66); }
    .cv29-mode-copy { min-width:0; }
    .cv29-mode-copy b { display:block; color:#edf2f8; font-size:.72rem; }
    .cv29-mode-copy span { display:block; color:#687486; font-size:.58rem; margin-top:.08rem; }
    .cv29-panel { height:100%; padding:.72rem .78rem; border:1px solid rgba(255,255,255,.065); border-radius:17px; background:linear-gradient(145deg,rgba(15,20,28,.93),rgba(10,14,20,.88)); box-shadow:0 12px 30px rgba(0,0,0,.12); }
    .cv29-panel-head { display:flex; align-items:center; justify-content:space-between; gap:.6rem; margin-bottom:.5rem; }
    .cv29-panel-title { color:#f2f5f9; font-size:.76rem; font-weight:850; }
    .cv29-panel-hint { color:#687487; font-size:.56rem; }
    .cv29-chip { display:inline-flex; align-items:center; gap:.28rem; padding:.2rem .45rem; border-radius:999px; color:#b9c7ff; background:rgba(111,105,255,.10); border:1px solid rgba(111,105,255,.17); font-size:.55rem; font-weight:850; letter-spacing:.04em; }
    .cv29-target { margin:.35rem 0 .5rem; padding:.58rem .62rem; border-radius:13px; background:rgba(5,9,14,.56); border:1px solid rgba(255,255,255,.05); }
    .cv29-target-kicker { color:#5f6b7c; font-size:.52rem; font-weight:900; letter-spacing:.13em; }
    .cv29-target-title { color:#f1f5f9; font-size:.83rem; font-weight:850; margin-top:.16rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .cv29-target-meta { color:#7f8b9b; font-size:.57rem; margin-top:.13rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .cv29-mini-grid { display:grid; grid-template-columns:1fr 1fr; gap:.38rem; margin-top:.45rem; }
    .cv29-mini-card { min-height:38px; padding:.42rem .5rem; border:1px solid rgba(255,255,255,.05); border-radius:10px; background:#0c1117; }
    .cv29-mini-card b { display:block; color:#e6ebf2; font-size:.58rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .cv29-mini-card span { display:block; color:#606d7e; font-size:.51rem; margin-top:.08rem; }
    .cv29-advanced { margin-top:.48rem; }
    .cv29-ready { display:flex; align-items:center; gap:.6rem; padding:.55rem .62rem; margin-bottom:.45rem; border:1px solid rgba(57,229,140,.13); border-radius:12px; background:rgba(57,229,140,.045); }
    .cv29-ready-icon { width:27px; height:27px; display:grid; place-items:center; border-radius:9px; background:rgba(57,229,140,.12); color:#65e6a0; font-weight:900; font-size:.72rem; }
    .cv29-ready b { display:block; color:#eaf6ef; font-size:.65rem; }
    .cv29-ready span { display:block; color:#71817a; font-size:.53rem; margin-top:.07rem; }
    .cv29-upload { padding:.55rem .62rem; border:1px dashed rgba(255,255,255,.10); border-radius:12px; background:rgba(7,11,16,.48); }
    .cv29-upload b { color:#e8edf3; font-size:.63rem; }
    .cv29-upload span { color:#657182; font-size:.53rem; }
    .cv29-action-row { display:flex; gap:.45rem; align-items:center; margin-top:.55rem; }
    .cv29-action-note { color:#687486; font-size:.56rem; line-height:1.3; }
    .cv29-prompt { border:1px solid rgba(255,255,255,.065); border-radius:13px; overflow:hidden; background:#090d12; margin:.35rem 0 .5rem; }
    .cv29-prompt-head { display:flex; align-items:center; gap:.5rem; padding:.45rem .58rem; border-bottom:1px solid rgba(255,255,255,.055); background:#0e131a; }
    .cv29-prompt-head b { color:#dfe6ef; font-size:.61rem; }
    .cv29-prompt-head span { color:#657181; font-size:.52rem; }
    .cv29-prompt-head .cv29-copy-label { margin-left:auto; color:#8ea5ff; font-weight:850; }
    .cv29-prompt textarea { display:block; box-sizing:border-box; width:100%; height:145px; resize:none; border:0; outline:0; padding:.55rem .62rem; background:#090d12; color:#dce4ee; font:10px/1.38 Consolas,monospace; }
    .cv29-final-actions { display:grid; grid-template-columns:1fr 1fr; gap:.5rem; align-items:end; }
    .cv29-final-actions > div { min-width:0; }
    .cv29-save-hint { color:#657181; font-size:.53rem; margin-top:.3rem; }
    .cv29-section-label { color:#657183; font-size:.53rem; font-weight:900; letter-spacing:.13em; margin:.52rem 0 .28rem; }
    .cv29-hidden-note { color:#647082; font-size:.56rem; margin:.18rem 0 0; }
    @media (max-width:1000px) {
        .cv29-final-actions { grid-template-columns:1fr; }
    }
    @media (max-width:850px) {
        .cv29-status { display:none; }
        .cv29-modebar { align-items:flex-start; flex-direction:column; }
    }

    @media (min-width:901px) {
        .block-container { padding-right:305px !important; }
    }

    /* v2.5.0 Apple Glass Home — Home-only visual system.
       Frosted "Liquid Glass" panels (blurred translucent surfaces, soft inner
       highlight, gentle depth on hover) replace the old neural-network
       visual. The page still fits one viewport without a page scrollbar at
       typical window heights (padding trimmed, not hard-clipped — an
       unusually short window degrades to a small scroll instead of hiding
       content), and only the "Online now" list keeps its own small internal
       scroll, since it's the one genuinely unbounded list on this page. */
    /* Home's own spacing is margin-based (see .jobsync-launch-hero's margin
       further down) rather than viewport-height flex centering — simpler
       and more predictable across different window heights/browser chrome
       than trying to force block-container itself to a fixed height. */
    body:has(.st-key-home_shell) [data-testid="stAppViewContainer"] .block-container {
        padding-top: .6vh !important;
        padding-bottom: 3vh !important;
    }
    .jobsync-home-shell,.st-key-home_shell{width:100%;max-width:1200px;margin:0 auto;padding:0;}
    .jobsync-home-greeting-top{text-align:left;margin:0 0 1vh;padding:0 2px;}
    .jobsync-home-greeting-kicker{font-size:.6rem;font-weight:950;letter-spacing:.22em;color:#a9d8ff;margin-bottom:4px}.jobsync-home-greeting-title{font-size:clamp(1.5rem,2.9vw,2.5rem);line-height:1.08;font-weight:800;letter-spacing:-.03em;color:#fff;animation:jobsync-home-fade .7s cubic-bezier(.22,1,.36,1) both}.jobsync-home-greeting-subtitle{margin-top:4px;color:rgba(226,233,247,.68);font-size:clamp(.7rem,1vw,.84rem);animation:jobsync-home-fade .7s cubic-bezier(.22,1,.36,1) .08s both;}
    @keyframes jobsync-home-fade{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}

    /* Glass panel base — used by the "about" card, the presence card and the
       quick-actions row so the whole page reads as one coherent material. */
    .ag-glass{position:relative;border-radius:28px;background:linear-gradient(135deg,rgba(255,255,255,.10),rgba(255,255,255,.025));border:1px solid rgba(255,255,255,.16);backdrop-filter:blur(28px) saturate(180%);-webkit-backdrop-filter:blur(28px) saturate(180%);box-shadow:0 24px 60px rgba(0,0,0,.35),inset 0 1px 0 rgba(255,255,255,.16),inset 0 -1px 0 rgba(0,0,0,.14);overflow:hidden;transition:transform .45s cubic-bezier(.22,1,.36,1),box-shadow .45s cubic-bezier(.22,1,.36,1),border-color .35s ease;}
    .ag-glass:before{content:"";position:absolute;inset:0;background:radial-gradient(circle at 16% -10%,rgba(255,255,255,.20),transparent 46%);pointer-events:none;}
    .ag-glass:hover{transform:translateY(-3px);border-color:rgba(255,255,255,.24);box-shadow:0 32px 80px rgba(0,0,0,.4),inset 0 1px 0 rgba(255,255,255,.22),inset 0 -1px 0 rgba(0,0,0,.14);}

    .ag-grid{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:14px;align-items:stretch;}
    .ag-about{padding:24px 26px;display:flex;flex-direction:column;min-height:0;}
    .ag-about-kicker{font-size:.58rem;font-weight:850;letter-spacing:.24em;color:#a9d8ff;text-transform:uppercase;position:relative;z-index:1;}
    .ag-about-title{margin-top:9px;font-size:clamp(1.1rem,1.8vw,1.42rem);font-weight:800;color:#fff;letter-spacing:-.02em;line-height:1.3;position:relative;z-index:1;}
    .ag-about-copy{margin-top:8px;color:rgba(230,236,248,.72);font-size:.76rem;line-height:1.62;max-width:560px;position:relative;z-index:1;}
    .ag-feature-row{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:16px;position:relative;z-index:1;}
    .ag-feature{padding:13px 14px;border-radius:18px;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.11);transition:transform .35s cubic-bezier(.22,1,.36,1),background .3s ease,border-color .3s ease;}
    .ag-feature:hover{transform:translateY(-3px);background:rgba(255,255,255,.09);border-color:rgba(255,255,255,.2);}
    .ag-feature-icon{width:30px;height:30px;border-radius:10px;display:grid;place-items:center;background:linear-gradient(135deg,rgba(120,180,255,.4),rgba(190,130,255,.32));font-size:.82rem;box-shadow:inset 0 1px 0 rgba(255,255,255,.3);}
    .ag-feature-name{margin-top:9px;color:#fff;font-size:.72rem;font-weight:750;letter-spacing:-.01em;}
    .ag-feature-sub{margin-top:3px;color:rgba(222,229,242,.6);font-size:.6rem;line-height:1.45;}

    /* Presence card */
    .ag-presence{padding:20px 20px 16px;display:flex;flex-direction:column;min-height:0;}
    .ag-presence-head{display:flex;align-items:center;gap:8px;position:relative;z-index:1;}
    .ag-live-dot{width:8px;height:8px;border-radius:50%;background:#39e58c;box-shadow:0 0 0 0 rgba(57,229,140,.55);animation:agPulse 2.2s ease-out infinite;flex:0 0 auto;}
    @keyframes agPulse{0%{box-shadow:0 0 0 0 rgba(57,229,140,.55)}70%{box-shadow:0 0 0 11px rgba(57,229,140,0)}100%{box-shadow:0 0 0 0 rgba(57,229,140,0)}}
    .ag-presence-title{font-size:.82rem;font-weight:800;color:#fff;letter-spacing:-.01em;}
    .ag-presence-count{margin-left:auto;color:#82f2b8;font-weight:850;font-size:.9rem;}
    .ag-presence-sub{margin-top:2px;color:rgba(222,229,242,.55);font-size:.58rem;position:relative;z-index:1;}
    .ag-avatar-stack{display:flex;margin-top:14px;position:relative;z-index:1;}
    .ag-avatar{width:36px;height:36px;border-radius:50%;display:grid;place-items:center;font-size:.6rem;font-weight:800;color:#fff;background:linear-gradient(135deg,#57d8ff,#8c6bff 55%,#ff7ad1);border:2px solid rgba(10,14,24,.92);margin-left:-10px;box-shadow:0 6px 16px rgba(0,0,0,.35);transition:transform .3s cubic-bezier(.22,1,.36,1);}
    .ag-avatar:first-child{margin-left:0;}
    .ag-avatar:hover{transform:translateY(-4px) scale(1.1);z-index:5;}
    .ag-avatar-more{width:36px;height:36px;border-radius:50%;display:grid;place-items:center;font-size:.56rem;font-weight:800;color:#dfe6f5;background:rgba(255,255,255,.12);border:2px solid rgba(10,14,24,.92);margin-left:-10px;}
    .ag-presence-list{margin-top:14px;max-height:19vh;overflow-y:auto;position:relative;z-index:1;}
    .ag-presence-row{display:flex;align-items:center;gap:9px;padding:6px 2px;border-bottom:1px solid rgba(255,255,255,.06);}
    .ag-presence-row:last-child{border-bottom:0;}
    .ag-presence-avatar{width:24px;height:24px;border-radius:50%;display:grid;place-items:center;font-size:.5rem;font-weight:800;color:#fff;background:linear-gradient(135deg,#57d8ff,#8c6bff 55%,#ff7ad1);flex:0 0 auto;}
    .ag-presence-name{color:#eef2fb;font-size:.66rem;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
    .ag-presence-empty{color:rgba(222,229,242,.5);font-size:.66rem;padding:14px 2px;text-align:center;position:relative;z-index:1;}

    /* Quick actions — real Streamlit buttons styled as glass pills. */
    .ag-actions-head{margin:14px 2px 8px;color:rgba(222,229,242,.6);font-size:.58rem;font-weight:800;letter-spacing:.14em;text-transform:uppercase;}
    .st-key-ag_actions_row [data-testid="stHorizontalBlock"]{gap:10px !important;}
    .st-key-ag_actions_row .stButton > button{min-height:56px !important;border-radius:18px !important;background:linear-gradient(135deg,rgba(255,255,255,.10),rgba(255,255,255,.025)) !important;border:1px solid rgba(255,255,255,.16) !important;backdrop-filter:blur(20px) !important;color:#fff !important;font-weight:750 !important;font-size:.72rem !important;box-shadow:0 12px 30px rgba(0,0,0,.28),inset 0 1px 0 rgba(255,255,255,.14) !important;transition:transform .35s cubic-bezier(.22,1,.36,1),background .3s ease,box-shadow .3s ease !important;white-space:pre-line !important;line-height:1.4 !important;}
    .st-key-ag_actions_row .stButton > button:hover{transform:translateY(-3px) !important;background:rgba(255,255,255,.14) !important;box-shadow:0 18px 42px rgba(0,0,0,.34),inset 0 1px 0 rgba(255,255,255,.2) !important;}
    .st-key-ag_actions_row .stButton > button p:first-line{font-weight:800;}

    @media(prefers-reduced-motion:reduce){.ag-glass,.ag-feature,.ag-avatar,.st-key-ag_actions_row .stButton > button,.jobsync-home-greeting-title,.jobsync-home-greeting-subtitle,.ag-live-dot{animation:none!important;transition:none!important}}
    @media(max-width:1100px){.ag-grid{grid-template-columns:minmax(0,1fr) 280px}}
    @media(max-width:900px){.block-container{padding-left:1rem!important;padding-right:1rem!important}.ag-grid{grid-template-columns:1fr}.ag-presence-list{max-height:22vh;}}
    @media(max-width:640px){.jobsync-home-greeting-title{font-size:clamp(1.7rem,7vw,2.2rem)}.ag-feature-row{grid-template-columns:1fr}.ag-about{padding:18px 18px}.ag-presence{padding:16px 16px 14px}}
    @media (min-width:1500px){.jobsync-home-shell,.st-key-home_shell{max-width:1360px}}
    /* Online presence is rendered only inside the Home overview. The duplicate
       fixed top-right presence panel has intentionally been removed. */
    .jobsync-role-badge { display:inline-flex; align-items:center; padding:2px 7px; border-radius:999px; font-size:.58rem; line-height:1.25; font-weight:850; letter-spacing:.04em; text-transform:uppercase; background:rgba(255,77,91,.08); border:1px solid rgba(255,77,91,.18); color:#ff9da2 !important; }
    .jobsync-role-badge.admin { background:rgba(239,68,68,.10); border-color:rgba(239,68,68,.25); color:#ff8e96 !important; }
    .jobsync-role-badge.moderator { background:rgba(245,158,11,.10); border-color:rgba(245,158,11,.25); color:#ffc265 !important; }
    .jobsync-role-badge.member { background:rgba(34,197,94,.08); border-color:rgba(34,197,94,.20); color:#7df0a5 !important; }
    .role-admin-note { border-left:3px solid #ef4444; padding:.7rem .8rem; border-radius:10px; background:rgba(239,68,68,.055); color:#b9c1cd; font-size:.78rem; margin:.6rem 0; }
    @media (max-width:1200px) {
        .block-container { padding-right:1rem !important; }
    }
    @media (max-width:900px) {
        .block-container { padding-right:1rem !important; padding-left:1rem !important; }
    }
    @media (max-width:640px) {
        .block-container { padding-left:.65rem !important; padding-right:.65rem !important; }
        .home-center-brand { padding:2.4rem .4rem 1.6rem; }
        .home-center-title { font-size:clamp(2.8rem,16vw,4.2rem); }
        .contact-actions { flex-direction:column; width:100%; }
        .contact-btn { width:100%; text-align:center; box-sizing:border-box; }
    }


    /* ===== Compact CV folder ===== */
    .jobsync-folder-upload-card {
        display:flex; align-items:center; gap:12px; margin:.2rem 0 .55rem; padding:.85rem 1rem;
        border:1px solid rgba(255,255,255,.07); border-radius:17px;
        background:linear-gradient(135deg,rgba(19,23,30,.88),rgba(11,14,19,.78));
        box-shadow:0 14px 35px rgba(0,0,0,.16);
    }
    .jobsync-folder-upload-icon { width:34px; height:34px; display:grid; place-items:center; border-radius:11px;
        background:linear-gradient(135deg,rgba(255,77,91,.20),rgba(255,122,89,.08));
        border:1px solid rgba(255,77,91,.20); color:#ff9a98; font-size:1.2rem; font-weight:700; }
    .jobsync-folder-upload-title { color:#f5f7fa; font-size:.86rem; font-weight:850; }
    .jobsync-folder-upload-copy { color:#7f8a99; font-size:.68rem; margin-top:2px; }
    .jobsync-folder-header { display:flex; align-items:center; justify-content:space-between; gap:1rem; margin:1.15rem 0 .55rem;
        padding:.85rem 0 .7rem; border-bottom:1px solid rgba(255,255,255,.07); }
    .jobsync-folder-header .section-title { margin:0 !important; }
    .jobsync-folder-count { color:#6f7b8b; font-size:.61rem; font-weight:850; letter-spacing:.12em; }
    .jobsync-cv-row { padding:.82rem .9rem; margin:.45rem 0; border:1px solid rgba(255,255,255,.065);
        border-radius:16px; background:linear-gradient(135deg,rgba(18,22,29,.82),rgba(10,13,18,.76));
        box-shadow:0 10px 28px rgba(0,0,0,.13); transition:border-color .16s ease,transform .16s ease,background .16s ease; }
    .jobsync-cv-row:hover { border-color:rgba(255,255,255,.13); transform:translateY(-1px); }
    .jobsync-cv-row-main { min-width:0; padding:.1rem .2rem; }
    .jobsync-cv-name { color:#f3f6fa; font-size:.9rem; font-weight:850; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .jobsync-cv-position { color:#9ba6b5; font-size:.72rem; margin-top:.3rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .jobsync-cv-position span { color:#687484; font-size:.57rem; font-weight:850; letter-spacing:.1em; margin-right:.45rem; }
    .jobsync-edit-label { color:#ff858b; font-size:.59rem; font-weight:900; letter-spacing:.14em; margin:0 0 .45rem .15rem; }
    .jobsync-folder-empty { display:flex; align-items:center; gap:.75rem; padding:1rem; border:1px dashed rgba(255,255,255,.08);
        border-radius:16px; color:#7f8a99; background:rgba(12,16,22,.5); }
    .jobsync-folder-empty-icon { width:34px; height:34px; display:grid; place-items:center; border-radius:10px;
        background:#171c23; border:1px solid rgba(255,255,255,.07); color:#ff7b84; font-size:.6rem; font-weight:900; }
    .jobsync-folder-empty b { display:block; color:#eef2f6; font-size:.78rem; }
    .jobsync-folder-empty span { display:block; color:#788494; font-size:.66rem; margin-top:.12rem; }
    @media (max-width:700px) {
        .jobsync-cv-row { padding:.72rem; }
        .jobsync-cv-row [data-testid="stHorizontalBlock"] { gap:.4rem !important; }
        .jobsync-cv-row .stButton > button { padding:.35rem .45rem !important; font-size:.72rem !important; }
    }

    /* ===== Two-panel CV library workspace ===== */
    .jobsync-folder-workspace { margin-top: .9rem; }
    .jobsync-folder-pane {
        min-height: 116px; padding: 1rem 1.05rem; border: 1px solid rgba(255,255,255,.075); border-radius: 18px;
        background: linear-gradient(135deg, rgba(16,21,31,.94), rgba(12,14,24,.88));
        box-shadow: 0 18px 42px rgba(0,0,0,.14);
    }
    .jobsync-folder-upload-pane { margin-bottom: .7rem; }
    .jobsync-folder-library-pane { display:flex; align-items:flex-start; justify-content:space-between; gap:1rem; min-height:0; margin-bottom:.7rem; }
    .jobsync-folder-pane-kicker { color:#7fb5ff; font-size:.58rem; font-weight:900; letter-spacing:.14em; margin-bottom:.28rem; }
    .jobsync-folder-pane-title { color:#f3f6fb; font-size:1rem; font-weight:900; letter-spacing:-.02em; }
    .jobsync-folder-pane-title span { display:inline-grid; place-items:center; min-width:25px; height:20px; padding:0 6px; margin-left:5px; border-radius:999px; background:rgba(88,92,230,.16); border:1px solid rgba(111,113,255,.2); color:#b9bcff; font-size:.62rem; vertical-align:2px; }
    .jobsync-folder-pane-copy { color:#7f8b9d; font-size:.67rem; line-height:1.45; margin-top:.28rem; max-width:620px; }
    .jobsync-folder-live { flex:0 0 auto; color:#63e9a2; font-size:.57rem; font-weight:900; letter-spacing:.12em; padding:.35rem .55rem; border-radius:999px; background:rgba(52,211,153,.06); border:1px solid rgba(52,211,153,.15); }
    .jobsync-folder-upload-hint { color:#687587; font-size:.55rem; font-weight:850; letter-spacing:.11em; margin:.42rem 0 .5rem; }
    .jobsync-folder-upload-note { margin-top:.8rem; padding:.75rem .8rem; border:1px solid rgba(255,255,255,.055); border-radius:14px; background:rgba(8,12,18,.46); }
    .jobsync-folder-upload-note span { color:#77859a; font-size:.55rem; font-weight:900; letter-spacing:.12em; }
    .jobsync-folder-upload-note p { color:#687587; font-size:.65rem; line-height:1.4; margin:.22rem 0 0; }
    .jobsync-folder-scroll-note { color:#667386; font-size:.58rem; text-align:right; margin:-.35rem 0 .45rem; }
    .jobsync-folder-item { padding:.8rem .75rem .72rem; margin:0 0 .65rem; border:1px solid rgba(255,255,255,.065); border-radius:15px; background:linear-gradient(135deg,rgba(19,24,32,.9),rgba(10,14,21,.84)); }
    .jobsync-folder-item:last-child { margin-bottom:.1rem; }
    .jobsync-folder-item-head { display:flex; align-items:center; gap:.65rem; min-width:0; }
    .jobsync-folder-file-icon { width:38px; height:38px; flex:0 0 38px; display:grid; place-items:center; border-radius:12px; background:linear-gradient(135deg,rgba(53,179,224,.16),rgba(104,74,230,.18)); border:1px solid rgba(111,131,255,.18); color:#aab9ff; font-size:.48rem; font-weight:950; letter-spacing:.04em; }
    .jobsync-folder-item-main { min-width:0; }
    .jobsync-folder-item-name { color:#eef2f7; font-size:.78rem; font-weight:850; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .jobsync-folder-item-meta { display:flex; gap:.38rem; align-items:center; flex-wrap:wrap; color:#788596; font-size:.58rem; margin-top:.25rem; }
    .jobsync-folder-item-meta span:first-child { color:#a5afbd; }
    .jobsync-folder-item-meta i { color:#4f5b6b; font-style:normal; }
    .jobsync-folder-item .stButton > button, .jobsync-folder-item .stDownloadButton > button { min-height:34px !important; padding:.35rem .5rem !important; font-size:.64rem !important; }
    .jobsync-folder-edit-title { color:#ff8b92; font-size:.56rem; font-weight:900; letter-spacing:.13em; margin-bottom:.45rem; }
    .jobsync-folder-empty-large { margin:.1rem; min-height:150px; justify-content:center; }
    @media (max-width:900px) {
        .jobsync-folder-workspace [data-testid="stHorizontalBlock"] { gap:.8rem !important; }
    }
    @media (max-width:700px) {
        .jobsync-folder-library-pane { flex-direction:column; }
    }

    /* Fluid responsive workspace: use available width on large windows and stack cleanly on small ones. */
    .block-container {
        width:100% !important;
        max-width:none !important;
        box-sizing:border-box !important;
        padding-left:clamp(.75rem, 2vw, 2.25rem) !important;
        padding-right:clamp(.75rem, 2vw, 2.25rem) !important;
    }
    [data-testid="stHorizontalBlock"] {
        width:100% !important;
        max-width:none !important;
        box-sizing:border-box !important;
    }
    [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
        min-width:0 !important;
        box-sizing:border-box !important;
    }
    .card, .hero, .action-card, .metric-card, .chart-card, .info-card, .jobs-panel {
        width:100% !important;
        max-width:none !important;
        box-sizing:border-box !important;
    }
    @media (min-width:1401px) {
        .block-container { padding-left:clamp(1rem, 2.2vw, 3rem) !important; padding-right:clamp(1rem, 2.2vw, 3rem) !important; }
        .action-card { min-height:180px !important; }
        .metric-card { min-height:140px !important; }
    }
    @media (max-width:1200px) {
        [data-testid="stHorizontalBlock"] { gap:.8rem !important; }
    }
    @media (max-width:900px) {
        [data-testid="stHorizontalBlock"] { flex-wrap:wrap !important; }
        [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] { flex:1 1 48% !important; min-width:280px !important; }
    }
    @media (max-width:640px) {
        .block-container { padding-left:.55rem !important; padding-right:.55rem !important; }
        [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] { flex:1 1 100% !important; min-width:100% !important; }
        .hero, .action-card, .metric-card, .chart-card, .info-card, .jobs-panel { border-radius:15px !important; }
    }

    /* Fluid responsive layout: use the available window width and adapt columns. */
    [data-testid="stHorizontalBlock"] { width:100% !important; max-width:none !important; box-sizing:border-box !important; }
    [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] { min-width:0 !important; box-sizing:border-box !important; }
    .card, .hero, .action-card, .metric-card, .chart-card, .info-card, .jobs-panel { width:100% !important; max-width:none !important; box-sizing:border-box !important; }
    @media (min-width:1500px) { .block-container { padding-left:clamp(1rem,2.3vw,3rem) !important; padding-right:clamp(1rem,2.3vw,3rem) !important; } .action-card { min-height:180px !important; } .metric-card { min-height:140px !important; } }
    @media (max-width:1100px) { [data-testid="stHorizontalBlock"] { gap:.75rem !important; } }
    @media (max-width:900px) { [data-testid="stHorizontalBlock"] { flex-wrap:wrap !important; } [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] { flex:1 1 48% !important; min-width:280px !important; } }
    @media (max-width:640px) { .block-container { padding-left:.55rem !important; padding-right:.55rem !important; } [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] { flex:1 1 100% !important; min-width:100% !important; } }
    


    /* ===== PROFILE v1.7.0 — IDENTITY COMMAND CENTER ===== */

    /* v1.7.0 — unified JobSync command-center shell */
    .jobsync-commandbar{display:none!important}
    .jobsync-command-search{display:none!important}
    .jobsync-command-search kbd{margin-left:auto;padding:.18rem .42rem;border:1px solid rgba(255,255,255,.08);border-radius:7px;background:rgba(255,255,255,.03);color:#63738a;font-size:.5rem}
    .jobsync-command-live{font-size:.52rem;letter-spacing:.12em;color:#68e6b1;border:1px solid rgba(67,229,160,.16);padding:.3rem .55rem;border-radius:999px;background:rgba(67,229,160,.035)}
    .jobsync-commandbar + div [data-testid="stTextInput"] input{height:34px!important;border-radius:11px!important;background:rgba(8,17,31,.74)!important;border:1px solid rgba(90,135,190,.18)!important;color:#dce9f5!important;font-size:.72rem!important}
    .jobsync-commandbar + div [data-testid="stButton"] button{height:34px!important;min-height:34px!important;border-radius:11px!important}
    .p17-command{display:flex;align-items:center;justify-content:space-between;gap:1.2rem;margin:.15rem 0 .75rem;padding:1rem 1.15rem;border:1px solid rgba(83,188,255,.18);border-radius:22px;background:radial-gradient(circle at 90% 10%,rgba(129,75,255,.18),transparent 34%),linear-gradient(120deg,rgba(7,22,39,.98),rgba(18,11,39,.97));box-shadow:inset 0 1px 0 rgba(255,255,255,.035)}
    .p17-command-state{min-width:180px;padding:.65rem .75rem;border:1px solid rgba(255,255,255,.07);border-radius:14px;background:rgba(4,10,18,.36);text-align:right}.p17-command-state span{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;background:#f05b68;box-shadow:0 0 12px rgba(240,91,104,.6);animation:p17Pulse 1.8s ease-in-out infinite}.p17-command-state.complete span{background:#3ee7a0;box-shadow:0 0 12px rgba(62,231,160,.55)}.p17-command-state strong{font-size:.52rem;letter-spacing:.1em;color:#dbe8f3}.p17-command-state small{display:block;color:#64758a;font-size:.5rem;margin-top:.2rem}
    @keyframes p17Pulse{50%{transform:scale(1.35);opacity:.65}}
    .p17-profile-new{display:grid;grid-template-columns:minmax(300px,.78fr) minmax(0,1.22fr);gap:.8rem;margin:.75rem 0}
    .p17-person-card{overflow:hidden;border:1px solid rgba(255,255,255,.08);border-radius:23px;background:linear-gradient(150deg,rgba(9,22,37,.98),rgba(7,10,19,.98));box-shadow:0 18px 45px rgba(0,0,0,.18)}
    .p17-cover{height:72px;background:radial-gradient(circle at 75% 15%,rgba(69,216,255,.2),transparent 28%),linear-gradient(120deg,rgba(23,65,95,.72),rgba(53,30,92,.68));display:flex;align-items:flex-start;padding:.7rem .8rem;color:#7edfff;font-size:.46rem;font-weight:900;letter-spacing:.16em}.p17-person-body{padding:.75rem 1rem 1rem;text-align:center;position:relative}.p17-avatar-wrap{position:relative;width:92px;height:92px;margin:-44px auto .45rem}.p17-avatar-new{width:92px;height:92px;border-radius:30px;display:grid;place-items:center;font-size:1.6rem;font-weight:950;color:#fff;background:linear-gradient(145deg,#32d8ff,#7758ff 55%,#ef58b4);border:5px solid #0a1624;box-shadow:0 0 0 1px rgba(97,214,255,.5),0 0 38px rgba(100,91,255,.28);animation:p17Float 5s ease-in-out infinite}.p17-avatar-live{position:absolute;right:1px;bottom:1px;width:14px;height:14px;border-radius:50%;background:#43e5a1;border:3px solid #0a1624;box-shadow:0 0 13px rgba(67,229,161,.65)}
    @keyframes p17Float{50%{transform:translateY(-2px)}}
    .p17-person-status{display:inline-block;padding:.27rem .5rem;border-radius:999px;color:#9bf2c7;background:rgba(64,225,157,.055);border:1px solid rgba(64,225,157,.15);font-size:.48rem;font-weight:900;letter-spacing:.08em}.p17-person-body h2{margin:.42rem 0 .08rem;color:#f2f7fb;font-size:1.22rem;font-weight:950}.p17-person-email{color:#7a899a;font-size:.6rem}.p17-person-facts{display:grid;grid-template-columns:1fr 1fr;gap:.4rem;margin-top:.8rem;text-align:left}.p17-person-facts div{padding:.48rem .52rem;border-radius:12px;background:rgba(255,255,255,.018);border:1px solid rgba(255,255,255,.05)}.p17-person-facts span,.p17-glance-grid-new span{display:block;color:#61738a;font-size:.43rem;font-weight:900;letter-spacing:.1em}.p17-person-facts b{display:block;margin-top:.12rem;color:#dce7f1;font-size:.56rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.p17-profile-meter{margin-top:.7rem;text-align:left;padding:.55rem .6rem;border-radius:13px;background:rgba(255,255,255,.018);border:1px solid rgba(255,255,255,.05)}.p17-profile-meter>div:first-child{display:flex;justify-content:space-between;color:#718297;font-size:.45rem;font-weight:900;letter-spacing:.08em}.p17-profile-meter strong{color:#eef6fc;font-size:.6rem}.p17-meter-track{height:6px;margin:.35rem 0;border-radius:99px;background:rgba(255,255,255,.07);overflow:hidden}.p17-meter-track i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,#36d9ff,#795aff,#f05eb6);box-shadow:0 0 16px rgba(91,145,255,.35)}.p17-profile-meter small{color:#5f7084;font-size:.45rem}
    .p17-option-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.65rem}.p17-option{min-height:124px;padding:.78rem;border-radius:18px;border:1px solid rgba(255,255,255,.065);background:linear-gradient(145deg,rgba(13,25,43,.94),rgba(8,12,21,.98));display:flex;gap:.7rem;align-items:flex-start;transition:transform .2s ease,border-color .2s ease,background .2s ease}.p17-option:hover{transform:translateY(-2px);border-color:rgba(76,211,255,.25);background:linear-gradient(145deg,rgba(18,35,57,.96),rgba(9,13,23,.98))}.p17-option-icon{width:38px;height:38px;flex:0 0 38px;border-radius:13px;display:grid;place-items:center;font-size:.9rem;color:#e9f6ff;border:1px solid rgba(255,255,255,.08)}.p17-option-icon.cyan{background:rgba(48,211,255,.09)}.p17-option-icon.violet{background:rgba(126,91,255,.11)}.p17-option-icon.pink{background:rgba(239,82,178,.10)}.p17-option-icon.green{background:rgba(62,225,157,.09)}.p17-option-icon.gold{background:rgba(245,187,73,.10)}.p17-option-icon.blue{background:rgba(69,156,255,.10)}.p17-option h3{margin:.05rem 0 .2rem;color:#edf4fa;font-size:.75rem;font-weight:900}.p17-option p{margin:0;color:#728296;font-size:.54rem;line-height:1.45}.p17-glance-new{margin:.7rem 0;padding:.8rem .9rem;border-radius:19px;border:1px solid rgba(255,255,255,.065);background:linear-gradient(100deg,rgba(43,178,224,.035),rgba(117,83,255,.045))}.p17-glance-head-new{display:flex;justify-content:space-between;gap:1rem;align-items:flex-start}.p17-glance-head-new .p17-kicker{display:block}.p17-glance-head-new h3{margin:.2rem 0 .08rem;color:#eaf2f8;font-size:.82rem}.p17-glance-head-new p{margin:0;color:#68798d;font-size:.52rem}.p17-glance-status{padding:.35rem .55rem;border-radius:999px;border:1px solid rgba(63,225,159,.14);color:#7be9b8;background:rgba(63,225,159,.035);font-size:.46rem;font-weight:900;letter-spacing:.08em}.p17-glance-grid-new{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:.4rem;margin-top:.65rem}.p17-glance-grid-new>div{min-width:0;padding:.5rem;border-radius:12px;background:rgba(255,255,255,.018);border:1px solid rgba(255,255,255,.05)}.p17-glance-grid-new b{display:block;margin-top:.13rem;color:#dce7f0;font-size:.56rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.p17-bottom-cta{display:flex;justify-content:space-between;align-items:center;margin:.7rem 0;padding:.75rem .9rem;border-radius:18px;border:1px solid rgba(53,213,255,.14);background:linear-gradient(100deg,rgba(31,105,141,.12),rgba(101,68,170,.1))}.p17-bottom-cta strong{display:block;color:#edf6fb;font-size:.72rem;margin-top:.18rem}.p17-bottom-cta p{margin:.1rem 0 0;color:#708194;font-size:.52rem}
    @media(max-width:1050px){.p17-profile-new{grid-template-columns:1fr}.p17-option-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.p17-glance-grid-new{grid-template-columns:repeat(3,minmax(0,1fr))}}
    @media(max-width:760px){.p17-command{align-items:flex-start;flex-direction:column}.p17-command-state{width:100%;text-align:left}.p17-option-grid{grid-template-columns:1fr}.p17-glance-grid-new{grid-template-columns:repeat(2,minmax(0,1fr))}.p17-person-facts{grid-template-columns:1fr}.jobsync-commandbar{display:none}}
    @media(prefers-reduced-motion:reduce){.p17-avatar-new,.p17-command-state span{animation:none}.p17-option{transition:none}}

    .p17-hero{position:relative;overflow:hidden;margin:.1rem 0 .8rem;padding:1.05rem 1.15rem;border:1px solid rgba(255,255,255,.08);border-radius:24px;background:radial-gradient(circle at 86% 18%,rgba(124,83,255,.18),transparent 28%),radial-gradient(circle at 8% 90%,rgba(55,220,255,.10),transparent 28%),linear-gradient(135deg,rgba(10,22,36,.98),rgba(12,10,28,.98));box-shadow:0 24px 65px rgba(0,0,0,.22),inset 0 1px 0 rgba(255,255,255,.045)}
    .p17-hero-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(240px,.55fr);gap:1.2rem;align-items:center;position:relative;z-index:1}
    .p17-kicker{color:#75dcff;font-size:.58rem;font-weight:950;letter-spacing:.2em;text-transform:uppercase}
    .p17-title{margin:.16rem 0 .2rem;color:#f4f8fc;font-size:clamp(1.45rem,2.6vw,2.25rem);font-weight:950;letter-spacing:-.055em;line-height:1.02}
    .p17-title em{font-style:normal;background:linear-gradient(90deg,#bcefff,#a88cff,#ff8ab8);-webkit-background-clip:text;background-clip:text;color:transparent}
    .p17-copy{max-width:700px;color:#8290a1;font-size:.68rem;line-height:1.5}
    .p17-readiness{padding:.75rem .85rem;border-radius:18px;border:1px solid rgba(255,255,255,.07);background:rgba(5,10,18,.36);backdrop-filter:blur(10px)}
    .p17-readiness-top{display:flex;justify-content:space-between;gap:.5rem;align-items:center;color:#8391a2;font-size:.54rem;font-weight:850;letter-spacing:.1em;text-transform:uppercase}
    .p17-readiness-top strong{color:#eff8ff;font-size:.75rem;letter-spacing:0}
    .p17-track{height:7px;margin:.45rem 0 .35rem;border-radius:999px;background:rgba(255,255,255,.07);overflow:hidden}
    .p17-track span{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,#39d9ff,#765cff,#ff5b9a);box-shadow:0 0 18px rgba(93,155,255,.45);transition:width .7s ease}
    .p17-ready-note{display:flex;justify-content:space-between;gap:.5rem;color:#596878;font-size:.5rem}
    .p17-profile{display:grid;grid-template-columns:minmax(260px,.62fr) minmax(0,1.38fr);gap:.8rem;margin:.8rem 0}
    .p17-identity{position:relative;overflow:hidden;min-height:365px;padding:1.1rem;border-radius:25px;border:1px solid rgba(255,255,255,.08);background:radial-gradient(circle at 50% 12%,rgba(74,207,255,.12),transparent 25%),radial-gradient(circle at 92% 88%,rgba(124,83,255,.14),transparent 35%),linear-gradient(150deg,rgba(12,23,34,.98),rgba(7,10,18,.98));display:flex;flex-direction:column;align-items:center;text-align:center;justify-content:center;box-shadow:0 20px 55px rgba(0,0,0,.2)}
    .p17-identity:before{content:"IDENTITY";position:absolute;right:-.1rem;top:.4rem;color:rgba(255,255,255,.018);font-size:4.5rem;font-weight:950;letter-spacing:-.08em;pointer-events:none}
    .p17-avatar{position:relative;width:94px;height:94px;border-radius:30px;display:grid;place-items:center;color:#fff;font-size:1.55rem;font-weight:950;background:linear-gradient(145deg,#37d9ff,#7d5cff 55%,#ed4caa);border:1px solid rgba(255,255,255,.22);box-shadow:0 0 0 7px rgba(74,214,255,.045),0 0 55px rgba(89,104,255,.25),inset 0 1px 0 rgba(255,255,255,.35);animation:p17Avatar 5s ease-in-out infinite}
    .p17-avatar:after{content:"";position:absolute;inset:-8px;border:1px solid rgba(73,220,255,.2);border-radius:34px;transform:rotate(4deg)}
    .p17-avatar-dot{position:absolute;right:-2px;bottom:-2px;width:14px;height:14px;border-radius:50%;background:#39e58c;border:3px solid #0a121d;box-shadow:0 0 16px rgba(57,229,140,.65)}
    @keyframes p17Avatar{0%,100%{box-shadow:0 0 0 7px rgba(74,214,255,.045),0 0 45px rgba(89,104,255,.2),inset 0 1px 0 rgba(255,255,255,.35)}50%{box-shadow:0 0 0 9px rgba(74,214,255,.065),0 0 62px rgba(126,83,255,.28),inset 0 1px 0 rgba(255,255,255,.4)}}
    .p17-status{margin:.7rem 0 .3rem;padding:.3rem .55rem;border-radius:999px;color:#a8f0c6;background:rgba(57,229,140,.055);border:1px solid rgba(57,229,140,.14);font-size:.52rem;font-weight:900;letter-spacing:.08em}
    .p17-name{color:#f5f8fb;font-size:1.25rem;font-weight:950;letter-spacing:-.04em}
    .p17-email{color:#778597;font-size:.62rem;margin-top:.18rem}
    .p17-facts{display:grid;grid-template-columns:1fr 1fr;gap:.42rem;width:100%;margin-top:.85rem;text-align:left}
    .p17-fact{padding:.48rem .55rem;border-radius:13px;border:1px solid rgba(255,255,255,.055);background:rgba(255,255,255,.018)}
    .p17-fact span{display:block;color:#657486;font-size:.46rem;font-weight:900;letter-spacing:.1em;text-transform:uppercase}.p17-fact b{display:block;margin-top:.14rem;color:#dbe6f0;font-size:.57rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .p17-edit-primary{width:100%;margin-top:.75rem}
    .p17-cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.7rem}
    .p17-card{position:relative;min-height:176px;padding:.9rem;border-radius:21px;border:1px solid rgba(255,255,255,.07);background:linear-gradient(145deg,rgba(14,25,40,.94),rgba(9,12,21,.98));transition:transform .2s ease,border-color .2s ease,background .2s ease}
    .p17-card:hover{transform:translateY(-2px);border-color:rgba(104,205,255,.2);background:linear-gradient(145deg,rgba(17,31,49,.96),rgba(10,13,24,.98))}
    .p17-card-icon{width:38px;height:38px;border-radius:13px;display:grid;place-items:center;color:#c7f4ff;background:rgba(72,213,255,.07);border:1px solid rgba(72,213,255,.12);font-size:.95rem}
    .p17-card h3{margin:.62rem 0 .18rem;color:#edf3f8;font-size:.8rem;font-weight:900}.p17-card p{margin:0;color:#718093;font-size:.57rem;line-height:1.45;min-height:2.45em}.p17-card .p17-card-state{margin-top:.55rem;color:#9aa8b7;font-size:.52rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.p17-card button{position:absolute;right:.75rem;bottom:.7rem}
    .p17-glance{padding:.85rem .95rem;margin:.7rem 0;border-radius:20px;border:1px solid rgba(255,255,255,.065);background:linear-gradient(90deg,rgba(72,213,255,.035),rgba(125,83,255,.035))}
    .p17-glance-head{display:flex;align-items:center;justify-content:space-between;gap:.7rem}.p17-glance-title{color:#e8f0f6;font-size:.72rem;font-weight:900}.p17-glance-sub{color:#667587;font-size:.52rem;margin-top:.12rem}.p17-glance-grid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:.4rem;margin-top:.7rem}.p17-glance-item{min-width:0;padding:.48rem;border-radius:13px;background:rgba(255,255,255,.018);border:1px solid rgba(255,255,255,.05)}.p17-glance-item span{display:block;color:#617082;font-size:.46rem;font-weight:850}.p17-glance-item b{display:block;color:#dce6ef;font-size:.55rem;margin-top:.13rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .p17-cta{display:flex;align-items:center;justify-content:space-between;gap:1rem;margin:.7rem 0 .6rem;padding:.8rem .95rem;border-radius:20px;border:1px solid rgba(74,214,255,.15);background:linear-gradient(100deg,rgba(31,101,136,.15),rgba(92,63,170,.13));}.p17-cta-copy strong{display:block;color:#eaf5fb;font-size:.72rem}.p17-cta-copy span{display:block;color:#718194;font-size:.53rem;margin-top:.15rem}.p17-danger{margin-top:.7rem;padding:.7rem .85rem;border-radius:17px;border:1px solid rgba(255,77,91,.11);background:rgba(255,77,91,.025)}
    @media (max-width:1050px){.p17-profile{grid-template-columns:1fr}.p17-identity{min-height:300px}.p17-glance-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
    @media (max-width:760px){.p17-hero-grid{grid-template-columns:1fr}.p17-cards{grid-template-columns:1fr}.p17-glance-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.p17-cta{align-items:flex-start;flex-direction:column}.p17-facts{grid-template-columns:1fr}.p17-hero{padding:.9rem}.p17-title{font-size:1.5rem}}
    @media (prefers-reduced-motion:reduce){.p17-avatar{animation:none}.p17-track span,.p17-card{transition:none}}


    /* ===== APPLIED — APPLICATION OPERATIONS DECK ===== */
    .applied-hero { position:relative; overflow:hidden; margin:.05rem 0 .75rem; padding:1rem 1.05rem .9rem; border-radius:24px; border:1px solid rgba(255,255,255,.075); background:radial-gradient(circle at 84% 18%,rgba(255,77,91,.18),transparent 25%),radial-gradient(circle at 8% 88%,rgba(57,229,140,.07),transparent 25%),radial-gradient(circle at 48% 120%,rgba(83,220,255,.07),transparent 30%),linear-gradient(135deg,rgba(16,22,32,.97),rgba(7,10,17,.98)); box-shadow:0 24px 65px rgba(0,0,0,.25),inset 0 1px 0 rgba(255,255,255,.045); }
    .applied-hero::after { content:"PIPELINE"; position:absolute; right:-.15rem; bottom:-1.2rem; font-size:4.8rem; font-weight:950; letter-spacing:-.09em; color:rgba(255,255,255,.022); pointer-events:none; }
    .applied-hero-top { display:flex; align-items:flex-start; justify-content:space-between; gap:1rem; position:relative; z-index:1; }
    .applied-kicker { color:#ff8f91; font-size:.6rem; font-weight:900; letter-spacing:.2em; text-transform:uppercase; }
    .applied-title { color:#f7f9fc; font-size:clamp(1.45rem,2.6vw,2.05rem); font-weight:930; letter-spacing:-.055em; line-height:1.02; margin-top:.2rem; }
    .applied-copy { color:#7f8a99; font-size:.68rem; line-height:1.45; margin-top:.3rem; max-width:730px; }
    .applied-live { display:inline-flex; align-items:center; gap:.4rem; padding:.34rem .55rem; border-radius:999px; color:#baf2d0; background:rgba(57,229,140,.055); border:1px solid rgba(57,229,140,.14); font-size:.55rem; font-weight:900; white-space:nowrap; }
    .applied-live i { width:6px; height:6px; border-radius:50%; background:#39e58c; box-shadow:0 0 10px rgba(57,229,140,.55); }
    .applied-meta { display:flex; flex-wrap:wrap; gap:.35rem; margin-top:.65rem; }
    .applied-chip { display:inline-flex; align-items:center; gap:.3rem; padding:.29rem .5rem; border-radius:999px; color:#cfd8e3; background:rgba(255,255,255,.03); border:1px solid rgba(255,255,255,.06); font-size:.54rem; font-weight:760; }
    .applied-dot { width:5px; height:5px; border-radius:50%; background:#ff5a65; box-shadow:0 0 9px rgba(255,90,101,.55); }
    .applied-dot.green { background:#39e58c; box-shadow:0 0 9px rgba(57,229,140,.45); }
    .applied-dot.blue { background:#55dcff; box-shadow:0 0 9px rgba(85,220,255,.45); }
    .applied-overview { display:grid; grid-template-columns:1.45fr repeat(4,minmax(90px,1fr)); gap:.45rem; margin:.6rem 0 .7rem; }
    .applied-stat { position:relative; overflow:hidden; min-height:67px; padding:.62rem .68rem; border-radius:16px; border:1px solid rgba(255,255,255,.06); background:linear-gradient(145deg,rgba(16,21,29,.92),rgba(8,12,18,.94)); box-shadow:0 12px 30px rgba(0,0,0,.14),inset 0 1px 0 rgba(255,255,255,.025); }
    .applied-stat::after { content:""; position:absolute; width:55px; height:55px; right:-20px; top:-22px; border-radius:50%; background:rgba(255,255,255,.025); pointer-events:none; }
    .applied-stat-label { color:#727e8e; font-size:.53rem; font-weight:800; letter-spacing:.08em; text-transform:uppercase; }
    .applied-stat-value { color:#f4f7fa; font-size:1.25rem; font-weight:920; letter-spacing:-.045em; margin-top:.15rem; }
    .applied-stat-note { color:#626e7e; font-size:.52rem; margin-top:.05rem; }
    .applied-stat.main { background:radial-gradient(circle at 90% 10%,rgba(85,220,255,.08),transparent 35%),linear-gradient(145deg,rgba(16,25,34,.94),rgba(8,12,18,.96)); }
    .applied-stat.main .applied-stat-value { background:linear-gradient(90deg,#f7f9fc,#74e3ff); -webkit-background-clip:text; background-clip:text; color:transparent; }
    .applied-stat.interview { box-shadow:inset 0 1px 0 rgba(102,166,255,.55),0 12px 30px rgba(0,0,0,.14); }
    .applied-stat.offer { box-shadow:inset 0 1px 0 rgba(57,229,140,.6),0 12px 30px rgba(0,0,0,.14); }
    .applied-stat.rejected { box-shadow:inset 0 1px 0 rgba(255,77,91,.48),0 12px 30px rgba(0,0,0,.14); }
    .applied-command { display:flex; align-items:center; justify-content:space-between; gap:.7rem; padding:.58rem .68rem; margin-bottom:.65rem; border-radius:16px; border:1px solid rgba(255,255,255,.055); background:rgba(10,15,22,.78); box-shadow:0 12px 30px rgba(0,0,0,.12); }
    .applied-command-left { display:flex; align-items:center; gap:.55rem; min-width:0; }
    .applied-command-icon { width:30px; height:30px; border-radius:10px; display:grid; place-items:center; color:#fff; font-size:.7rem; font-weight:900; background:linear-gradient(135deg,rgba(85,220,255,.17),rgba(139,92,255,.2)); border:1px solid rgba(85,220,255,.14); }
    .applied-command-title { color:#e8edf3; font-size:.64rem; font-weight:850; }
    .applied-command-copy { color:#687485; font-size:.52rem; margin-top:.08rem; }
    .applied-command-badge { padding:.27rem .48rem; border-radius:999px; color:#9feaff; border:1px solid rgba(85,220,255,.12); background:rgba(85,220,255,.04); font-size:.51rem; font-weight:850; white-space:nowrap; }
    .applied-actions [data-testid="stButton"] > button, .applied-actions [data-testid="stDownloadButton"] > button { min-height:37px !important; border-radius:11px !important; font-size:.61rem !important; }
    .applied-actions [data-testid="stDownloadButton"] > button { background:rgba(255,255,255,.025) !important; border-color:rgba(255,255,255,.07) !important; }
    .applied-list-head { display:flex; align-items:flex-end; justify-content:space-between; gap:1rem; margin:.2rem 0 .5rem; }
    .applied-list-kicker { color:#6ee7ff; font-size:.54rem; font-weight:900; letter-spacing:.18em; text-transform:uppercase; }
    .applied-list-title { color:#eef2f6; font-size:.88rem; font-weight:880; margin-top:.12rem; }
    .applied-list-copy { color:#687586; font-size:.52rem; text-align:right; }
    .applied-card { position:relative; overflow:hidden; margin:.48rem 0; padding:.72rem .78rem .68rem; border-radius:19px; border:1px solid rgba(255,255,255,.06); background:linear-gradient(135deg,rgba(13,18,26,.94),rgba(8,12,18,.96)); box-shadow:0 16px 38px rgba(0,0,0,.16),inset 0 1px 0 rgba(255,255,255,.025); }
    .applied-card::before { content:""; position:absolute; left:0; top:0; bottom:0; width:2px; background:linear-gradient(180deg,rgba(85,220,255,.7),rgba(139,92,255,.35),transparent); opacity:.65; }
    .applied-card-top { display:flex; align-items:flex-start; justify-content:space-between; gap:.8rem; }
    .applied-card-index { color:#536171; font-size:.5rem; font-weight:900; letter-spacing:.1em; margin-bottom:.18rem; }
    .applied-card-title { color:#f0f4f8; font-size:.86rem; font-weight:880; letter-spacing:-.025em; line-height:1.15; }
    .applied-card-company { color:#8c98a7; font-size:.6rem; margin-top:.18rem; }
    .applied-card-company b { color:#cbd4de; font-weight:750; }
    .applied-card-meta { display:flex; flex-wrap:wrap; gap:.3rem; margin-top:.42rem; }
    .applied-mini-chip { display:inline-flex; align-items:center; padding:.25rem .42rem; border-radius:999px; color:#8995a4; background:rgba(255,255,255,.025); border:1px solid rgba(255,255,255,.05); font-size:.5rem; }
    .applied-card-status { text-align:right; min-width:132px; }
    .applied-card-status-label { color:#687586; font-size:.48rem; font-weight:800; letter-spacing:.1em; text-transform:uppercase; margin-bottom:.15rem; }
    .applied-card .status-pill { display:inline-flex; margin-top:.22rem; }
    .applied-card-controls { margin-top:.55rem; padding-top:.55rem; border-top:1px solid rgba(255,255,255,.045); }
    .applied-card-controls [data-testid="stSelectbox"] label { font-size:.52rem !important; color:#687586 !important; margin-bottom:.16rem !important; }
    .applied-card-controls [data-baseweb="select"] > div { min-height:35px !important; border-radius:10px !important; background:rgba(255,255,255,.022) !important; border-color:rgba(255,255,255,.06) !important; }
    .applied-card-link { margin-top:.42rem; }
    .applied-card-link [data-testid="stLinkButton"] > a, .applied-card-link [data-testid="stButton"] > button { border-radius:10px !important; font-size:.57rem !important; min-height:34px !important; }
    .applied-empty { min-height:290px; display:flex; flex-direction:column; align-items:center; justify-content:center; text-align:center; padding:2rem; border:1px dashed rgba(255,255,255,.09); border-radius:20px; background:radial-gradient(circle at 50% 40%,rgba(85,220,255,.055),transparent 35%),rgba(9,13,19,.65); }
    .applied-empty-icon { width:50px; height:50px; border-radius:16px; display:grid; place-items:center; color:#b9efff; font-size:1.2rem; background:linear-gradient(135deg,rgba(85,220,255,.10),rgba(139,92,255,.12)); border:1px solid rgba(85,220,255,.12); }
    .applied-empty-title { color:#e9eef4; font-size:.95rem; font-weight:850; margin-top:.7rem; }
    .applied-empty-copy { color:#697585; font-size:.62rem; line-height:1.45; max-width:450px; margin-top:.28rem; }
    @media (max-width:900px){.applied-overview{grid-template-columns:repeat(2,minmax(0,1fr))}.applied-stat.main{grid-column:span 2}.applied-hero-top{flex-direction:column}.applied-live{align-self:flex-start}}
    @media (max-width:560px){.applied-overview{grid-template-columns:1fr 1fr}.applied-stat.main{grid-column:span 2}.applied-command{align-items:stretch;flex-direction:column}.applied-list-head{align-items:flex-start;flex-direction:column}.applied-list-copy{text-align:left}.applied-card-top{flex-direction:column}.applied-card-status{text-align:left}.applied-hero::after{display:none}}

</style>
    """,
    unsafe_allow_html=True,
)


# ── v1.3.60 visual system ────────────────────────────────────────────────
# Inspired by the supplied dashboard reference: fixed-size information widgets,
# deep navy surfaces, violet/blue/pink accents, and a compact command-centre layout.
st.markdown(r"""
<style>
  :root {
    --v60-bg:#070b18;
    --v60-surface:#0d1326;
    --v60-surface-2:#111832;
    --v60-surface-3:#151b3a;
    --v60-border:rgba(150,164,255,.14);
    --v60-text:#f5f7ff;
    --v60-muted:#8e99b3;
    --v60-cyan:#38d8ff;
    --v60-violet:#7c5cff;
    --v60-pink:#ec4fd1;
    --v60-coral:#ff6470;
    --v60-green:#50e39a;
  }

  html, body, [data-testid="stAppViewContainer"], .stApp,
  [data-testid="stAppViewContainer"] > .main {
    background:
      radial-gradient(circle at 78% 7%, rgba(124,92,255,.12), transparent 27%),
      radial-gradient(circle at 16% 86%, rgba(56,216,255,.08), transparent 28%),
      linear-gradient(145deg,#060a15 0%,#080d1b 48%,#0a0b1d 100%) !important;
  }

  .block-container {
    max-width:1360px !important;
    margin:0 auto !important;
    padding-top:0 !important;
    padding-left:clamp(1rem,2.1vw,2.1rem) !important;
    padding-right:clamp(1rem,2.1vw,2.1rem) !important;
  }

  /* Reference-style fixed visual rail. The page can still scroll, but the
     individual information widgets retain predictable desktop dimensions. */
  section[data-testid="stSidebar"] {
    background:linear-gradient(180deg,#090e1c 0%,#080c17 100%) !important;
    border-right:1px solid rgba(115,132,196,.12) !important;
    box-shadow:18px 0 45px rgba(0,0,0,.22) !important;
  }
  section[data-testid="stSidebar"] > div:first-child { padding-top:1rem !important; }
  section[data-testid="stSidebar"] .stButton button {
    min-height:43px !important;
    border-radius:12px !important;
    background:rgba(17,24,43,.82) !important;
    border:1px solid rgba(151,165,255,.08) !important;
    box-shadow:none !important;
  }
  section[data-testid="stSidebar"] .stButton button:hover {
    background:linear-gradient(90deg,rgba(56,216,255,.10),rgba(124,92,255,.16),rgba(236,79,209,.08)) !important;
    border-color:rgba(124,92,255,.32) !important;
    transform:translateX(2px) !important;
  }

  /* Shared widget surfaces. */
  .dash-hero, .dash-card, .dash-kpi, .dash-jobs, .cvwiz-hero, .cvwiz-card,
  .mh-page-hero, .jobsync-search-command, .applied-hero, .applied-stat,
  .applied-card, .info-card, .template-summary {
    border-color:var(--v60-border) !important;
    background:
      linear-gradient(145deg,rgba(15,22,43,.96),rgba(12,14,31,.96)) !important;
    box-shadow:0 16px 42px rgba(0,0,0,.24), inset 0 1px 0 rgba(255,255,255,.025) !important;
  }

  /* Compact reference-dashboard cards. */
  .dash-hero { min-height:146px !important; padding:18px 22px !important; border-radius:20px !important; }
  .dash-kpis { grid-template-columns:repeat(6,minmax(0,1fr)) !important; gap:10px !important; }
  .dash-kpi { height:104px !important; min-height:104px !important; padding:13px 14px !important; border-radius:15px !important; }
  .dash-kpi-value { font-size:1.42rem !important; }
  .dash-main { grid-template-columns:1.15fr 1fr 1fr !important; gap:10px !important; }
  .dash-card { height:218px !important; min-height:218px !important; border-radius:16px !important; }
  .dash-card-head { padding:13px 14px 8px !important; }
  .dash-jobs { min-height:238px !important; max-height:238px !important; border-radius:16px !important; }

  /* Use the reference's colorful command accents without changing semantics. */
  .dash-hero-kicker, .mh-page-kicker, .cvwiz-kicker, .jobsync-search-kicker { color:var(--v60-cyan) !important; }
  .dash-badge.live, .jobsync-search-live {
    color:var(--v60-green) !important;
    border-color:rgba(80,227,154,.22) !important;
    background:rgba(80,227,154,.06) !important;
  }
  .dash-avatar { background:linear-gradient(135deg,var(--v60-cyan),var(--v60-violet),var(--v60-pink)) !important; }

  /* Fixed-height controls make the UI feel like a desktop product rather than
     an unbounded Streamlit form. */
  [data-testid="stTextInput"] input, [data-testid="stTextArea"] textarea,
  [data-baseweb="select"] > div, [data-testid="stFileUploaderDropzone"] {
    border-radius:11px !important;
    border-color:rgba(143,157,225,.12) !important;
    background:#0c1224 !important;
  }
  [data-testid="stTextInput"] input { min-height:43px !important; }
  [data-testid="stButton"] button, [data-testid="stLinkButton"] a {
    min-height:42px !important;
    border-radius:11px !important;
    border-color:rgba(142,156,229,.13) !important;
    background:linear-gradient(135deg,#10172b,#15152e) !important;
  }
  [data-testid="stButton"] button[kind="primary"] {
    background:linear-gradient(100deg,#2e9ed0 0%,#5b5be8 52%,#a73db1 100%) !important;
    border-color:rgba(255,255,255,.16) !important;
    box-shadow:0 10px 25px rgba(92,92,232,.18) !important;
  }

  /* CV Studio: keep the main document widget stable while its source area
     scrolls internally. */
  .cvwiz { max-width:1120px !important; }
  .cvwiz-hero { min-height:122px !important; border-radius:20px !important; }
  .cvwiz-card { min-height:0 !important; border-radius:20px !important; }
  .cvwiz-ready { width:min(760px,100%) !important; }

  /* Search result cards get a consistent viewport-friendly height. */
  .jobsync-search-command { min-height:108px !important; border-radius:20px !important; }

  /* Smooth, restrained motion matching the supplied reference. */
  .dash-kpi, .dash-card, .dash-jobs, .cvwiz-card, .applied-card {
    transition:transform .18s ease, border-color .18s ease, box-shadow .18s ease !important;
  }
  .dash-kpi:hover, .dash-card:hover, .applied-card:hover {
    transform:translateY(-2px) !important;
    border-color:rgba(124,92,255,.28) !important;
    box-shadow:0 20px 48px rgba(0,0,0,.28),0 0 0 1px rgba(124,92,255,.04) !important;
  }

  @media(max-width:1100px){
    .dash-kpis{grid-template-columns:repeat(3,minmax(0,1fr)) !important;}
    .dash-main{grid-template-columns:1fr 1fr !important;}
    .dash-card:last-child{grid-column:1/-1;}
  }
  @media(max-width:700px){
    .dash-kpis{grid-template-columns:repeat(2,minmax(0,1fr)) !important;}
    .dash-main{grid-template-columns:1fr !important;}
    .dash-card{height:auto !important;min-height:180px !important;}
    .dash-card:last-child{grid-column:auto;}
  }

  /* Equal viewport spacing: remove Streamlit's excess top breathing room so every
     page begins at the same visual baseline. */
  [data-testid="stAppViewContainer"] > .main,
  [data-testid="stAppViewContainer"] > .main > div,
  [data-testid="stAppViewContainer"] > .main .block-container {
    margin-top:0 !important;
  }

  /* ===== GLOBAL WINDOW-RESPONSIVE LAYOUT =====
     Keep every JobSync page fluid inside the available Streamlit viewport.
     Streamlit columns receive inline widths, so these rules intentionally
     override those widths at compact breakpoints and allow safe wrapping. */
  *, *::before, *::after { box-sizing:border-box !important; }
  html, body, #root, .stApp, [data-testid="stAppViewContainer"],
  [data-testid="stAppViewContainer"] > .main, .main .block-container {
    min-width:0 !important; max-width:100% !important; overflow-x:hidden !important;
  }
  .main .block-container {
    width:100% !important; margin:0 auto !important;
    padding-left:clamp:.65rem,2vw,2.5rem) !important;
    padding-right:clamp(.65rem, 2vw, 2.5rem) !important;
  }
  [data-testid="stHorizontalBlock"] {
    width:100% !important; max-width:100% !important; min-width:0 !important;
    flex-wrap:wrap !important; align-items:stretch !important;
    gap:clamp(.45rem, 1.2vw, 1rem) !important;
  }
  [data-testid="stHorizontalBlock"] > [data-testid="column"] {
    min-width:0 !important; max-width:100% !important;
  }
  [data-testid="stHorizontalBlock"] img,
  [data-testid="stHorizontalBlock"] video,
  [data-testid="stHorizontalBlock"] iframe { max-width:100% !important; }
  [data-testid="stDataFrame"], [data-testid="stTable"] { max-width:100% !important; overflow:auto !important; }
  [data-testid="stMarkdownContainer"] { min-width:0 !important; overflow-wrap:anywhere !important; }
  .stButton, .stForm, .stFormSubmitButton, .stLinkButton, .stDownloadButton,
  div[data-testid="stTextInput"], div[data-testid="stTextArea"],
  div[data-testid="stSelectbox"], div[data-testid="stMultiSelect"],
  div[data-testid="stDateInput"], div[data-testid="stNumberInput"] {
    max-width:100% !important; min-width:0 !important;
  }
  .stButton > button, .stFormSubmitButton > button, .stLinkButton > a, .stDownloadButton > button {
    max-width:100% !important; white-space:normal !important; overflow-wrap:anywhere !important;
  }
  section[data-testid="stSidebar"] {
    max-width:min(280px,30vw) !important;
  }

  /* Desktop/tablet: four-up and larger column groups become two-up before
     they become single-column, preventing cramped cards at medium widths. */
  @media (max-width:1100px) {
    .main .block-container { padding-left:1rem !important; padding-right:1rem !important; }
    [data-testid="stHorizontalBlock"]:has(> [data-testid="column"]:nth-child(4)) > [data-testid="column"] {
      flex:1 1 calc(50% - .6rem) !important; width:calc(50% - .6rem) !important;
    }
    section[data-testid="stSidebar"] { max-width:250px !important; }
  }

  /* Phones/small windows: every multi-column Streamlit row stacks cleanly.
     Nothing is allowed to force page-level horizontal scrolling. */
  @media (max-width:760px) {
    .main .block-container {
      padding-left:.7rem !important; padding-right:.7rem !important;
      padding-top:.25rem !important; padding-bottom:1.5rem !important;
    }
    [data-testid="stHorizontalBlock"] {
      flex-direction:column !important; flex-wrap:nowrap !important; gap:.65rem !important;
    }
    [data-testid="stHorizontalBlock"] > [data-testid="column"] {
      flex:1 1 100% !important; width:100% !important;
    }
    section[data-testid="stSidebar"] { max-width:220px !important; }
    .hero, .mh-page-hero, .jobsync-search-command {
      max-width:100% !important; overflow:hidden !important;
    }
    .hero h1, .mh-page-title { font-size:clamp(1.45rem,7vw,2rem) !important; }
    .jobsync-search-command-title { font-size:clamp(1.25rem,6vw,1.75rem) !important; }
    .jobsync-home-user, .jobsync-home-minimal, .cvwiz, .dash-shell {
      max-width:100% !important; width:100% !important;
    }
    .cvwiz-card { min-height:0 !important; }
  }

  @media (max-width:480px) {
    .main .block-container { padding-left:.5rem !important; padding-right:.5rem !important; }
    section[data-testid="stSidebar"] { max-width:205px !important; }
    .stButton > button, .stFormSubmitButton > button, .stLinkButton > a, .stDownloadButton > button {
      min-height:44px !important; font-size:.82rem !important;
    }
    input, textarea, select { max-width:100% !important; }
  }
</style>
""", unsafe_allow_html=True)

st.session_state.sidebar_collapsed = False
if "cv_studio_cycle" not in st.session_state:
    st.session_state.cv_studio_cycle = 0
if "folder_upload_cycle" not in st.session_state:
    st.session_state.folder_upload_cycle = 0

# Sidebar sizing/behavior lives entirely in the single JOBSYNC NAV RAIL skin
# rendered inside `with st.sidebar:` below — this used to be a separate
# always-on 280px-forcing block left over from an older click-to-toggle
# design (`sidebar_collapsed` is hardcoded False above, so its collapsed
# branch never ran, and the 280px branch fought the rail on every render).

# Responsive layout overrides. Keep the desktop navigation, but switch to an overlay
# sidebar and full-width content on smaller windows/tablets/phones. Streamlit's
# default column sizing is also allowed to wrap so controls never force horizontal scroll.
st.markdown(
    """
    <style>
    /* Tablet / small laptop: remove the fixed collaboration rail from the content width. */
    @media (max-width: 1100px) {
        .block-container {
            padding-left: 1rem !important;
            padding-right: 1rem !important;
            max-width: none !important;
        }
            position: relative !important;
            top: auto !important;
            right: auto !important;
            width: 100% !important;
            max-width: none !important;
            max-height: none !important;
            margin: 0 0 1rem !important;
        }
    }

    /* Phone / portrait: navigation becomes a real overlay instead of occupying
       part of the page. The existing JobSync Hide navigation button still controls it. */
    @media (max-width: 900px) {
        section[data-testid="stSidebar"] {
            position: fixed !important;
            left: 0 !important;
            top: 0 !important;
            bottom: 0 !important;
            width: min(300px, 88vw) !important;
            min-width: min(300px, 88vw) !important;
            max-width: min(300px, 88vw) !important;
            height: 100dvh !important;
            z-index: 100000 !important;
            overflow-y: auto !important;
            overflow-x: hidden !important;
            -webkit-overflow-scrolling: touch !important;
        }
        section[data-testid="stSidebar"] > div:first-child {
            width: 100% !important;
            min-width: 0 !important;
            max-width: none !important;
            box-sizing: border-box !important;
            padding-left: .7rem !important;
            padding-right: .7rem !important;
        }
        .main .block-container,
        .block-container {
            margin-left: 0 !important;
            padding-left: .75rem !important;
            padding-right: .75rem !important;
            padding-top: .5rem !important;
            width: auto !important;
            max-width: none !important;
        }
            position: relative !important;
            width: 100% !important;
            right: auto !important;
            top: auto !important;
            max-height: none !important;
        }
        [data-testid="stHorizontalBlock"] {
            flex-wrap: wrap !important;
            align-items: stretch !important;
            gap: .75rem !important;
        }
        [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
            min-width: min(100%, 280px) !important;
            flex: 1 1 280px !important;
            width: auto !important;
        }
        .hero,
        .mh-page-hero,
        .cv-hero {
            max-width: 100% !important;
            box-sizing: border-box !important;
        }
        .hero h1 { font-size: clamp(1.8rem, 7vw, 2.4rem) !important; }
        .page-title { font-size: clamp(1.7rem, 6vw, 2.1rem) !important; }
        .mh-page-title { font-size: clamp(1.55rem, 6vw, 2rem) !important; }
        .cv-page-title { font-size: clamp(1.55rem, 6vw, 2rem) !important; }
        .contact-card { flex-direction: column !important; align-items: stretch !important; }
        .contact-actions { width: 100% !important; }
        .contact-btn { flex: 1 1 180px !important; }
        .cv-target-top,
        .cv-command-panel,
        .cv-assistant-card { min-width: 0 !important; }
        .cv-hero { padding: 1.15rem !important; }
        .cv-title-row { align-items: flex-start !important; }
        .cv-hero-copy { margin-left: 0 !important; }
    }

    /* Narrow phones: make every column a full-width block and keep controls readable. */
    @media (max-width: 560px) {
        section[data-testid="stSidebar"] {
            width: min(290px, 91vw) !important;
            min-width: min(290px, 91vw) !important;
            max-width: min(290px, 91vw) !important;
        }
        .block-container,
        .main .block-container {
            padding-left: .55rem !important;
            padding-right: .55rem !important;
        }
        [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
            flex: 1 1 100% !important;
            min-width: 100% !important;
            width: 100% !important;
        }
        .mh-flowbar,
        .cv-stepper,
        .cv-control-grid { grid-template-columns: 1fr !important; }
        .mh-page-hero { padding: 1rem !important; border-radius: 18px !important; }
        .hero { border-radius: 16px !important; padding: 1.1rem !important; }
        .card, .job-card, .metric-card, .chart-card, .info-card, .jobs-panel,
        .template-summary { border-radius: 15px !important; }
        .stButton > button,
        .stFormSubmitButton > button,
        .stLinkButton > a,
        .stDownloadButton > button { min-height: 44px !important; }
        [data-baseweb="tab-list"] { overflow-x: auto !important; flex-wrap: nowrap !important; }
        [data-baseweb="tab"] { white-space: nowrap !important; padding: 0 .75rem !important; }
        body::after { display: none !important; }
    }
    /* Final responsive scroll/navigation safety overrides */
    html, body {
        height: 100% !important;
        overflow: hidden !important;
    }
    [data-testid="stAppViewContainer"] {
        height: 100dvh !important;
        overflow: hidden !important;
    }
    [data-testid="stAppViewContainer"] > .main,
    .stMain {
        height: 100dvh !important;
        min-height: 0 !important;
        overflow-y: auto !important;
        overflow-x: hidden !important;
        -webkit-overflow-scrolling: touch !important;
        scrollbar-gutter: stable;
    }
    [data-testid="stAppViewContainer"] .block-container {
        min-height: max-content !important;
    }

    /* Keep the desktop navigation in its own scrolling rail. */
    section[data-testid="stSidebar"] {
        max-height: 100dvh !important;
        overflow-y: auto !important;
        overflow-x: hidden !important;
        -webkit-overflow-scrolling: touch !important;
    }

    /* Small windows/tablets/phones: navigation overlays the page instead of
       stealing document width. Main content always keeps its own scroll. */
    @media (max-width: 900px) {
        section[data-testid="stSidebar"] {
            position: fixed !important;
            inset: 0 auto 0 0 !important;
            width: min(300px, 88vw) !important;
            min-width: min(300px, 88vw) !important;
            max-width: min(300px, 88vw) !important;
            height: 100dvh !important;
            max-height: 100dvh !important;
            z-index: 2147483000 !important;
            box-sizing: border-box !important;
        }
        section[data-testid="stSidebar"] > div:first-child {
            min-height: 100% !important;
            height: auto !important;
            overflow-y: visible !important;
        }
        [data-testid="stAppViewContainer"] > .main,
        .stMain,
        .main {
            width: 100% !important;
            min-width: 0 !important;
            max-width: none !important;
            margin: 0 !important;
            height: 100dvh !important;
            overflow-y: auto !important;
            overflow-x: hidden !important;
        }
        .main .block-container,
        [data-testid="stAppViewContainer"] .block-container {
            width: auto !important;
            max-width: none !important;
            margin: 0 !important;
            padding-left: .75rem !important;
            padding-right: .75rem !important;
        }
            position: relative !important;
            inset: auto !important;
            width: 100% !important;
            max-width: none !important;
            max-height: none !important;
            margin-bottom: 1rem !important;
            z-index: auto !important;
        }
    }

    @media (max-width: 560px) {
        section[data-testid="stSidebar"] {
            width: min(290px, 91vw) !important;
            min-width: min(290px, 91vw) !important;
            max-width: min(290px, 91vw) !important;
        }
        .main .block-container,
        [data-testid="stAppViewContainer"] .block-container {
            padding-left: .55rem !important;
            padding-right: .55rem !important;
        }
    }
    

    /* ================================================================
       JOBSYNC GLASS — VISUAL LAYER ONLY
       Keeps every existing page layout and interaction intact while
       giving the application a refined frosted-glass / ambient look.
       ================================================================ */
    :root {
        --glass-bg: #060b16;
        --glass-panel: rgba(12, 18, 31, .66);
        --glass-panel-strong: rgba(15, 21, 36, .80);
        --glass-border: rgba(255,255,255,.095);
        --glass-border-soft: rgba(255,255,255,.055);
        --glass-text: #f6f8ff;
        --glass-muted: #9aa7bb;
        --glass-cyan: #35d8ff;
        --glass-purple: #8b5cff;
        --glass-pink: #ec4fd1;
    }

    html, body, [data-testid="stAppViewContainer"], .stApp {
        background:
            radial-gradient(ellipse at 72% 8%, rgba(139,92,255,.18), transparent 34%),
            radial-gradient(ellipse at 18% 94%, rgba(35,210,255,.13), transparent 34%),
            linear-gradient(145deg, #030711 0%, #07101d 46%, #090615 100%) !important;
        color:var(--glass-text) !important;
    }
    [data-testid="stAppViewContainer"] { position:relative; }
    [data-testid="stAppViewContainer"]::before,
    [data-testid="stAppViewContainer"]::after {
        content:"";
        position:fixed;
        pointer-events:none;
        z-index:0;
        border-radius:50%;
        filter:blur(42px);
        opacity:.72;
        transform:translate3d(0,0,0);
    }
    [data-testid="stAppViewContainer"]::before {
        width:72vw; height:34vh; left:-18vw; top:-9vh;
        background:radial-gradient(ellipse, rgba(28,211,255,.48) 0%, rgba(28,140,255,.18) 38%, transparent 72%);
        animation:jobsyncGlassFloat 18s ease-in-out infinite alternate;
    }
    [data-testid="stAppViewContainer"]::after {
        width:68vw; height:38vh; right:-17vw; top:4vh;
        background:radial-gradient(ellipse, rgba(214,61,255,.38) 0%, rgba(116,64,255,.18) 42%, transparent 72%);
        animation:jobsyncGlassFloat 22s ease-in-out infinite alternate-reverse;
    }
    @keyframes jobsyncGlassFloat {
        from { transform:translate3d(-2%, -1%, 0) scale(1); }
        to { transform:translate3d(3%, 2%, 0) scale(1.06); }
    }

    /* Content always stays above the ambient layer. */
    [data-testid="stAppViewContainer"] > .main,
    [data-testid="stAppViewContainer"] > .main > div,
    [data-testid="stAppViewContainer"] .block-container {
        visibility:visible !important;
        opacity:1 !important;
    }
    [data-testid="stAppViewContainer"] > .main,
    [data-testid="stSidebar"],
    [data-testid="stAppViewContainer"] .block-container { position:relative !important; z-index:2 !important; }

    /* Frosted sidebar */
    section[data-testid="stSidebar"] {
        background:linear-gradient(180deg, rgba(8,13,24,.76), rgba(5,9,18,.84)) !important;
        border-right:1px solid var(--glass-border-soft) !important;
        box-shadow:18px 0 55px rgba(0,0,0,.30), inset -1px 0 0 rgba(255,255,255,.025) !important;
        backdrop-filter:blur(24px) saturate(135%);
        -webkit-backdrop-filter:blur(24px) saturate(135%);
    }
    section[data-testid="stSidebar"] .brand {
        border-bottom:1px solid var(--glass-border-soft) !important;
    }
    section[data-testid="stSidebar"] .stButton > button {
        background:rgba(255,255,255,.025) !important;
        border:1px solid transparent !important;
        border-radius:13px !important;
        color:#dce5f4 !important;
        box-shadow:inset 0 1px 0 rgba(255,255,255,.018) !important;
        backdrop-filter:blur(10px);
        -webkit-backdrop-filter:blur(10px);
    }
    section[data-testid="stSidebar"] .stButton > button:hover {
        background:linear-gradient(100deg, rgba(53,216,255,.10), rgba(139,92,255,.10)) !important;
        border-color:rgba(53,216,255,.18) !important;
        box-shadow:0 10px 30px rgba(0,0,0,.20), inset 0 1px 0 rgba(255,255,255,.05) !important;
        transform:translateX(2px);
    }
    section[data-testid="stSidebar"] .stButton > button[kind="primary"] {
        background:linear-gradient(100deg, rgba(139,92,255,.22), rgba(236,79,209,.10)) !important;
        border-color:rgba(139,92,255,.28) !important;
        box-shadow:0 10px 30px rgba(89,54,180,.18), inset 0 1px 0 rgba(255,255,255,.06) !important;
    }
    section[data-testid="stSidebar"] .stButton > button[kind="primary"]::before {
        background:linear-gradient(180deg,var(--glass-cyan),var(--glass-purple),var(--glass-pink)) !important;
        box-shadow:0 0 18px rgba(139,92,255,.60) !important;
    }

    /* Universal glass surfaces — selectors intentionally target existing components only. */
    .card, .job-card, .metric-card, .action-card, .chart-card, .info-card,
    .jobs-panel, .template-summary, .hero, .mh-page-hero,
    .jobsync-search-command, .jobsync-search-settings-card,
    .jobsync-search-empty, .cv-hero, .cv-target-card, .cv-info-tile,
    .cv-command-panel, .cv-assistant-card, .cv-upload-card, .cv-empty-card,
    .jobsync-home-chart-area, .jobsync-home-online {
        background:linear-gradient(145deg, rgba(17,25,42,.72), rgba(8,14,26,.62)) !important;
        border-color:var(--glass-border) !important;
        box-shadow:0 22px 60px rgba(0,0,0,.26), inset 0 1px 0 rgba(255,255,255,.045) !important;
        backdrop-filter:blur(20px) saturate(130%);
        -webkit-backdrop-filter:blur(20px) saturate(130%);
    }
    .card:hover, .job-card:hover, .metric-card:hover, .action-card:hover,
    .info-card:hover, .jobsync-search-settings-card:hover {
        border-color:rgba(53,216,255,.18) !important;
        box-shadow:0 28px 70px rgba(0,0,0,.30), inset 0 1px 0 rgba(255,255,255,.06) !important;
    }

    /* Inputs, selects, text areas and buttons retain their geometry but feel native/glass. */
    [data-baseweb="input"], [data-baseweb="select"], [data-baseweb="textarea"],
    div[data-testid="stTextInput"] input, div[data-testid="stTextArea"] textarea,
    div[data-testid="stNumberInput"] input {
        background:rgba(6,11,21,.58) !important;
        border-color:rgba(255,255,255,.09) !important;
        color:#f4f7ff !important;
        box-shadow:inset 0 1px 0 rgba(255,255,255,.025) !important;
    }
    div[data-testid="stTextInput"] input:focus, div[data-testid="stTextArea"] textarea:focus,
    div[data-baseweb="input"] input:focus {
        border-color:rgba(53,216,255,.38) !important;
        box-shadow:0 0 0 1px rgba(53,216,255,.16), 0 0 24px rgba(53,216,255,.08) !important;
    }
    .stButton > button, .stFormSubmitButton > button, .stDownloadButton > button,
    .stLinkButton > a {
        border-radius:12px !important;
        border:1px solid rgba(255,255,255,.085) !important;
        background:linear-gradient(135deg, rgba(255,255,255,.055), rgba(255,255,255,.018)) !important;
        color:#edf4ff !important;
        box-shadow:inset 0 1px 0 rgba(255,255,255,.045), 0 8px 24px rgba(0,0,0,.14) !important;
        backdrop-filter:blur(12px);
        -webkit-backdrop-filter:blur(12px);
        transition:transform .16s ease, border-color .16s ease, box-shadow .16s ease, background .16s ease !important;
    }
    .stButton > button:hover, .stFormSubmitButton > button:hover, .stDownloadButton > button:hover,
    .stLinkButton > a:hover {
        border-color:rgba(53,216,255,.26) !important;
        background:linear-gradient(135deg, rgba(53,216,255,.10), rgba(139,92,255,.10)) !important;
        box-shadow:0 12px 30px rgba(0,0,0,.20), 0 0 24px rgba(53,216,255,.07) !important;
        transform:translateY(-1px);
    }
    .stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {
        background:linear-gradient(135deg, rgba(53,216,255,.72), rgba(111,77,255,.84) 58%, rgba(236,79,209,.72)) !important;
        border-color:rgba(255,255,255,.18) !important;
        color:#fff !important;
        box-shadow:0 12px 34px rgba(92,78,220,.25), inset 0 1px 0 rgba(255,255,255,.22) !important;
    }

    /* Tabs / expanders / popovers */
    [data-baseweb="tab-list"] {
        background:rgba(7,12,22,.44) !important;
        border:1px solid var(--glass-border-soft) !important;
        border-radius:14px !important;
        padding:4px !important;
        backdrop-filter:blur(16px);
    }
    [data-baseweb="tab"] { color:#9aa7bb !important; border-radius:10px !important; }
    [aria-selected="true"][data-baseweb="tab"] {
        color:#fff !important;
        background:linear-gradient(135deg,rgba(53,216,255,.13),rgba(139,92,255,.16)) !important;
    }
    div[data-testid="stExpander"] {
        background:rgba(10,16,28,.54) !important;
        border:1px solid var(--glass-border) !important;
        border-radius:16px !important;
        backdrop-filter:blur(18px);
    }

    /* Home / login accents follow the new palette without changing their layout. */
    .jobsync-login-logo, .jobsync-home-avatar {
        background:linear-gradient(135deg,#35d8ff,#7c5cff 58%,#ec4fd1) !important;
        box-shadow:0 14px 34px rgba(108,80,255,.24), inset 0 1px 0 rgba(255,255,255,.22) !important;
    }
    .jobsync-login-heading, .jobsync-home-greeting-title, .home-center-title {
        background:linear-gradient(90deg,#f8fbff 0%,#b8eaff 35%,#b28cff 68%,#ff91e9 100%) !important;
        -webkit-background-clip:text !important;
        background-clip:text !important;
        color:transparent !important;
    }
    .jobsync-login-kicker, .jobsync-home-greeting-kicker, .home-center-kicker,
    .mh-page-kicker, .jobsync-search-kicker { color:#6ee7ff !important; }

    /* Keep Streamlit's scroll containers usable in the native window. */
    [data-testid="stAppViewContainer"] > .main { min-height:0 !important; }
    @media (prefers-reduced-motion: reduce) {
        [data-testid="stAppViewContainer"]::before,
        [data-testid="stAppViewContainer"]::after { animation:none !important; }
    }
</style>
    """,
    unsafe_allow_html=True,
)

# ── Local account gate ───────────────────────────────────────────────────
# JobSync accounts are completely local. A normal sign-in lasts 3 hours.
# "Remember me" uses a revocable local token rather than storing the password.
if "local_user_id" not in st.session_state:
    st.session_state["local_user_id"] = None
if "local_user_email" not in st.session_state:
    st.session_state["local_user_email"] = None
if "_auth_started_at" not in st.session_state:
    st.session_state["_auth_started_at"] = None
if "_remembered_login" not in st.session_state:
    st.session_state["_remembered_login"] = False

if not st.session_state.get("local_user_id"):
    remembered = _restore_remembered_login()
    if remembered:
        uid, remembered_email = remembered
        st.session_state["local_user_id"] = uid
        st.session_state["local_user_email"] = remembered_email
        st.session_state["_auth_started_at"] = time.time()
        st.session_state["_remembered_login"] = True

if st.session_state.get("local_user_id") and not st.session_state.get("_remembered_login"):
    started = st.session_state.get("_auth_started_at")
    if started and (time.time() - float(started)) >= AUTH_SESSION_SECONDS:
        st.session_state["local_user_id"] = None
        st.session_state["local_user_email"] = None
        st.session_state["_auth_started_at"] = None
        st.session_state["_remembered_login"] = False
        set_active_user(None)
        st.session_state["_auth_expired"] = True
        st.session_state.nav = "Login"

is_authed = bool(st.session_state.get("local_user_id"))

if is_authed:
    set_active_user(st.session_state["local_user_id"])
    state = load_state(st.session_state["local_user_id"])
else:
    set_active_user(None)
    state = json.loads(json.dumps(DEFAULT_STATE))

profile = state["profile"]
if is_authed:
    profile["email"] = profile.get("email") or str(st.session_state.get("local_user_email") or "")
profile_completed = bool(state.get("settings", {}).get("profile_completed", False))
st.session_state["_authed"] = is_authed
st.session_state["_user_id"] = st.session_state.get("local_user_id")
st.session_state["_user_email"] = st.session_state.get("local_user_email") or ""
st.session_state["_profile_completed"] = profile_completed


def _refresh_auth_state() -> None:
    global state, profile
    uid = st.session_state.get("local_user_id")
    if uid:
        set_active_user(uid)
        state = load_state(uid)
        profile = state["profile"]
        email = str(st.session_state.get("local_user_email") or "")
        if email and not profile.get("email"):
            profile["email"] = email
        st.session_state["_authed"] = True
        st.session_state["_user_id"] = uid
        st.session_state["_user_email"] = email
        st.session_state["_profile_completed"] = bool(
            state.get("settings", {}).get("profile_completed", False)
        )
    else:
        set_active_user(None)
        state = json.loads(json.dumps(DEFAULT_STATE))
        profile = state["profile"]
        st.session_state["_authed"] = False
        st.session_state["_user_id"] = None
        st.session_state["_user_email"] = ""
        st.session_state["_profile_completed"] = False


def refresh_state() -> None:
    _refresh_auth_state()


def notify_success(message: str, *args, **kwargs):
    result = st.success(message, *args, **kwargs)
    try:
        st.toast(str(message), icon="✅")
    except Exception:
        pass
    return result


def notify_error(message: str, *args, **kwargs):
    result = st.error(message, *args, **kwargs)
    try:
        st.toast(str(message), icon="⚠️")
    except Exception:
        pass
    return result

# ----------------- Identity / automatic update alerts -----------------

def _identity() -> tuple[str, str]:
    """Lightweight local identity (no login gate) — sourced from profile."""
    name = (profile.get("name") or "").strip() or "User"
    email = (profile.get("email") or "").strip()
    return name[:120], email[:240]


def _presence_session_id() -> str:
    """A per-browser-session presence identity, distinct from the account id.

    heartbeat_presence() upserts on presence_id (on_conflict="presence_id"),
    so using the account's local_user_id directly meant two sessions signed
    into the SAME account (two windows, or two testers sharing one test
    login) silently overwrote each other's row instead of both showing up —
    "two users online" would report as one. Appending a random token
    generated once per Streamlit session makes every open window/tab its
    own presence row regardless of which account it's signed into.
    """
    key = "_presence_session_token"
    if not st.session_state.get(key):
        st.session_state[key] = uuid.uuid4().hex[:12]
    account_id = str(st.session_state.get("local_user_id") or "anon")
    return f"{account_id}:{st.session_state[key]}"


def _presence_heartbeat(max_age: float = 25.0):
    if not is_authed or not presence_configured():
        return
    try:
        now = time.time()
        if (now - st.session_state.get("_presence_beat", 0.0)) < max_age:
            return
        st.session_state["_presence_beat"] = now
        name, _email = _identity()
        heartbeat_presence(
            user_id=_presence_session_id(),
            display_name=name,
            avatar_seed=name,
        )
        st.session_state["_presence_error"] = ""
    except Exception as exc:
        # Presence is a nice-to-have, never worth crashing Home over — but a
        # silently swallowed error here is indistinguishable from "everyone
        # else is just offline", which is what made this look broken with no
        # way to tell why. Keep the last error so Home can show it.
        st.session_state["_presence_error"] = f"heartbeat: {exc}"


def _refresh_online_cache(max_age: float = 20.0):
    if not is_authed or not presence_configured():
        return
    try:
        now = time.time()
        if (now - st.session_state.get("_presence_list_at", 0.0)) < max_age:
            return
        st.session_state["_presence_list_at"] = now
        st.session_state["_online_users"] = list_online_users() or []
        st.session_state["_presence_error"] = ""
    except Exception as exc:
        st.session_state["_presence_error"] = f"list: {exc}"


def _presence_initials(name: str) -> str:
    parts = [p for p in re.split(r"\s+", str(name or "User").strip()) if p]
    if not parts:
        return "U"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _render_online_panel():
    if not is_authed or not presence_configured():
        return
    users = st.session_state.get("_online_users") or []
    rows = []
    for user in users:
        name = html.escape(str(user.get("display_name") or "User"))[:60]
        initials = html.escape(_presence_initials(user.get("display_name") or "User"))
        rows.append(
            '<div class="jobsync-user-row jobsync-home-online-row">'
            f'<div class="jobsync-user-avatar" aria-hidden="true">{initials}</div>'
            '<div class="jobsync-user-copy">'
            f'<div class="jobsync-user-name">{name}</div>'
            '<div class="jobsync-user-meta" aria-label="Online"><span class="jobsync-user-status"></span></div>'
            '</div></div>'
        )
    body = ''.join(rows) or '<div class="jobsync-presence-empty">No one online right now</div>'
    st.markdown(
        '<div class="jobsync-online-panel">'
        '<div class="jobsync-presence-head"><div class="jobsync-presence-title">🟢 Online now</div>'
        f'<div class="jobsync-presence-count">{len(users)} connected</div></div>'
        f'<div>{body}</div></div>',
        unsafe_allow_html=True,
    )


def _render_live_presence():
    """Refresh shared online presence independently of the current page rerun."""
    if not is_authed or not presence_configured():
        return
    try:
        name, _email = _identity()
        uid = str(st.session_state.get("local_user_id") or "")
        if uid:
            heartbeat_presence(user_id=uid, display_name=name, avatar_seed=name)
        users = list_online_users() or []
        st.session_state["_online_users"] = users
    except Exception:
        users = st.session_state.get("_online_users") or []

    current_uid = str(st.session_state.get("local_user_id") or "")
    rows = []
    for user in users:
        online_id = str(user.get("presence_id") or "")
        raw_name = str(user.get("display_name") or "User")
        name = html.escape(raw_name)[:60]
        initials = html.escape(_presence_initials(raw_name))
        rows.append(
            '<div class="jobsync-home-online-row">'
            f'<div class="jobsync-user-avatar" aria-hidden="true">{initials}</div>'
            '<div class="jobsync-user-copy">'
            f'<div class="jobsync-user-name">{name}</div>'
            f'<div class="jobsync-user-meta" aria-label="Online"><span class="jobsync-user-status"></span></div>'
            '</div></div>'
        )
    body = "".join(rows) or (
        '<div class="jobsync-home-online-empty"><span class="jobsync-online-dot"></span>'
        'No one online right now</div>'
    )
    st.markdown(
        '<div class="jobsync-live-presence-marker"></div>'
        '<div class="jobsync-home-online jobsync-live-presence-card">'
        '<div class="jobsync-home-online-head"><div>'
        '<div class="jobsync-home-online-title"><span class="jobsync-online-dot jobsync-online-dot-lg"></span>Online now</div>'
        '<div class="jobsync-home-online-subtitle">People currently using JobSync</div></div>'
        f'<div class="jobsync-home-online-count">{len(users)}</div></div>'
        f'<div class="jobsync-home-online-list">{body}</div></div>',
        unsafe_allow_html=True,
    )


def _live_presence_widget():
    """Deprecated compatibility shim; Home owns the live presence fragment."""
    return None


def _parse_semver(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?(?!\d)", str(text))
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or "0"))


def _check_latest_release() -> str | None:
    """Return the newer release tag if one exists on GitHub, else None."""
    updater_root = _find_github_updater_root()
    if updater_root is None:
        return None
    try:
        cfg = json.loads((updater_root / "update-config.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    owner = str(cfg.get("github_owner") or "").strip()
    repo = str(cfg.get("github_repo") or "").strip()
    if not owner or not repo:
        return None
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "JobSync-Auto-Update"},
            timeout=6,
        )
        if resp.status_code != 200:
            return None
        tag = str(resp.json().get("tag_name") or "").strip()
        current = _parse_semver(APP_VERSION)
        latest = _parse_semver(tag)
        if not current or not latest:
            return None
        return tag if latest > current else None
    except Exception:
        return None


def _maybe_auto_update_check(period_hours: float = 6.0):
    """Auto-check GitHub on app start and periodically; fire a desktop toast once per new version."""
    try:
        now = time.time()
        last = st.session_state.get("_auto_update_at", 0.0)
        if last and (now - last) < period_hours * 3600:
            return
        st.session_state["_auto_update_at"] = now
    except Exception:
        return
    try:
        tag = _check_latest_release()
    except Exception:
        return
    if not tag:
        return
    st.session_state["_update_banner"] = tag
    notify_key = f"_update_notified_{tag}"
    if not st.session_state.get(notify_key):
        st.session_state[notify_key] = True
        try:
            threading.Thread(
                target=desktop_notify,
                args=(
                    "JobSync update available",
                    f"A newer version ({tag}) is ready. "
                    f"Open Settings → Software updates to download and install it.",
                ),
                daemon=True,
            ).start()
        except Exception:
            pass


def latest_final_pdf_evidence(state_data: dict, job: dict, document_type: str, max_chars: int = 18000) -> str:
    """Return the latest locally stored, user-approved PDF text for this job/document."""
    kind = "generated_cv" if document_type == "CV" else "generated_coverletter"
    title = str(job.get("title") or "").strip()
    company = str(job.get("company") or "").strip()
    for doc in reversed(state_data.get("documents", [])):
        if doc.get("kind") != kind:
            continue
        if str(doc.get("job_title") or "").strip() != title:
            continue
        if str(doc.get("company") or "").strip() != company:
            continue
        text_path = Path(str(doc.get("pdf_text_path") or ""))
        if text_path.exists():
            try:
                text = text_path.read_text(encoding="utf-8", errors="ignore").strip()
                if text:
                    return text[:max_chars]
            except OSError:
                pass
        pdf_path = Path(str(doc.get("pdf_path") or doc.get("path") or ""))
        if pdf_path.exists():
            try:
                text = extract_text(pdf_path).strip()
                if text:
                    return text[:max_chars]
            except Exception:
                pass
    return ""




# v1.6.0 — Updates center visual system
st.markdown("""
<style>
/* v1.6.0 update center: compact header, large useful content */
.updates-shell{max-width:1280px;margin:0 auto;}
.updates-panel{min-height:500px;height:100%;padding:28px;border:1px solid rgba(255,255,255,.09);border-radius:20px;background:linear-gradient(145deg,rgba(9,16,25,.97),rgba(15,12,29,.97));}
.updates-panel-head{display:flex;align-items:center;gap:14px;margin-bottom:18px;}
.updates-panel-icon{width:48px;height:48px;border-radius:14px;display:grid;place-items:center;background:linear-gradient(135deg,rgba(48,188,229,.18),rgba(124,86,230,.20));border:1px solid rgba(101,208,255,.20);font-size:1.25rem;}
.updates-panel-title{color:#edf2f8;font-size:1.12rem;font-weight:900;}
.updates-panel-sub{color:#718096;font-size:.68rem;margin-top:3px;}
.updates-status{display:flex;align-items:center;gap:9px;padding:13px 15px;border-radius:13px;background:rgba(255,255,255,.025);border:1px solid rgba(255,255,255,.06);color:#a7b2c1;font-size:.68rem;margin-bottom:12px;}
.updates-status .dot{width:8px;height:8px;border-radius:50%;background:#3bd59b;box-shadow:0 0 13px rgba(59,213,155,.35);}
.updates-empty{padding:42px 20px;text-align:center;border:1px dashed rgba(255,255,255,.10);border-radius:15px;color:#748196;font-size:.68rem;line-height:1.7;margin-bottom:14px;}
.updates-action-note{padding:12px 14px;border-radius:13px;background:rgba(98,220,255,.045);border:1px solid rgba(98,220,255,.10);color:#8090a5;font-size:.62rem;line-height:1.5;margin:12px 0;}
.updates-enabled{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 14px;border-radius:13px;background:rgba(55,212,154,.055);border:1px solid rgba(55,212,154,.14);color:#8ed9bd;font-size:.62rem;margin:12px 0;}
.updates-enabled strong{color:#d9fff0;}
.update-row{display:flex;gap:11px;align-items:flex-start;padding:12px 2px;border-bottom:1px solid rgba(255,255,255,.055);color:#aeb8c7;font-size:.67rem;line-height:1.45;}
.update-row:last-child{border-bottom:0;}
.update-dot{width:7px;height:7px;border-radius:50%;margin-top:6px;flex:none;box-shadow:0 0 10px rgba(98,220,255,.25);}
.section-kicker{color:#6f8299;font-size:.53rem;font-weight:900;letter-spacing:.16em;text-transform:uppercase;}
.software-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:4px;}
.software-card{position:relative;overflow:hidden;padding:28px;border:1px solid rgba(113,93,241,.20);border-radius:20px;background:linear-gradient(145deg,rgba(8,17,29,.97),rgba(25,12,46,.97));min-height:235px;}
.software-card:after{content:"";position:absolute;left:-25%;right:-25%;height:1px;top:0;background:linear-gradient(90deg,transparent,rgba(70,212,255,.55),rgba(181,72,204,.55),transparent);animation:updatesSweep 3.2s linear infinite;}
.software-label{color:#7f90a8;font-size:.55rem;font-weight:900;letter-spacing:.16em;text-transform:uppercase;}
.software-version{font-size:3.5rem;font-weight:950;letter-spacing:-.07em;margin-top:7px;background:linear-gradient(90deg,#f4f7fb,#6cddff,#a86cf3);-webkit-background-clip:text;background-clip:text;color:transparent;}
.software-meta{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;}
.software-chip{padding:7px 10px;border-radius:999px;background:rgba(255,255,255,.035);border:1px solid rgba(255,255,255,.07);color:#8c9aad;font-size:.53rem;font-weight:800;}
.update-check-card{display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center;}
.update-radar{width:78px;height:78px;border-radius:50%;position:relative;margin-bottom:13px;background:radial-gradient(circle,rgba(67,209,255,.42) 0 7%,rgba(67,209,255,.08) 9% 22%,transparent 23%);border:1px solid rgba(80,214,255,.28);box-shadow:0 0 0 10px rgba(80,214,255,.035),0 0 0 22px rgba(123,87,235,.025);animation:updatesRadar 2.4s ease-in-out infinite;}
.update-radar:after{content:"";position:absolute;inset:8px;border:1px dashed rgba(112,104,255,.45);border-radius:50%;animation:updatesSpin 5s linear infinite;}
.update-check-title{color:#eef3f9;font-size:1rem;font-weight:900;}
.update-check-copy{color:#718096;font-size:.63rem;line-height:1.5;margin-top:5px;max-width:360px;}
.updates-result{margin-top:16px;padding:15px 16px;border-radius:14px;border:1px solid rgba(64,215,165,.18);background:rgba(16,34,31,.58);color:#9adbc5;font-size:.63rem;}
@keyframes updatesSweep{to{transform:translateX(120%)}}
@keyframes updatesRadar{50%{transform:scale(1.04);box-shadow:0 0 0 14px rgba(80,214,255,.035),0 0 0 28px rgba(123,87,235,.02)}}
@keyframes updatesSpin{to{transform:rotate(360deg)}}
@media(max-width:900px){.software-grid{grid-template-columns:1fr}.updates-panel{min-height:auto;padding:20px}}
@media(prefers-reduced-motion:reduce){.software-card:after,.update-radar,.update-radar:after{animation:none!important}}
</style>
""", unsafe_allow_html=True)

# ---------------- Navigation helpers ----------------
BASE_PAGES = ["Home", "Dashboard", "New Search", "Applied Jobs", "Updates", "CV & Cover Letter", "Folders", "Profile", "Settings"]

def custom_sections() -> list[dict]:
    raw = state.get("settings", {}).get("custom_sections") or []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name and name not in BASE_PAGES and name not in {x.get("name") for x in out}:
            out.append({"name": name[:50], "description": str(item.get("description") or "").strip()[:500], "icon": str(item.get("icon") or "▣")[:4]})
    return out

PAGES = BASE_PAGES + [x["name"] for x in custom_sections()]

JOB_SEARCH_MODES = {
    "Free APIs & public sources (no Apify)": "free",
    "Apify Actors": "apify",
    "Free first + Apify fallback": "both",
}
JOB_SEARCH_MODE_LABELS = {v: k for k, v in JOB_SEARCH_MODES.items()}

JOB_SOURCE_PRESETS = {
    "Open source": "open",
    "LinkedIn": "linkedin",
    "Apify": "apify",
}
OPEN_SOURCE_DEFAULTS = [name for name in FREE_SOURCE_NAMES if name != "LinkedIn"]

if "nav" not in st.session_state:
    st.session_state.nav = "Home"


def go(page: str):
    st.session_state.nav = page
    st.rerun()


# ── Live job map helpers (v1.6.0) ─────────────────────────────────────────────
@st.cache_data(ttl=7 * 24 * 60 * 60, show_spinner=False)
def _geocode_job_location(location: str) -> tuple[float, float] | None:
    """Geocode a public job location without requiring an API key."""
    raw = re.sub(r"\s+", " ", str(location or "")).strip()
    if not raw:
        return None
    low = raw.lower()
    if low in {"remote", "worldwide", "anywhere", "home office", "hybrid"}:
        return None
    known = {
        "hannover": (52.3759, 9.7320), "hanover": (52.3759, 9.7320),
        "berlin": (52.5200, 13.4050), "hamburg": (53.5511, 9.9937),
        "münchen": (48.1351, 11.5820), "munich": (48.1351, 11.5820),
        "köln": (50.9375, 6.9603), "cologne": (50.9375, 6.9603),
        "frankfurt": (50.1109, 8.6821), "stuttgart": (48.7758, 9.1829),
        "düsseldorf": (51.2277, 6.7735), "dusseldorf": (51.2277, 6.7735),
        "dortmund": (51.5136, 7.4653), "leipzig": (51.3397, 12.3731),
        "dresden": (51.0504, 13.7373), "bremen": (53.0793, 8.8017),
        "nürnberg": (49.4521, 11.0767), "nuremberg": (49.4521, 11.0767),
        "essen": (51.4556, 7.0116), "amsterdam": (52.3676, 4.9041),
        "paris": (48.8566, 2.3522), "london": (51.5074, -0.1278),
        "zurich": (47.3769, 8.5417), "zürich": (47.3769, 8.5417),
        "vienna": (48.2082, 16.3738), "wien": (48.2082, 16.3738),
        "prague": (50.0755, 14.4378), "brussels": (50.8503, 4.3517),
        "copenhagen": (55.6761, 12.5683), "warsaw": (52.2297, 21.0122),
    }
    for key, coords in known.items():
        if low == key or low.startswith(key + ","):
            return coords
    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": raw, "format": "jsonv2", "limit": 1},
            headers={"User-Agent": "JobSync/1.7.0 (live job map)", "Accept-Language": "de,en;q=0.8"},
            timeout=8,
        )
        response.raise_for_status()
        items = response.json()
        if items:
            lat, lon = float(items[0]["lat"]), float(items[0]["lon"])
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
    except Exception:
        pass
    return None


def _job_map_html(jobs: list[dict], search_location: str = "") -> tuple[str, int, int]:
    '''Build a dependency-free interactive slippy map for the desktop WebView.

    The previous implementation depended on Leaflet being fetched from a CDN.
    That is unreliable inside packaged Windows WebViews, so this map implements
    the small amount of tile/pan/zoom/marker behavior JobSync needs directly.
    OpenStreetMap tiles remain the geographic background; when tiles are
    unavailable, the markers and job popups still work on the fallback canvas.
    '''
    by_location: dict[str, list[dict]] = defaultdict(list)
    for job in jobs:
        location = re.sub(r"\s+", " ", str(job.get("location") or "")).strip()
        if location:
            by_location[location].append(job)

    ranked = sorted(by_location.items(), key=lambda item: (-len(item[1]), item[0].lower()))
    location_coords: dict[str, tuple[float, float]] = {}
    for location, job_rows in ranked[:18]:
        coords = _geocode_job_location(location)
        if coords:
            location_coords[location] = coords

    fallback_center = _geocode_job_location(search_location) if search_location else None
    points = [(loc, location_coords[loc], job_rows) for loc, job_rows in ranked if loc in location_coords]
    mapped_jobs = sum(len(job_rows) for _loc, _coords, job_rows in points)
    total_with_location = sum(len(job_rows) for _loc, job_rows in ranked)
    skipped_jobs = max(0, total_with_location - mapped_jobs)

    if points:
        center = [
            sum(coords[0] for _loc, coords, _rows in points) / len(points),
            sum(coords[1] for _loc, coords, _rows in points) / len(points),
        ]
        zoom = 5 if len(points) > 5 else (7 if len(points) > 1 else 10)
    elif fallback_center:
        center, zoom = list(fallback_center), 9
    else:
        center, zoom = [51.1657, 10.4515], 5

    map_points = []
    for location, coords, job_rows in points:
        listings = []
        for job in job_rows[:30]:
            listings.append({
                "title": str(job.get("title") or "Untitled role"),
                "company": str(job.get("company") or "Unknown company"),
                "posted": str(job.get("posted_date") or ""),
                "url": str(job.get("url") or ""),
                "source": str(job.get("source") or job.get("actor") or "Job"),
            })
        source_counts = Counter(str(j.get("source") or j.get("actor") or "Job") for j in job_rows)
        map_points.append({
            "location": location,
            "lat": coords[0],
            "lon": coords[1],
            "count": len(job_rows),
            "sources": dict(source_counts),
            "jobs": listings,
            "more": max(0, len(job_rows) - len(listings)),
        })

    payload = json.dumps(map_points, ensure_ascii=False).replace("</", "<\\/")
    center_json = json.dumps(center)
    map_html = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
html,body{margin:0;padding:0;width:100%;height:100%;overflow:hidden;background:#0a1422;font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#eef4fb}
#map{position:relative;width:100%;height:100%;min-height:420px;overflow:hidden;background:#0b1726;touch-action:none;user-select:none}
#fallback-map{position:absolute;inset:0;overflow:hidden;background:#dfe8e1;pointer-events:none}
#fallback-svg{position:absolute;display:block;overflow:visible}
#fallback-svg .country{fill:#d6e2d7;stroke:#a7b8a9;stroke-width:.8;vector-effect:non-scaling-stroke}
#fallback-svg .country-label{fill:#5d6d61;font-size:4px;font-weight:650;paint-order:stroke;stroke:#eef3ee;stroke-width:4px;stroke-linejoin:round;opacity:.9}
#tiles{position:absolute;inset:0;overflow:hidden;background:transparent}
.tile{position:absolute;width:256px;height:256px;image-rendering:auto;pointer-events:none}
#fallback-grid{position:absolute;inset:0;pointer-events:none;background:linear-gradient(rgba(75,145,156,.08) 1px,transparent 1px),linear-gradient(90deg,rgba(75,145,156,.08) 1px,transparent 1px);background-size:64px 64px;opacity:.18}
#markers{position:absolute;inset:0;pointer-events:none}
.marker{position:absolute;transform:translate(-50%,-50%);pointer-events:auto;border:0;padding:0;background:none;cursor:pointer}
.job-marker{display:flex;align-items:center;justify-content:center;width:38px;height:38px;border-radius:50%;background:#102338;border:2px solid #67e8f9;box-shadow:0 0 0 5px rgba(103,232,249,.12),0 8px 20px rgba(0,0,0,.35);color:#f7fbff;font-weight:800;font-size:12px;transition:transform .16s ease,box-shadow .16s ease}
.marker:focus-visible .job-marker,.marker:hover .job-marker{transform:scale(1.1);box-shadow:0 0 0 7px rgba(103,232,249,.18),0 10px 24px rgba(0,0,0,.4);outline:none}
#popup{position:absolute;z-index:20;display:none;width:340px;max-width:calc(100% - 28px);max-height:calc(100% - 28px);overflow:auto;box-sizing:border-box;padding:14px;border:1px solid #33465c;border-radius:14px;background:rgba(7,16,28,.97);box-shadow:0 18px 45px rgba(0,0,0,.42);color:#eaf2fa}
.popup-close{position:absolute;right:8px;top:7px;width:34px;height:34px;border:0;border-radius:9px;background:#16263a;color:#dbe7f3;font-size:18px;cursor:pointer}
.popup-head{padding-right:36px;padding-bottom:9px;border-bottom:1px solid #2b3a4d;margin-bottom:4px}.popup-location{font-size:15px;font-weight:800;color:#f8fbff}.popup-meta{font-size:11px;color:#73dff7;margin-top:3px}.job{padding:9px 0;border-bottom:1px solid #263447}.job:last-child{border-bottom:0}.job-title{font-weight:750;color:#fff}.job-company{font-size:12px;color:#aebaca;margin-top:2px}.job-source{font-size:10px;color:#7f91a6;margin-top:2px}.job a{display:inline-block;margin-top:5px;color:#70ddf5;text-decoration:none;font-weight:700}.job a:hover{text-decoration:underline}.more{font-size:11px;color:#9aabba;margin-top:8px}
.status{position:absolute;z-index:10;top:10px;left:50%;transform:translateX(-50%);padding:7px 11px;border:1px solid #26364b;border-radius:999px;background:rgba(7,16,28,.92);color:#9fb0c4;font-size:11px;pointer-events:none;white-space:nowrap}
.map-btn{position:absolute;z-index:12;left:10px;width:36px;height:36px;border:1px solid #2d4055;background:rgba(9,22,36,.94);color:#eaf4ff;font-size:21px;line-height:1;border-radius:9px;cursor:pointer;box-shadow:0 5px 16px rgba(0,0,0,.25)}
#zoom-in{top:48px;border-radius:9px 9px 4px 4px}#zoom-out{top:85px;border-radius:4px 4px 9px 9px}
.attribution{position:absolute;z-index:8;right:0;bottom:0;padding:3px 6px;background:rgba(7,16,28,.82);font-size:10px;color:#9aa9bb}.attribution a{color:#78dff8;text-decoration:none}
.error{display:flex;align-items:center;justify-content:center;height:100%;padding:24px;box-sizing:border-box;text-align:center;color:#d9e5f0;font-size:13px}
@media (max-width:600px){#popup{width:290px}.map-btn{left:8px}}
</style>
</head>
<body>
<div id="map">
  <div id="fallback-map"><svg id="fallback-svg" viewBox="0 0 2048 2048" preserveAspectRatio="none" aria-label="Geographic map of Europe"><rect width="2048" height="2048" fill="#dfe8e1"/><g class="countries"><path class="country" data-name="Russia" d="M2040.7,439.6L2048.0,432.2L2048.0,444.2L2041.8,445.1L2040.7,439.6ZM1303.3,725.3L1300.7,730.2L1295.2,731.5L1289.6,739.9L1294.7,747.4L1294.2,752.6L1300.4,761.7L1297.0,764.8L1296.0,766.7L1293.5,766.2L1289.6,761.6L1284.4,759.5L1282.7,756.4L1277.4,754.8L1273.9,756.0L1272.9,754.5L1265.2,750.8L1252.0,748.2L1251.3,749.1L1244.0,742.5L1237.6,739.5L1232.6,734.7L1236.8,733.5L1241.5,726.6L1238.3,723.4L1246.7,720.0L1246.6,718.1L1241.4,719.5L1241.6,715.8L1244.6,713.4L1250.1,712.8L1251.0,709.9L1249.7,705.2L1252.0,700.7L1251.9,698.1L1243.6,695.2L1240.2,695.3L1236.7,691.2L1232.4,692.6L1225.1,689.4L1225.3,687.7L1223.2,683.7L1218.7,683.3L1218.2,680.5L1219.7,678.6L1216.0,673.4L1208.4,673.8L1207.0,675.9L1204.8,675.5L1202.1,666.4L1203.2,665.6L1207.8,665.9L1210.0,663.8L1208.4,661.3L1204.5,659.6L1204.9,657.8L1202.5,656.1L1199.0,649.6L1200.2,647.0L1199.6,642.3L1194.1,639.9L1191.1,641.1L1190.3,638.6L1184.3,636.0L1182.5,629.9L1182.0,624.9L1179.2,622.4L1181.7,619.1L1180.0,609.0L1184.0,602.6L1183.2,600.7L1189.6,594.4L1183.7,589.0L1201.2,566.9L1203.3,560.6L1194.9,551.9L1197.2,543.5L1192.1,533.7L1195.9,522.0L1189.3,505.8L1194.5,494.7L1185.8,484.5L1186.7,473.6L1200.9,465.6L1206.8,459.9L1216.1,469.8L1231.7,473.6L1253.2,491.1L1257.6,498.3L1258.0,508.0L1251.6,515.5L1242.4,519.3L1217.0,508.5L1212.8,510.3L1222.1,520.6L1222.8,540.8L1234.6,548.1L1235.3,541.8L1231.9,536.1L1235.5,531.0L1249.2,539.4L1254.0,536.1L1250.2,526.2L1263.5,512.5L1268.7,513.3L1274.0,518.3L1277.3,508.5L1272.6,499.8L1275.4,490.9L1271.2,481.3L1287.1,486.3L1290.4,494.8L1283.2,496.6L1283.2,504.8L1287.7,509.8L1296.5,506.7L1297.9,497.3L1329.6,476.8L1333.9,477.6L1328.3,487.0L1335.3,488.6L1339.4,483.4L1350.1,483.0L1358.5,476.5L1365.0,485.9L1371.5,475.5L1365.5,466.2L1368.5,460.8L1385.3,465.8L1393.1,470.8L1413.8,488.7L1417.6,480.6L1411.8,472.3L1411.6,468.9L1404.8,467.3L1406.6,459.5L1403.6,446.3L1403.4,440.8L1413.9,424.6L1417.7,407.5L1421.9,403.7L1436.9,408.8L1438.1,419.3L1432.7,434.1L1436.3,439.7L1438.1,451.8L1436.8,474.3L1443.1,483.9L1440.6,494.0L1429.5,514.7L1436.0,516.8L1438.3,511.7L1444.5,508.0L1446.0,500.8L1451.0,493.7L1447.6,485.1L1450.3,474.8L1444.1,473.5L1442.7,464.5L1447.3,447.7L1439.9,433.4L1450.0,421.1L1448.7,407.7L1451.6,407.3L1454.6,417.8L1452.3,435.4L1458.4,438.6L1455.8,425.7L1465.3,418.4L1477.1,417.4L1487.6,427.9L1482.6,412.5L1482.0,391.6L1491.9,387.5L1505.6,388.4L1517.9,385.7L1513.3,374.8L1519.9,360.5L1526.4,359.9L1537.5,348.7L1552.5,345.7L1554.4,339.3L1569.3,337.1L1574.0,342.4L1586.8,329.7L1597.2,330.1L1598.8,319.5L1604.2,308.6L1617.6,297.9L1627.4,306.4L1619.7,312.8L1632.5,316.7L1634.1,328.9L1639.3,322.9L1655.9,323.3L1668.7,335.1L1673.3,343.9L1671.9,355.8L1650.6,374.4L1646.4,380.7L1661.8,388.8L1667.0,384.9L1669.9,397.9L1672.4,392.7L1681.5,389.5L1699.7,392.8L1701.1,402.1L1724.9,405.0L1725.2,389.8L1746.4,393.3L1755.5,403.7L1758.2,415.9L1754.8,423.7L1761.9,437.9L1770.9,445.0L1776.4,426.4L1785.5,434.5L1795.2,429.7L1806.2,435.2L1810.4,430.1L1819.7,432.7L1815.6,415.6L1823.1,407.4L1874.5,419.7L1879.3,430.5L1894.2,444.0L1917.2,440.7L1928.5,443.6L1933.3,450.7L1932.6,462.9L1939.6,467.6L1947.2,464.2L1957.3,463.8L1968.0,467.0L1978.8,465.2L1988.7,479.4L1995.8,474.4L1991.2,464.1L1993.7,456.7L2011.8,461.4L2023.7,460.4L2040.0,468.2L2048.0,475.2L2048.0,533.2L2040.6,539.2L2033.3,538.2L2038.4,545.2L2041.8,555.8L2044.4,559.2L2045.1,564.3L2043.6,567.6L2033.0,564.9L2017.1,574.0L2012.0,575.4L1995.1,590.9L1993.0,596.1L1984.9,588.2L1970.0,597.1L1967.4,592.9L1962.0,597.8L1954.4,596.2L1952.5,603.6L1945.7,614.2L1945.9,618.6L1952.4,620.9L1951.6,636.1L1946.3,636.5L1943.9,644.9L1946.3,649.2L1936.3,654.2L1934.3,665.2L1925.9,667.5L1924.2,677.0L1916.0,685.5L1913.9,679.2L1908.2,644.0L1911.0,629.8L1915.8,623.6L1916.1,618.6L1924.9,616.2L1944.9,590.8L1955.1,581.5L1959.7,564.5L1952.8,565.6L1949.3,575.6L1934.9,588.5L1930.3,574.0L1915.6,578.1L1901.3,597.5L1906.0,604.3L1884.5,608.4L1884.9,600.3L1876.1,598.6L1869.1,604.1L1851.7,602.2L1832.9,605.5L1792.7,650.5L1801.7,651.7L1804.5,657.8L1810.0,659.9L1813.6,655.1L1819.9,655.7L1828.1,666.3L1828.3,674.3L1823.8,683.5L1823.4,694.2L1820.8,708.1L1812.2,720.3L1810.3,726.1L1791.3,749.4L1783.7,754.0L1780.1,754.1L1776.5,750.3L1768.9,756.0L1768.0,758.6L1767.2,757.2L1767.2,753.3L1770.1,753.1L1770.9,743.8L1769.4,737.0L1774.3,734.1L1781.2,735.6L1785.0,727.6L1787.0,718.6L1789.2,715.5L1792.1,707.8L1782.7,710.4L1777.8,713.7L1769.2,713.7L1766.9,705.7L1760.1,699.5L1750.2,696.7L1748.1,688.0L1740.5,669.1L1735.5,665.6L1727.0,662.8L1712.4,664.8L1707.7,669.5L1710.8,671.7L1710.9,676.8L1707.7,679.8L1702.6,689.4L1702.6,693.3L1694.6,698.9L1687.8,695.6L1681.0,696.3L1678.0,693.3L1674.6,692.4L1666.3,698.6L1653.5,702.2L1641.1,700.9L1637.7,696.4L1632.1,692.1L1626.4,691.0L1613.8,693.8L1605.7,690.0L1604.6,683.3L1592.8,679.9L1586.4,676.0L1580.5,685.5L1582.8,690.8L1577.3,697.0L1569.1,694.8L1563.4,694.4L1559.6,690.3L1553.7,690.2L1548.7,687.4L1540.1,691.6L1529.2,699.2L1521.0,701.5L1518.0,696.1L1510.6,697.3L1508.2,693.5L1504.2,691.8L1501.5,686.6L1498.4,685.0L1490.2,687.3L1482.3,682.1L1479.3,686.8L1466.6,663.3L1459.3,655.9L1461.4,652.8L1447.2,661.9L1441.7,662.5L1442.2,657.2L1434.9,653.9L1428.9,656.3L1427.1,646.1L1416.9,643.9L1411.8,648.1L1397.6,651.7L1394.8,654.1L1373.5,657.5L1370.9,660.8L1375.0,667.3L1369.5,669.8L1370.6,672.3L1365.1,676.9L1374.4,683.2L1372.9,687.4L1365.0,687.0L1363.3,689.7L1356.0,685.0L1347.0,685.2L1341.0,689.0L1321.7,679.1L1312.8,679.3L1301.1,689.2L1300.4,695.7L1294.5,690.5L1290.0,700.2L1291.6,702.0L1288.3,708.6L1293.2,714.3L1297.4,714.1L1301.0,719.7L1300.4,724.0L1303.3,725.3ZM1557.5,194.5L1569.8,186.2L1580.9,204.5L1594.0,237.0L1592.5,264.7L1580.1,268.3L1564.3,259.8L1554.8,248.2L1550.5,225.2L1542.7,218.5L1557.5,194.5ZM1604.8,250.7L1623.5,269.6L1621.8,281.2L1589.7,291.9L1600.1,254.1L1604.8,250.7ZM1813.8,337.1L1828.8,338.2L1849.4,350.5L1844.9,367.0L1823.9,366.4L1814.5,371.5L1803.2,357.3L1806.3,341.6L1813.8,337.1ZM1856.6,352.0L1881.5,361.3L1874.9,369.9L1865.8,367.9L1855.3,359.3L1856.6,352.0ZM1819.7,397.2L1825.1,389.2L1832.2,387.3L1840.3,395.1L1840.9,400.3L1819.7,397.2ZM1279.1,210.0L1298.9,203.2L1300.0,212.6L1303.3,204.2L1308.7,198.3L1317.1,206.2L1314.9,211.5L1302.2,218.6L1301.4,224.1L1294.7,229.6L1288.5,221.7L1291.8,211.1L1279.1,210.0ZM1153.3,654.4L1142.9,654.5L1135.8,653.4L1137.1,649.1L1145.0,645.9L1153.5,649.2L1152.9,651.9L1153.3,654.4ZM1328.4,389.5L1342.0,371.2L1340.5,361.3L1372.0,334.4L1390.9,329.9L1400.7,320.8L1411.7,317.5L1415.7,327.3L1411.9,334.8L1374.3,357.3L1356.7,378.0L1339.3,416.5L1340.4,431.7L1351.3,446.1L1348.0,447.7L1329.4,445.4L1327.9,437.7L1317.6,432.9L1316.7,423.1L1322.5,419.1L1322.3,408.8L1333.6,392.0L1328.4,389.5ZM1835.5,654.0L1839.0,669.6L1838.8,678.7L1841.2,687.9L1846.9,703.5L1838.5,700.7L1835.0,713.1L1840.5,721.7L1840.4,727.5L1836.1,722.5L1832.3,728.9L1831.3,722.0L1831.9,713.8L1831.3,704.5L1832.6,698.0L1832.8,686.1L1829.5,677.1L1830.0,664.3L1835.3,659.9L1833.0,655.4L1835.5,654.0ZM28.9,502.0L28.4,511.0L32.2,514.5L30.9,504.1L46.3,506.2L57.5,519.6L51.8,525.6L42.5,527.0L42.4,540.2L40.1,542.9L34.7,542.5L30.4,537.9L22.8,534.0L21.6,528.1L15.8,525.9L9.3,527.7L6.2,522.9L7.5,517.7L0.7,521.0L3.2,527.5L0.0,533.2L0.0,475.2L13.9,487.1L28.9,502.0ZM7.4,443.2L0.0,444.2L0.0,432.2L5.6,431.4L13.8,436.6L13.3,439.0L7.4,443.2ZM1214.2,728.8L1215.7,726.8L1221.6,728.9L1223.2,731.4L1226.0,733.4L1231.8,732.9L1230.7,735.8L1224.5,737.2L1216.8,741.8L1213.6,740.2L1214.8,736.4L1208.6,734.1L1209.6,732.5L1215.1,729.8L1214.2,728.8Z"/><path class="country" data-name="Norway" d="M1110.1,240.4L1112.3,229.4L1120.7,228.2L1146.6,262.5L1132.2,273.9L1129.1,294.4L1124.1,299.5L1121.4,320.8L1114.5,321.8L1102.3,306.2L1107.5,296.9L1098.9,289.0L1087.8,265.0L1083.4,241.1L1098.9,229.6L1102.0,240.9L1110.1,240.4ZM1200.9,465.6L1186.7,473.6L1189.1,462.2L1181.8,455.6L1172.9,461.2L1170.1,473.1L1164.7,480.1L1158.6,476.3L1151.2,477.1L1144.9,468.7L1141.5,472.9L1137.9,473.6L1137.1,483.9L1126.4,481.4L1124.9,490.0L1119.4,489.9L1110.0,516.5L1101.1,535.8L1103.2,540.4L1101.2,545.6L1095.6,545.3L1091.9,557.3L1092.2,573.7L1095.9,579.7L1094.0,593.4L1089.2,601.1L1086.7,607.5L1082.9,600.7L1071.7,613.5L1064.1,616.0L1056.2,610.5L1054.2,598.6L1052.4,571.6L1057.6,563.7L1072.7,553.2L1083.9,539.8L1108.0,493.0L1133.1,461.4L1145.6,454.1L1155.0,455.0L1163.6,440.8L1174.0,441.5L1184.2,438.0L1202.0,450.7L1194.7,455.2L1200.9,465.6ZM1179.9,228.1L1171.5,245.3L1155.0,249.0L1138.2,243.8L1137.2,235.0L1129.0,234.5L1122.8,219.3L1140.4,209.7L1148.6,218.0L1154.4,207.6L1168.8,216.3L1179.9,228.1ZM1164.7,293.7L1151.9,304.6L1141.9,298.4L1145.8,291.5L1142.4,282.7L1154.2,277.0L1156.4,287.5L1164.7,293.7Z"/><path class="country" data-name="France" d="M730.1,1000.3L726.8,1005.6L725.0,1009.7L722.8,1011.9L720.1,1012.3L719.3,1010.7L718.1,1010.5L716.3,1012.0L713.8,1010.8L715.3,1008.4L716.8,1003.4L714.5,1000.0L714.1,996.1L717.0,991.2L723.2,993.2L729.2,998.0L730.1,1000.3ZM1059.2,699.3L1061.9,701.6L1070.1,703.2L1067.2,709.1L1066.5,715.1L1064.9,716.6L1062.3,715.8L1062.5,717.9L1058.3,722.6L1058.3,726.4L1061.0,725.1L1062.9,728.7L1062.7,731.0L1064.4,734.0L1062.4,736.5L1063.9,742.7L1066.9,743.7L1066.3,747.1L1061.1,751.5L1049.9,749.4L1041.6,752.0L1041.0,756.6L1034.4,757.6L1028.0,754.1L1025.9,755.8L1015.5,752.3L1013.2,749.2L1016.1,744.5L1017.2,728.5L1011.3,719.8L1007.1,715.6L998.4,712.3L997.9,706.1L1005.3,704.2L1014.8,706.4L1013.0,696.5L1018.4,700.3L1031.6,693.4L1033.3,686.1L1038.3,684.3L1039.1,687.5L1041.8,687.6L1048.4,695.4L1051.3,694.7L1057.6,699.5L1059.2,699.3ZM1073.8,755.4L1077.4,752.5L1078.4,759.1L1076.5,765.0L1073.9,763.4L1072.6,758.3L1073.8,755.4Z"/><path class="country" data-name="Armenia" d="M1288.6,784.4L1286.5,784.6L1284.2,779.2L1281.7,779.2L1280.0,777.3L1278.8,777.5L1276.6,775.3L1272.4,773.4L1272.9,769.8L1271.9,767.2L1279.8,766.0L1281.0,768.0L1283.2,769.3L1282.0,771.1L1285.1,773.7L1283.5,776.1L1288.4,779.3L1288.6,784.4Z"/><path class="country" data-name="Sweden" d="M1086.7,607.5L1089.2,601.1L1094.0,593.4L1095.9,579.7L1092.2,573.7L1091.9,557.3L1095.6,545.3L1101.2,545.6L1103.2,540.4L1101.1,535.8L1110.0,516.5L1119.4,489.9L1124.9,490.0L1126.4,481.4L1137.1,483.9L1137.9,473.6L1141.5,472.9L1157.9,491.1L1158.1,513.7L1160.0,519.2L1150.2,523.1L1144.7,532.6L1145.6,540.8L1125.5,562.1L1121.4,579.2L1125.4,587.4L1130.9,593.8L1125.7,606.5L1119.7,609.0L1117.6,627.0L1114.3,636.7L1107.4,635.7L1104.2,643.7L1097.6,644.2L1095.8,634.6L1091.1,622.8L1086.7,607.5Z"/><path class="country" data-name="Belarus" d="M1184.3,636.0L1190.3,638.6L1191.1,641.1L1194.1,639.9L1199.6,642.3L1200.2,647.0L1199.0,649.6L1202.5,656.1L1204.9,657.8L1204.5,659.6L1208.4,661.3L1210.0,663.8L1207.8,665.9L1203.2,665.6L1202.1,666.4L1204.8,675.5L1199.9,676.1L1198.2,678.1L1197.8,682.7L1195.6,681.8L1190.4,682.3L1188.9,680.1L1186.8,681.7L1184.7,680.4L1168.1,677.3L1163.7,677.5L1160.6,680.0L1157.8,680.4L1157.7,676.3L1156.0,672.0L1159.4,670.0L1159.4,666.3L1157.8,662.7L1157.6,658.4L1163.1,658.5L1169.3,654.8L1170.6,649.3L1175.3,646.1L1174.7,641.6L1184.3,636.0Z"/><path class="country" data-name="Ukraine" d="M1199.9,676.1L1207.0,675.9L1208.4,673.8L1216.0,673.4L1219.7,678.6L1218.2,680.5L1218.7,683.3L1223.2,683.7L1225.3,687.7L1225.1,689.4L1232.4,692.6L1236.7,691.2L1240.2,695.3L1243.6,695.2L1251.9,698.1L1252.0,700.7L1249.7,705.2L1251.0,709.9L1250.1,712.8L1244.6,713.4L1241.6,715.8L1241.4,719.5L1236.9,720.1L1233.1,722.8L1227.8,723.3L1222.9,726.4L1223.2,730.7L1221.6,728.9L1215.7,726.8L1214.2,728.8L1204.6,725.9L1204.2,722.8L1198.9,723.8L1196.8,728.3L1192.4,734.4L1189.8,733.0L1187.2,734.3L1184.6,732.8L1186.0,731.9L1188.6,726.5L1188.2,725.0L1189.4,724.3L1189.9,725.5L1194.8,725.1L1193.7,724.3L1194.1,723.0L1192.2,720.9L1191.3,717.4L1189.3,716.1L1189.7,713.2L1187.1,710.9L1184.8,710.6L1180.6,707.9L1173.0,710.0L1171.6,712.0L1165.5,714.1L1162.8,712.1L1155.7,711.1L1153.2,712.9L1152.8,710.6L1149.6,708.3L1152.3,702.6L1153.6,703.1L1152.1,699.2L1157.3,691.8L1160.1,690.8L1160.7,688.3L1157.8,680.4L1160.6,680.0L1163.7,677.5L1168.1,677.3L1184.7,680.4L1186.8,681.7L1188.9,680.1L1190.4,682.3L1195.6,681.8L1197.8,682.7L1198.2,678.1L1199.9,676.1Z"/><path class="country" data-name="Poland" d="M1157.6,658.4L1157.8,662.7L1159.4,666.3L1159.4,670.0L1156.0,672.0L1157.7,676.3L1157.8,680.4L1160.7,688.3L1160.1,690.8L1157.3,691.8L1152.1,699.2L1153.6,703.1L1146.9,699.2L1142.8,700.5L1140.1,699.6L1136.8,701.4L1133.9,698.3L1131.6,699.5L1128.6,694.7L1124.4,694.1L1123.9,691.4L1120.0,690.4L1119.1,692.7L1116.0,690.8L1116.4,688.3L1112.1,687.6L1109.4,684.7L1107.1,678.8L1107.5,675.7L1106.1,670.7L1104.1,667.3L1105.7,664.8L1104.3,659.9L1124.3,649.2L1129.9,650.9L1130.4,653.3L1153.3,654.4L1156.2,655.4L1157.6,658.4Z"/><path class="country" data-name="Austria" d="M1120.6,710.9L1120.2,714.3L1117.0,714.3L1118.1,716.2L1115.1,723.0L1110.1,723.2L1107.2,725.0L1094.4,722.3L1093.1,719.4L1087.5,720.8L1086.9,722.4L1077.9,719.5L1078.8,717.4L1078.6,715.9L1080.3,715.5L1083.2,717.8L1084.0,715.6L1089.0,715.9L1093.1,714.4L1095.8,714.7L1097.6,716.4L1098.1,715.0L1097.3,709.4L1099.3,708.4L1101.3,704.4L1105.6,707.2L1108.8,703.6L1110.8,703.0L1115.2,705.6L1117.9,705.2L1120.5,706.8L1120.0,707.9L1120.6,710.9Z"/><path class="country" data-name="Hungary" d="M1149.6,708.3L1152.8,710.6L1153.2,712.9L1149.7,714.7L1143.6,726.0L1139.0,727.6L1135.5,727.2L1129.0,730.6L1124.3,729.0L1118.2,724.5L1117.1,721.7L1116.2,721.6L1118.1,716.2L1117.0,714.3L1120.2,714.3L1120.6,710.9L1125.6,714.0L1130.4,712.9L1130.8,711.2L1139.1,709.1L1140.5,707.1L1142.3,706.6L1148.4,709.2L1149.6,708.3Z"/><path class="country" data-name="Moldova" d="M1175.4,710.0L1180.6,707.9L1184.8,710.6L1187.1,710.9L1189.7,713.2L1189.3,716.1L1191.3,717.4L1192.2,720.9L1194.1,723.0L1193.7,724.3L1194.8,725.1L1189.9,725.5L1189.4,724.3L1188.2,725.0L1188.6,726.5L1186.0,731.9L1184.6,732.8L1183.6,729.1L1184.0,721.9L1177.2,710.9L1175.4,710.0Z"/><path class="country" data-name="Romania" d="M1184.6,732.8L1187.2,734.3L1189.8,733.0L1192.4,734.4L1192.5,736.4L1189.8,738.2L1188.1,737.4L1186.5,747.0L1183.1,746.2L1179.0,743.3L1172.3,745.1L1169.5,747.2L1161.1,746.7L1156.7,745.5L1154.5,746.1L1151.9,741.4L1153.2,740.1L1151.8,739.1L1150.0,740.9L1146.7,738.6L1146.2,735.3L1142.8,733.4L1142.1,730.8L1139.0,727.6L1143.6,726.0L1149.7,714.7L1155.7,711.1L1162.8,712.1L1165.5,714.1L1171.6,712.0L1173.0,710.0L1175.4,710.0L1177.2,710.9L1184.0,721.9L1183.6,729.1L1184.6,732.8Z"/><path class="country" data-name="Lithuania" d="M1174.7,641.6L1175.3,646.1L1170.6,649.3L1169.3,654.8L1163.1,658.5L1157.6,658.4L1156.2,655.4L1153.3,654.4L1152.9,651.9L1153.5,649.2L1145.0,645.9L1143.8,637.4L1150.3,634.3L1159.8,634.9L1165.4,633.9L1166.2,636.1L1169.3,636.7L1174.7,641.6Z"/><path class="country" data-name="Latvia" d="M1179.2,622.4L1182.0,624.9L1182.5,629.9L1184.3,636.0L1174.7,641.6L1169.3,636.7L1166.2,636.1L1165.4,633.9L1159.8,634.9L1150.3,634.3L1143.8,637.4L1144.0,629.7L1146.8,623.1L1152.1,619.5L1156.7,627.4L1161.2,627.2L1162.3,619.0L1167.2,617.2L1174.5,622.4L1179.2,622.4Z"/><path class="country" data-name="Estonia" d="M1183.2,600.7L1184.0,602.6L1180.0,609.0L1181.7,619.1L1179.2,622.4L1174.5,622.4L1167.2,617.2L1162.3,619.0L1163.0,612.7L1160.9,614.1L1157.3,610.2L1156.8,603.9L1171.1,599.1L1177.3,601.0L1183.2,600.7Z"/><path class="country" data-name="Germany" d="M1104.3,659.9L1105.7,664.8L1104.1,667.3L1106.1,670.7L1107.5,675.7L1107.1,678.8L1109.4,684.7L1106.9,685.6L1105.4,684.6L1104.0,686.3L1093.6,692.2L1095.2,698.6L1101.3,704.4L1099.3,708.4L1097.3,709.4L1098.1,715.0L1097.6,716.4L1095.8,714.7L1093.1,714.4L1089.0,715.9L1084.0,715.6L1083.2,717.8L1080.3,715.5L1078.6,715.9L1072.5,713.4L1071.3,715.2L1066.5,715.1L1067.2,709.1L1070.1,703.2L1061.9,701.6L1059.2,699.3L1059.5,695.4L1058.4,693.4L1059.0,687.4L1058.1,677.9L1061.5,677.9L1062.9,674.4L1064.3,665.8L1063.3,662.6L1064.4,660.5L1069.1,660.0L1070.2,662.1L1074.1,657.4L1072.8,653.7L1072.5,648.1L1076.8,649.5L1080.4,647.9L1080.5,651.8L1086.3,654.0L1086.2,657.5L1092.0,655.7L1095.2,653.0L1101.6,656.8L1104.3,659.9Z"/><path class="country" data-name="Bulgaria" d="M1152.9,742.8L1154.5,746.1L1156.7,745.5L1161.1,746.7L1169.5,747.2L1172.3,745.1L1179.0,743.3L1183.1,746.2L1186.5,747.0L1183.5,750.3L1181.4,755.8L1183.3,760.2L1178.4,759.2L1172.6,761.6L1172.5,765.4L1167.3,766.1L1163.3,763.4L1158.8,765.5L1154.6,765.3L1154.2,760.3L1151.3,757.8L1152.3,756.7L1151.6,755.8L1152.6,753.3L1154.8,750.9L1152.0,747.5L1151.5,744.6L1152.9,742.8Z"/><path class="country" data-name="Greece" d="M1173.6,809.1L1172.8,811.2L1164.7,811.8L1164.7,810.6L1157.8,809.3L1158.8,806.3L1161.9,808.6L1170.6,808.7L1170.5,810.0L1173.6,809.1ZM1154.6,765.3L1158.8,765.5L1163.3,763.4L1167.3,766.1L1172.5,765.4L1172.6,761.6L1175.3,763.6L1173.6,768.3L1172.2,769.2L1165.8,768.2L1158.9,770.2L1162.9,774.4L1160.0,775.6L1156.8,775.6L1153.8,771.8L1152.7,773.4L1154.0,777.9L1156.8,781.3L1154.7,782.9L1160.7,788.4L1160.8,792.5L1155.5,790.5L1157.2,794.2L1153.6,795.0L1155.7,801.2L1151.9,801.3L1147.3,798.2L1144.1,787.7L1139.0,780.2L1138.6,778.1L1141.3,774.5L1141.6,772.1L1143.5,771.0L1143.6,769.0L1147.3,768.4L1149.5,766.7L1152.6,766.9L1154.6,765.3Z"/><path class="country" data-name="Turkey" d="M1278.7,795.9L1276.0,797.1L1274.0,795.3L1267.4,794.4L1264.9,795.5L1255.4,796.5L1248.8,799.2L1244.2,799.2L1241.1,797.8L1234.9,799.8L1233.0,798.4L1232.7,802.4L1229.7,805.5L1227.6,802.3L1229.7,799.6L1226.2,800.2L1221.5,798.6L1217.6,802.7L1208.9,803.5L1204.3,799.7L1198.2,799.4L1196.9,802.4L1193.0,803.2L1187.5,799.4L1181.2,799.6L1177.9,792.5L1173.7,788.5L1176.5,782.8L1172.9,779.3L1179.2,772.2L1188.0,771.9L1190.3,766.2L1201.2,767.2L1208.0,762.3L1214.7,760.1L1224.1,759.9L1234.0,765.3L1242.2,768.2L1248.8,767.1L1253.7,767.7L1260.4,763.8L1266.5,763.4L1271.9,767.2L1272.9,769.8L1272.4,773.4L1276.6,775.3L1278.8,777.5L1274.9,779.6L1276.7,787.9L1275.6,790.2L1278.7,795.9ZM1172.6,761.6L1178.4,759.2L1183.3,760.2L1183.9,763.1L1188.9,765.6L1187.9,767.4L1181.1,767.8L1173.9,774.2L1172.2,770.7L1172.2,769.2L1173.6,768.3L1175.3,763.6L1172.6,761.6Z"/><path class="country" data-name="Albania" d="M1143.6,769.0L1143.5,771.0L1141.6,772.1L1141.3,774.5L1138.6,778.1L1137.7,777.6L1137.6,776.0L1134.4,773.5L1133.9,769.9L1135.2,762.4L1134.2,761.2L1133.8,758.8L1136.3,755.0L1136.6,756.4L1138.2,755.7L1140.8,758.6L1141.1,761.4L1140.4,764.0L1141.2,767.2L1143.6,769.0Z"/><path class="country" data-name="Croatia" d="M1118.2,724.5L1124.3,729.0L1129.0,730.6L1131.1,729.3L1134.3,734.8L1132.1,737.8L1129.5,736.1L1125.6,736.2L1120.7,734.8L1118.1,735.0L1116.8,736.7L1114.8,734.8L1113.6,738.2L1117.6,744.4L1124.6,752.3L1129.6,755.3L1129.0,756.6L1120.3,750.9L1115.1,748.6L1110.3,742.8L1111.5,742.2L1108.9,738.8L1108.8,736.1L1105.1,734.8L1103.4,738.3L1101.7,735.6L1102.0,732.7L1106.0,733.0L1107.0,731.6L1111.2,733.1L1111.2,730.8L1113.2,730.0L1113.7,726.6L1118.2,724.5Z"/><path class="country" data-name="Switzerland" d="M1078.6,715.9L1078.8,717.4L1077.9,719.5L1080.5,721.0L1083.4,721.2L1083.0,724.6L1080.4,726.0L1076.2,725.0L1075.0,728.3L1072.3,728.6L1071.3,727.3L1068.1,730.0L1065.4,730.4L1062.9,728.7L1061.0,725.1L1058.3,726.4L1058.3,722.6L1062.5,717.9L1062.3,715.8L1064.9,716.6L1066.5,715.1L1071.3,715.2L1072.5,713.4L1078.6,715.9Z"/><path class="country" data-name="Luxembourg" d="M1058.4,693.4L1059.5,695.4L1059.2,699.3L1056.3,698.7L1056.9,693.8L1058.4,693.4Z"/><path class="country" data-name="Belgium" d="M1059.0,687.4L1058.4,693.4L1056.9,693.8L1056.3,698.7L1051.3,694.7L1048.4,695.4L1041.8,687.6L1039.1,687.5L1038.3,684.3L1042.9,682.5L1047.0,683.2L1052.3,681.3L1059.0,687.4Z"/><path class="country" data-name="Netherlands" d="M1063.3,662.6L1064.3,665.8L1062.9,674.4L1061.5,677.9L1058.1,677.9L1059.0,687.4L1052.3,681.3L1047.0,683.2L1042.9,682.5L1045.8,680.0L1050.8,666.3L1058.6,662.3L1063.3,662.6Z"/><path class="country" data-name="Portugal" d="M972.6,761.2L977.0,758.1L978.4,761.9L986.1,761.1L987.7,765.0L985.0,767.0L985.0,772.9L984.0,774.0L983.8,777.5L981.3,778.1L983.6,782.5L982.0,787.3L984.0,789.4L981.1,794.1L981.6,796.4L979.3,798.3L976.3,797.3L973.4,798.1L974.2,792.5L973.7,788.0L971.2,787.4L969.8,784.6L970.3,779.8L972.5,777.1L974.1,769.6L972.6,761.2Z"/><path class="country" data-name="Spain" d="M981.6,796.4L981.1,794.1L984.0,789.4L982.0,787.3L983.6,782.5L981.3,778.1L983.8,777.5L984.0,774.0L985.0,772.9L985.0,767.0L987.7,765.0L986.1,761.1L978.4,761.9L977.0,758.1L972.6,761.2L972.9,755.7L970.6,752.3L978.6,746.7L999.3,749.4L1013.2,749.2L1015.5,752.3L1025.9,755.8L1028.0,754.1L1034.4,757.6L1041.0,756.6L1041.3,761.1L1035.9,766.1L1028.6,767.7L1028.1,770.3L1024.6,774.4L1022.4,780.4L1024.6,784.6L1021.3,787.9L1020.1,792.5L1015.8,794.0L1011.8,799.5L999.1,799.4L995.6,801.9L993.4,804.6L990.6,804.0L988.5,801.6L986.9,797.5L981.6,796.4Z"/><path class="country" data-name="Ireland" d="M988.7,658.9L989.7,665.7L985.4,674.1L975.3,679.5L967.2,678.1L971.9,668.4L968.9,658.7L980.9,646.5L982.1,651.8L980.9,657.0L984.4,656.9L988.7,658.9Z"/><path class="country" data-name="Italy" d="M1083.4,721.2L1086.9,722.4L1087.5,720.8L1093.1,719.4L1094.4,722.3L1102.5,724.4L1101.9,728.5L1103.3,731.9L1098.8,730.8L1094.1,733.6L1094.5,737.6L1093.8,739.9L1095.6,744.0L1101.0,747.9L1103.8,754.4L1110.1,760.6L1114.6,760.5L1116.0,762.2L1114.4,763.8L1123.7,768.8L1128.5,772.7L1129.1,774.1L1128.1,776.7L1124.9,773.3L1120.0,772.0L1117.6,776.8L1121.7,779.6L1121.0,783.4L1118.6,783.8L1115.6,790.1L1113.2,790.6L1114.4,784.5L1115.6,783.0L1111.7,775.0L1109.3,774.0L1107.6,770.8L1104.0,769.5L1101.5,766.4L1097.3,765.9L1087.7,757.5L1083.8,753.1L1082.0,745.3L1074.6,741.8L1071.9,742.9L1068.7,746.5L1066.3,747.1L1066.9,743.7L1063.9,742.7L1062.4,736.5L1064.4,734.0L1062.7,731.0L1062.9,728.7L1065.4,730.4L1068.1,730.0L1071.3,727.3L1072.3,728.6L1075.0,728.3L1076.2,725.0L1080.4,726.0L1083.0,724.6L1083.4,721.2ZM1102.2,789.7L1112.3,788.3L1110.2,794.0L1111.1,796.2L1109.9,799.8L1105.6,797.2L1094.7,792.8L1095.5,789.1L1102.2,789.7ZM1073.6,768.6L1076.4,766.3L1079.8,771.6L1079.0,781.4L1076.4,780.9L1074.1,783.4L1071.9,781.4L1071.7,772.5L1070.4,768.2L1073.6,768.6Z"/><path class="country" data-name="Denmark" d="M1080.4,647.9L1076.8,649.5L1072.5,648.1L1070.2,642.6L1070.0,632.2L1072.6,626.3L1077.6,625.6L1079.6,622.7L1084.2,619.7L1084.0,625.2L1082.3,628.6L1083.0,631.5L1086.1,633.0L1084.7,636.9L1083.0,635.8L1078.9,643.1L1080.4,647.9ZM1094.4,636.6L1096.2,641.7L1092.8,649.8L1086.8,644.1L1086.0,640.0L1094.4,636.6Z"/><path class="country" data-name="United Kingdom" d="M988.7,658.9L984.4,656.9L980.9,657.0L982.1,651.8L980.9,646.5L985.7,646.1L991.8,652.2L988.7,658.9ZM1006.4,663.3L1007.2,657.7L1003.3,651.6L996.4,649.8L995.1,647.2L997.2,642.7L995.3,639.9L992.2,644.7L991.9,634.9L989.0,629.7L991.1,618.8L995.5,610.0L1000.0,610.9L1006.9,610.0L1000.8,621.6L1012.9,620.2L1011.4,628.8L1006.3,638.0L1012.1,638.6L1017.7,651.5L1021.6,653.1L1026.7,667.8L1033.6,669.6L1032.9,675.6L1030.0,678.3L1032.2,683.0L1027.1,687.7L1019.5,687.7L1009.8,690.1L1007.2,688.4L1003.4,692.5L998.2,691.5L994.2,694.9L991.1,693.2L999.5,683.7L1004.6,681.8L995.6,680.2L994.0,676.6L1000.0,673.7L996.9,668.6L997.9,662.4L1006.4,663.3Z"/><path class="country" data-name="Iceland" d="M941.5,512.8L940.1,521.9L946.6,531.3L939.2,541.4L917.9,552.7L894.5,546.7L900.1,540.9L887.7,534.4L897.8,531.8L897.6,527.8L885.6,524.6L889.5,515.6L898.1,513.5L906.9,523.0L915.6,515.4L922.7,519.3L932.0,511.8L941.5,512.8Z"/><path class="country" data-name="Azerbaijan" d="M1288.0,761.3L1289.6,761.6L1293.5,766.2L1296.0,766.7L1297.0,764.8L1300.4,761.7L1306.3,771.1L1308.9,771.4L1310.7,773.4L1306.0,774.0L1304.0,782.3L1301.9,784.1L1302.1,787.7L1300.7,788.0L1297.1,784.2L1299.1,780.6L1297.4,778.4L1295.3,779.0L1288.6,784.4L1288.4,779.3L1283.5,776.1L1285.1,773.7L1282.0,771.1L1283.2,769.3L1281.0,768.0L1279.8,766.0L1281.2,764.7L1285.5,766.9L1288.5,767.4L1289.3,766.5L1286.5,762.4L1288.0,761.3ZM1286.5,784.6L1282.6,783.6L1279.7,780.2L1278.8,777.5L1280.0,777.3L1281.7,779.2L1284.2,779.2L1286.5,784.6Z"/><path class="country" data-name="Georgia" d="M1251.3,749.1L1252.0,748.2L1265.2,750.8L1272.9,754.5L1273.9,756.0L1277.4,754.8L1282.7,756.4L1284.4,759.5L1288.0,761.3L1286.5,762.4L1289.3,766.5L1288.5,767.4L1285.5,766.9L1281.2,764.7L1279.8,766.0L1271.9,767.2L1266.5,763.4L1260.4,763.8L1261.2,760.5L1259.8,755.3L1256.5,752.4L1253.4,751.5L1251.3,749.1Z"/><path class="country" data-name="Slovenia" d="M1102.5,724.4L1107.2,725.0L1110.1,723.2L1115.1,723.0L1116.2,721.6L1117.1,721.7L1118.2,724.5L1113.7,726.6L1113.2,730.0L1111.2,730.8L1111.2,733.1L1107.0,731.6L1106.0,733.0L1102.0,732.7L1103.3,731.9L1101.9,728.5L1102.5,724.4Z"/><path class="country" data-name="Finland" d="M1186.7,473.6L1185.8,484.5L1194.5,494.7L1189.3,505.8L1195.9,522.0L1192.1,533.7L1197.2,543.5L1194.9,551.9L1203.3,560.6L1201.2,566.9L1183.7,589.0L1173.4,589.9L1154.1,596.5L1150.8,590.3L1145.3,586.5L1146.6,574.8L1143.8,563.8L1146.5,556.6L1151.7,548.5L1168.5,531.5L1167.9,525.7L1160.0,519.2L1158.1,513.7L1157.9,491.1L1141.5,472.9L1144.9,468.7L1151.2,477.1L1158.6,476.3L1164.7,480.1L1170.1,473.1L1172.9,461.2L1181.8,455.6L1189.1,462.2L1186.7,473.6Z"/><path class="country" data-name="Slovakia" d="M1152.3,702.6L1149.6,708.3L1148.4,709.2L1142.3,706.6L1140.5,707.1L1139.1,709.1L1130.8,711.2L1130.4,712.9L1125.6,714.0L1120.6,710.9L1120.0,707.9L1121.3,704.9L1125.8,704.2L1127.0,702.9L1127.4,701.0L1129.6,699.0L1131.6,699.5L1133.9,698.3L1136.8,701.4L1140.1,699.6L1142.8,700.5L1146.9,699.2L1152.3,702.6Z"/><path class="country" data-name="Czechia" d="M1109.4,684.7L1112.1,687.6L1116.4,688.3L1116.0,690.8L1119.1,692.7L1120.0,690.4L1123.9,691.4L1124.4,694.1L1128.6,694.7L1131.3,699.0L1129.6,699.0L1127.4,701.0L1127.0,702.9L1125.8,704.2L1121.3,704.9L1120.5,706.8L1117.9,705.2L1115.2,705.6L1110.8,703.0L1108.8,703.6L1105.6,707.2L1095.2,698.6L1093.6,692.2L1104.0,686.3L1105.4,684.6L1106.9,685.6L1109.4,684.7Z"/><path class="country" data-name="Bosnia and Herz." d="M1129.6,755.3L1124.6,752.3L1117.6,744.4L1113.6,738.2L1114.8,734.8L1116.8,736.7L1118.1,735.0L1120.7,734.8L1125.6,736.2L1129.5,736.1L1132.1,737.8L1134.2,737.8L1132.8,741.3L1135.5,744.4L1134.7,748.1L1130.4,751.0L1129.6,755.3Z"/><path class="country" data-name="Macedonia" d="M1151.3,757.8L1154.2,760.3L1154.6,765.3L1152.6,766.9L1149.5,766.7L1147.3,768.4L1143.6,769.0L1141.2,767.2L1140.4,764.0L1142.1,759.9L1151.3,757.8Z"/><path class="country" data-name="Serbia" d="M1131.1,729.3L1135.5,727.2L1139.0,727.6L1142.1,730.8L1142.8,733.4L1146.2,735.3L1146.7,738.6L1150.0,740.9L1151.8,739.1L1153.2,740.1L1151.9,741.4L1152.9,742.8L1151.5,744.6L1152.0,747.5L1154.8,750.9L1152.6,753.3L1151.6,755.8L1152.3,756.7L1151.3,757.8L1146.7,758.4L1147.9,755.0L1142.4,750.4L1141.4,750.8L1140.6,753.4L1139.2,754.0L1139.7,753.3L1133.3,748.4L1134.7,748.1L1135.5,744.4L1132.8,741.3L1134.2,737.8L1132.1,737.8L1134.3,734.8L1131.1,729.3Z"/><path class="country" data-name="Montenegro" d="M1138.2,755.7L1136.6,756.4L1136.3,755.0L1133.8,758.8L1134.2,761.2L1129.0,756.6L1130.4,751.0L1133.3,748.4L1139.7,753.3L1138.2,755.7Z"/><path class="country" data-name="Kosovo" d="M1141.1,761.4L1140.8,758.6L1138.2,755.7L1140.6,753.4L1141.4,750.8L1142.4,750.4L1147.9,755.0L1146.7,758.4L1142.1,759.9L1141.9,761.4L1141.1,761.4Z"/></g><g class="labels"><text x="1083.2" y="683.8" class="country-label">Germany</text><text x="1036.5" y="724.5" class="country-label">France</text><text x="1003.0" y="773.8" class="country-label">Spain</text><text x="1095.7" y="754.1" class="country-label">Italy</text><text x="1132.7" y="675.6" class="country-label">Poland</text><text x="1054.7" y="674.6" class="country-label">Netherlands</text><text x="1050.2" y="687.4" class="country-label">Belgium</text><text x="1104.2" y="715.3" class="country-label">Austria</text><text x="1070.6" y="722.0" class="country-label">Switzerland</text><text x="1111.6" y="696.3" class="country-label">Czechia</text><text x="1080.9" y="636.7" class="country-label">Denmark</text><text x="1109.3" y="571.3" class="country-label">Sweden</text><text x="1075.2" y="583.2" class="country-label">Norway</text><text x="1009.8" y="652.7" class="country-label">United Kingdom</text><text x="978.5" y="778.3" class="country-label">Portugal</text><text x="1134.4" y="718.7" class="country-label">Hungary</text><text x="1166.2" y="728.6" class="country-label">Romania</text></g></svg></div><div id="tiles"></div><div id="fallback-grid"></div><div id="markers"></div>
  <div class="status" id="status">Loading live job map…</div>
  <button class="map-btn" id="zoom-in" type="button" aria-label="Zoom in">+</button>
  <button class="map-btn" id="zoom-out" type="button" aria-label="Zoom out">−</button>
  <div id="popup" role="dialog" aria-label="Jobs at location"><button class="popup-close" id="popup-close" type="button" aria-label="Close">×</button><div id="popup-content"></div></div>
  <div class="attribution">© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors</div>
</div>
<script>
(function(){
  const points=__PAYLOAD__, initialCenter=__CENTER__, initialZoom=__ZOOM__;
  const root=document.getElementById('map'), tilesEl=document.getElementById('tiles'), fallbackSvg=document.getElementById('fallback-svg'), markersEl=document.getElementById('markers'), status=document.getElementById('status'), popup=document.getElementById('popup'), popupContent=document.getElementById('popup-content');
  const tileSize=256, maxZoom=19, minZoom=2;
  let zoom=Number(initialZoom)||5, centerLat=Number(initialCenter[0])||51.1657, centerLon=Number(initialCenter[1])||10.4515, dragging=false, moved=false, lastX=0,lastY=0, startX=0,startY=0, startLon=0,startLat=0, raf=0;
  const markers=[];
  function esc(value){return String(value??'').replace(/[&<>'"]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));}
  function safeUrl(value){try{const u=new URL(String(value||''));return ['http:','https:'].includes(u.protocol)?u.href:'';}catch(_){return '';}}
  function clampLat(lat){return Math.max(-85.05112878,Math.min(85.05112878,lat));}
  function worldSize(z){return tileSize*Math.pow(2,z);}
  function project(lat,lon,z){const n=worldSize(z), r=clampLat(lat)*Math.PI/180;return {x:(lon+180)/360*n,y:(1-Math.log(Math.tan(r)+1/Math.cos(r))/Math.PI)/2*n};}
  function unproject(x,y,z){const n=worldSize(z), lon=x/n*360-180, a=Math.PI*(1-2*y/n), lat=180/Math.PI*Math.atan(Math.sinh(a));return {lat,lon};}
  function wrapLon(lon){while(lon>180)lon-=360;while(lon<-180)lon+=360;return lon;}
  function setStatus(text){if(status)status.textContent=text;}
  function popupFor(point){
    const sourceText=Object.entries(point.sources||{}).map(([k,v])=>esc(k)+': '+v).join(' · ');
    const jobs=(point.jobs||[]).map(job=>{const url=safeUrl(job.url);return '<div class="job"><div class="job-title">'+esc(job.title)+'</div><div class="job-company">'+esc(job.company)+(job.posted?' · '+esc(job.posted):'')+'</div><div class="job-source">'+esc(job.source)+'</div>'+(url?'<a href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">Open job ↗</a>':'')+'</div>';}).join('');
    return '<div class="popup-head"><div class="popup-location">'+esc(point.location)+'</div><div class="popup-meta">'+point.count+' matching jobs'+(sourceText?' · '+sourceText:'')+'</div></div>'+jobs+(point.more?'<div class="more">+ '+point.more+' more jobs at this location</div>':'');
  }
  function showPopup(point,screenX,screenY){popupContent.innerHTML=popupFor(point);popup.style.display='block';const pw=popup.offsetWidth,ph=popup.offsetHeight;let x=screenX+14,y=screenY-ph-14;if(x+pw>root.clientWidth-8)x=root.clientWidth-pw-8;if(x<8)x=8;if(y<8)y=screenY+14;if(y+ph>root.clientHeight-8)y=root.clientHeight-ph-8;popup.style.left=x+'px';popup.style.top=y+'px';}
  function closePopup(){popup.style.display='none';}
  function render(){
    const w=root.clientWidth,h=root.clientHeight;if(!w||!h)return;
    const c=project(centerLat,centerLon,zoom), scale=worldSize(zoom), left=c.x-w/2, top=c.y-h/2;
    if(fallbackSvg){ fallbackSvg.style.width=scale+'px'; fallbackSvg.style.height=scale+'px'; fallbackSvg.style.left=(-left)+'px'; fallbackSvg.style.top=(-top)+'px'; }
    tilesEl.innerHTML='';
    const tx0=Math.floor(left/tileSize)-1, tx1=Math.floor((left+w)/tileSize)+1, ty0=Math.floor(top/tileSize)-1, ty1=Math.floor((top+h)/tileSize)+1, max=Math.pow(2,zoom);
    const tileCount=Math.max(1,(tx1-tx0+1)*(ty1-ty0+1)); let loaded=0,failed=0;
    for(let ty=ty0;ty<=ty1;ty++){
      if(ty<0||ty>=max)continue;
      for(let tx=tx0;tx<=tx1;tx++){
        const wrapped=((tx%max)+max)%max;const img=document.createElement('img');img.className='tile';img.alt='';img.draggable=false;img.src='https://tile.openstreetmap.org/'+zoom+'/'+wrapped+'/'+ty+'.png';img.style.left=(tx*tileSize-left)+'px';img.style.top=(ty*tileSize-top)+'px';img.addEventListener('load',()=>{loaded++;if(loaded+failed>=tileCount)setStatus(points.length?(points.length+' locations · '+points.reduce((n,p)=>n+p.count,0)+' jobs'):'No mappable locations in this search');},{once:true});img.addEventListener('error',()=>{failed++;img.remove();if(loaded+failed>=tileCount){setStatus(points.length?'Map tiles unavailable · markers still interactive':'No mappable locations in this search');}},{once:true});tilesEl.appendChild(img);
      }
    }
    markersEl.innerHTML='';markers.length=0;
    points.forEach(point=>{const p=project(point.lat,point.lon,zoom);let dx=p.x-left;while(dx<0)dx+=scale;while(dx>w)dx-=scale;const dy=p.y-top;if(dy<-70||dy>h+70||dx<-70||dx>w+70)return;const btn=document.createElement('button');btn.type='button';btn.className='marker';btn.setAttribute('aria-label',point.location+' · '+point.count+' jobs');btn.style.left=dx+'px';btn.style.top=dy+'px';btn.innerHTML='<span class="job-marker">'+esc(point.count)+'</span>';btn.addEventListener('click',()=>{if(moved)return;showPopup(point,dx,dy);});markersEl.appendChild(btn);markers.push(btn);});
    if(!points.length)setStatus('No mappable locations in this search');
  }
  function zoomAt(nextZoom,screenX,screenY){nextZoom=Math.max(minZoom,Math.min(maxZoom,Math.round(nextZoom)));if(nextZoom===zoom)return;const before=unproject(project(centerLat,centerLon,zoom).x+(screenX-root.clientWidth/2),project(centerLat,centerLon,zoom).y+(screenY-root.clientHeight/2),zoom);const newScale=worldSize(nextZoom), newCenter=project(before.lat,before.lon,nextZoom);const rootCenter={x:root.clientWidth/2,y:root.clientHeight/2};const target=unproject(newCenter.x-(screenX-rootCenter.x),newCenter.y-(screenY-rootCenter.y),nextZoom);zoom=nextZoom;centerLat=clampLat(target.lat);centerLon=wrapLon(target.lon);closePopup();render();}
  root.addEventListener('pointerdown',e=>{if(e.button!==0)return;if(e.target.closest('.marker, .map-btn, #popup'))return;dragging=true;moved=false;lastX=startX=e.clientX;lastY=startY=e.clientY;startLon=centerLon;startLat=centerLat;root.setPointerCapture?.(e.pointerId);});
  root.addEventListener('pointermove',e=>{if(!dragging)return;const dx=e.clientX-lastX,dy=e.clientY-lastY;if(Math.abs(e.clientX-startX)+Math.abs(e.clientY-startY)>5)moved=true;lastX=e.clientX;lastY=e.clientY;const c=project(startLat,startLon,zoom), next=unproject(c.x-dx,c.y-dy,zoom);centerLat=clampLat(next.lat);centerLon=wrapLon(next.lon);if(!raf){raf=requestAnimationFrame(()=>{raf=0;render();});}});
  root.addEventListener('pointerup',e=>{dragging=false;try{root.releasePointerCapture?.(e.pointerId);}catch(_){}setTimeout(()=>{moved=false;},0);});root.addEventListener('pointercancel',()=>{dragging=false;});
  root.addEventListener('wheel',e=>{e.preventDefault();zoomAt(zoom+(e.deltaY<0?1:-1),e.clientX,e.clientY);},{passive:false});
  document.getElementById('zoom-in').addEventListener('click',()=>zoomAt(zoom+1,root.clientWidth/2,root.clientHeight/2));document.getElementById('zoom-out').addEventListener('click',()=>zoomAt(zoom-1,root.clientWidth/2,root.clientHeight/2));document.getElementById('popup-close').addEventListener('click',closePopup);
  window.addEventListener('resize',()=>render());
  render();setTimeout(()=>{if(status)status.style.display='none';},2200);
})();
</script>
</body>
</html>'''
    map_html=map_html.replace('__PAYLOAD__',payload).replace('__CENTER__',center_json).replace('__ZOOM__',str(zoom))
    return map_html,mapped_jobs,skipped_jobs

def render_live_job_map(jobs: list[dict], search_location: str = "") -> None:
    st.markdown('<div class="jobsync-live-map-header"><div><span>LIVE JOB MAP</span><b>Jobs by location</b></div><small>Click a marker to see the matching listings · drag to pan · scroll to zoom</small></div>', unsafe_allow_html=True)
    try:
        map_html, mapped_jobs, skipped_jobs = _job_map_html(jobs, search_location)
        components.html(map_html, height=432, scrolling=False)
        if jobs:
            suffix = f" · {skipped_jobs} jobs without a mappable location" if skipped_jobs else ""
            st.caption(f"{mapped_jobs} of {len(jobs)} jobs plotted{suffix}.")
        else:
            st.caption("Run a search to populate the live map with job listings.")
    except Exception as exc:
        st.warning(f"The live map could not be loaded. Job results are still available below. ({exc})")


def safe_name(value: str, fallback: str = "document") -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "").strip("_")
    return clean[:70] or fallback


def unique_doc_path(folder: Path, stem: str, suffix: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = folder / f"{safe_name(stem)}_{stamp}{suffix}"
    counter = 2
    while path.exists():
        path = folder / f"{safe_name(stem)}_{stamp}_{counter}{suffix}"
        counter += 1
    return path


def cv_library_location() -> Path:
    """Return the single local folder used for organized CV-library copies."""
    CV_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    return CV_LIBRARY_DIR


def _cv_backup_payload() -> list[tuple[Path, str]]:
    """Collect managed CV files and a metadata snapshot for backup."""
    files: list[tuple[Path, str]] = []
    roots = [UPLOAD_CV, OUTPUT_CV, CV_LIBRARY_DIR]
    seen: set[str] = set()
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
        for path in root.rglob('*'):
            if not path.is_file():
                continue
            try:
                resolved = str(path.resolve())
            except Exception:
                resolved = str(path)
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                rel = path.relative_to(BASE_DIR).as_posix()
            except Exception:
                rel = path.name
            files.append((path, rel))
    return files


def create_cv_library_backup(reason: str = "manual") -> Path:
    """Create a timestamped ZIP backup of the CV library and metadata."""
    CV_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = CV_BACKUP_DIR / f"JobSync_CV_Backup_{stamp}.zip"
    metadata = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "reason": reason,
        "library_folder": str(cv_library_location()),
        "documents": state.get("documents", []),
        "applied": state.get("applied", []),
    }
    with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("cv_library_metadata.json", json.dumps(metadata, indent=2, ensure_ascii=False))
        for path, rel in _cv_backup_payload():
            try:
                zf.write(path, arcname=rel)
            except OSError:
                continue
    state.setdefault("settings", {})["cv_last_backup_path"] = str(backup_path)
    state["settings"]["cv_last_backup_at"] = metadata["created_at"]
    state["settings"]["cv_backup_count"] = int(state["settings"].get("cv_backup_count", 0)) + 1
    save_state(state)
    return backup_path


def maybe_library_copy(doc: dict) -> Path | None:
    """Ensure every managed CV has an organized copy inside the single CV library folder."""
    source = Path(str(doc.get("path") or ""))
    if not source.exists() or not source.is_file():
        return None
    library = cv_library_location()
    display_name = safe_name(str(doc.get("display_name") or source.stem), source.stem)
    suffix = source.suffix or ".bin"
    target = library / f"{display_name}{suffix}"
    if target.resolve() == source.resolve():
        return target
    if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
        shutil.copy2(source, target)
    return target


def sync_cv_library() -> int:
    """Refresh organized CV copies for all generated/uploaded/reference CV records."""
    count = 0
    for doc in cv_document_records():
        target = maybe_library_copy(doc)
        if target is not None:
            doc["library_path"] = str(target)
            count += 1
    if count:
        save_state(state)
    return count


def _cv_download_clicked(filename: str = "CV") -> None:
    """Give immediate in-app and Windows feedback when a browser download starts."""
    st.session_state["cv_last_download_name"] = filename
    message = f"Download started: {filename}"
    try:
        st.toast(message, icon="⬇️")
    except Exception:
        pass
    try:
        desktop_notify("JobSync", message)
    except Exception:
        pass



def _ai_api_key(provider: str) -> str:
    """Resolve a hosted-provider API key without persisting it in JobSync state.

    Keyed by the resolved provider (ChatGPT/Claude/Gemini), not the specific
    model display name, so entering a key once covers every model from that
    provider (e.g. GPT-4o mini and GPT-4o share the same OpenAI key).
    """
    resolved, _ = _resolve_ai_selection(provider)
    if resolved == "Auto":
        # The router itself has no key of its own — it's "connected" as soon
        # as at least one model in its fallback chain has a key.
        for candidate_name in AUTO_FREE_MODEL_CHAIN:
            candidate_provider = HOSTED_AI_MODELS.get(candidate_name, {}).get("provider", "")
            if candidate_provider and _ai_api_key(candidate_provider):
                return "auto-connected"
        return ""
    env_names = {
        "ChatGPT": "OPENAI_API_KEY",
        "Claude": "ANTHROPIC_API_KEY",
        "Gemini": "GEMINI_API_KEY",
        "Groq": "GROQ_API_KEY",
    }
    return str(st.session_state.get(f"cv_ai_key_{resolved}") or os.getenv(env_names.get(resolved, ""), "")).strip()


FREE_AI_KEY_SETUP = {
    "Gemini": {
        "url": "https://aistudio.google.com/apikey",
        "button": "Get free Gemini key ↗",
        "steps": [
            "Click **Get free Gemini key** — opens Google AI Studio in a new tab.",
            "Sign in with any Google account (no credit card).",
            "Click **Create API key**, then copy it.",
            "Click **Open Settings**, paste the key, and click **Save settings**.",
        ],
    },
    "Groq": {
        "url": "https://console.groq.com/keys",
        "button": "Get free Groq key ↗",
        "steps": [
            "Click **Get free Groq key** — opens the Groq console in a new tab.",
            "Sign in with Google, GitHub, or email (no credit card).",
            "Click **Create API Key**, then copy it.",
            "Click **Open Settings**, paste the key, and click **Save settings**.",
        ],
    },
    "Auto": {
        "url": "https://aistudio.google.com/apikey",
        "button": "Get free Gemini key ↗",
        "steps": [
            "The auto-switch router needs at least one connected free key — Gemini is the quickest to set up.",
            "Click **Get free Gemini key** — opens Google AI Studio in a new tab.",
            "Sign in with any Google account (no credit card), click **Create API key**, then copy it.",
            "Click **Open Settings**, paste it, click **Save settings** — then add a free Groq key too for extra fallback capacity.",
        ],
    },
}


LOCAL_AI_PROVIDER = "Local AI"
LOCAL_AI_MODELS = {
    "Qwen3 14B": {"model": "qwen3:14b", "size": "~9.3 GB", "ram": "Recommended: 16 GB RAM", "description": "Best overall balance for CVs, cover letters and multilingual tailoring."},
    "gpt-oss 20B": {"model": "gpt-oss:20b", "size": "~14 GB", "ram": "Recommended: 16 GB RAM", "description": "Strong reasoning and structured-output option for stronger PCs."},
    "Qwen3 30B-A3B": {"model": "qwen3:30b-a3b", "size": "~19 GB", "ram": "Recommended: 24 GB RAM", "description": "Highest-quality Qwen option here; needs substantially more memory."},
    "Gemma 3 12B": {"model": "gemma3:12b", "size": "~8.1 GB", "ram": "Recommended: 16 GB RAM", "description": "Compact strong general model with multilingual support."},
}
LOCAL_AI_DEFAULT = "Qwen3 14B"
LOCAL_AI_LEGACY_PROVIDER = "Local Qwen3"
OLLAMA_BASE_URL = os.getenv("JOBSYNC_OLLAMA_URL", "http://127.0.0.1:11435").rstrip("/")
OLLAMA_MODELS_DIR = BASE_DIR / "ai" / "models"
OLLAMA_MODELS_DIR.mkdir(parents=True, exist_ok=True)

def _local_ai_key(provider: str | None) -> str:
    value = str(provider or "").strip()
    return value if value in LOCAL_AI_MODELS else LOCAL_AI_DEFAULT

def _local_ai_config(provider: str | None) -> dict:
    return LOCAL_AI_MODELS[_local_ai_key(provider)]

# Online model catalogue. Generation happens over a plain HTTPS API call (no
# SDK, no local weights, no install) so the app stays small — this is the
# default path. "Free" here means the provider itself charges nothing (a
# free Google AI Studio key, no billing), not that zero setup is possible:
# every hosted model still needs its own API key pasted in once. The
# Offline/local models further below remain available for anyone who'd
# rather download a model to this PC instead (large download, no key).
HOSTED_AI_MODELS = {
    "Free AI (auto-switch)": {"provider": "Auto", "model": "", "tier": "Free", "note": "Routes across every connected free key · switches models automatically when one is rate-limited"},
    "Gemini Flash": {"provider": "Gemini", "model": "gemini-flash-latest", "tier": "Free", "note": "Free Google AI Studio key · fast"},
    "Gemini Pro": {"provider": "Gemini", "model": "gemini-pro-latest", "tier": "Free", "note": "Free Google AI Studio key · stronger reasoning"},
    "Groq Llama 3.3 70B": {"provider": "Groq", "model": "llama-3.3-70b-versatile", "tier": "Free", "note": "Free Groq key · very fast · high rate limits · 128K context"},
    "Groq Llama 3.1 8B": {"provider": "Groq", "model": "llama-3.1-8b-instant", "tier": "Free", "note": "Free Groq key · fastest, smaller model · separate rate-limit bucket from 70B · 128K context"},
    "GPT-4o mini": {"provider": "ChatGPT", "model": "gpt-4o-mini", "tier": "Paid", "note": "OpenAI API key with billing · low cost"},
    "GPT-4o": {"provider": "ChatGPT", "model": "gpt-4o", "tier": "Paid", "note": "OpenAI API key with billing · premium quality"},
    "Claude 3.5 Haiku": {"provider": "Claude", "model": "claude-3-5-haiku-20241022", "tier": "Paid", "note": "Anthropic API key with billing · fast, low cost"},
    "Claude Sonnet 4": {"provider": "Claude", "model": "claude-sonnet-4-20250514", "tier": "Paid", "note": "Anthropic API key with billing · premium quality"},
}
AI_DEFAULT_PROVIDER = "Free AI (auto-switch)"

# Preference order for the "Free AI (auto-switch)" router: each entry is a
# HOSTED_AI_MODELS display name. Different models on the SAME provider still
# help — Groq meters each model's rate limit independently, so 70B being
# limited doesn't mean 8B is too. Only entries whose provider has a
# configured key are actually tried. Every entry here must have a large
# enough context window for a full CV-generation prompt (template + CV +
# job description can easily run several thousand tokens) — a small-context
# model like Groq's Gemma2 9B (~8K tokens) was tried here and consistently
# failed with a 400 "request too large" instead of a usable fallback, so it
# was removed rather than left in the chain as a guaranteed dead end.
AUTO_FREE_MODEL_CHAIN = [
    "Gemini Flash", "Groq Llama 3.3 70B", "Gemini Pro",
    "Groq Llama 3.1 8B",
]


def _resolve_ai_selection(selection: str | None) -> tuple[str, str | None]:
    """Map a wizard AI selection to (actual provider key, model override).

    A selection is either a display name from HOSTED_AI_MODELS (e.g. "GPT-4o
    mini") or a local model's own name (e.g. "Qwen3 14B"), which is already
    the provider key everything downstream expects — resolving it is then a
    no-op, so every caller can keep passing whatever is in cv_wizard_ai /
    external_ai_provider unchanged.
    """
    sel = str(selection or "").strip()
    cfg = HOSTED_AI_MODELS.get(sel)
    if cfg:
        return cfg["provider"], cfg.get("model")
    return sel, None


def _is_local_ai_provider(provider: str | None) -> bool:
    resolved, _ = _resolve_ai_selection(provider)
    return resolved in set(LOCAL_AI_MODELS) | {LOCAL_AI_PROVIDER, LOCAL_AI_LEGACY_PROVIDER}


def _ollama_executable() -> str | None:
    """Find the Ollama executable on Windows/macOS/Linux."""
    names = ["ollama.exe", "ollama"] if os.name == "nt" else ["ollama"]
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        candidates = [
            Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
            Path(os.getenv("ProgramFiles", "")) / "Ollama" / "ollama.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
    return None


def _ollama_ready() -> bool:
    try:
        response = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=3)
        return response.ok
    except Exception:
        return False


def _start_ollama(executable: str | None, progress=None) -> None:
    if _ollama_ready():
        if progress:
            progress("Local AI service is ready.")
        return
    if executable:
        try:
            if progress:
                progress("Starting the private local AI engine…")
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            child_env = os.environ.copy()
            child_env["OLLAMA_MODELS"] = str(OLLAMA_MODELS_DIR)
            child_env["OLLAMA_HOST"] = OLLAMA_BASE_URL.replace("http://", "")
            subprocess.Popen([executable, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=creationflags, env=child_env)
        except Exception as exc:
            raise RuntimeError(f"Could not start the local AI engine: {exc}") from exc
    deadline = time.time() + 30
    while time.time() < deadline:
        if _ollama_ready():
            if progress:
                progress("Local AI service is ready.")
            return
        time.sleep(0.5)
    raise RuntimeError("The local AI engine did not start. Please restart JobSync and try again.")


def _ensure_local_ai(provider: str, progress=None) -> None:
    cfg = _local_ai_config(provider)
    model = cfg["model"]
    executable = _ollama_executable()
    if not executable and os.name == "nt":
        if progress: progress("Installing the local AI engine (Ollama)…")
        if shutil.which("winget"):
            try:
                result = subprocess.run(["winget", "install", "--id", "Ollama.Ollama", "-e", "--accept-source-agreements", "--accept-package-agreements", "--disable-interactivity"], capture_output=True, text=True, timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                executable = _ollama_executable() if result.returncode in (0, 3010) else None
            except Exception:
                executable = None
        if not executable:
            installer = Path(tempfile.gettempdir()) / "JobSync-OllamaSetup.exe"
            try:
                if progress: progress("Downloading the official Ollama installer…")
                with requests.get("https://ollama.com/download/OllamaSetup.exe", stream=True, timeout=120) as response:
                    response.raise_for_status()
                    with installer.open("wb") as fh:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if chunk: fh.write(chunk)
                if progress: progress("Installing Ollama automatically…")
                result = subprocess.run([str(installer), "/VERYSILENT", "/NORESTART"], capture_output=True, text=True, timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                executable = _ollama_executable()
                if result.returncode not in (0, 3010) and not executable:
                    raise RuntimeError("The official Ollama installer returned an error.")
            except Exception as exc:
                raise RuntimeError(f"JobSync could not automatically install Ollama: {exc}") from exc
            finally:
                try: installer.unlink()
                except OSError: pass
    if not executable:
        raise RuntimeError("JobSync could not find or install Ollama automatically.")
    _start_ollama(executable, progress=progress)
    tags = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=10).json().get("models", [])
    model_names = {str(item.get("name", "")) for item in tags if isinstance(item, dict)}
    if model not in model_names:
        if progress: progress(f"Downloading {provider} ({model}, {cfg['size']}) for first use…")
        pull = requests.post(f"{OLLAMA_BASE_URL}/api/pull", json={"name": model, "stream": True}, stream=True, timeout=3600)
        pull.raise_for_status()
        for raw in pull.iter_lines(decode_unicode=True):
            if not raw: continue
            try:
                data = json.loads(raw)
                status = str(data.get("status") or "Downloading local AI model…")
                if data.get("error"): raise RuntimeError(str(data["error"]))
                st.session_state["cv_local_ai_status"] = status
                if progress: progress(status)
            except json.JSONDecodeError:
                continue


def _repair_local_cv_structure(data: dict, template: str, source_prompt: str, progress=None, model: str | None = None) -> dict:
    """Repair incomplete local-AI CV content with a reliable, small-output path.

    Qwen3 14B was repeatedly failing on the previous JSON repair request for
    Professional Experience.  The important distinction is that a valid JSON
    response is not required to write experience bullets.  We therefore use a
    plain-text repair response for experience, which is much easier for a local
    model to complete, and parse the numbered bullets locally.  The locked
    blueprint remains the source of truth for structure and bullet counts.
    """
    template = template or ""
    expected_skills = [
        re.sub(r"\\([&%$#_{}])", r"\1", m.group(1).strip())
        for m in re.finditer(r"\\skillrow\{([^}]*)\}\{([^}]*)\}", template)
    ]

    exp_slots = []
    for m in re.finditer(
        r"(?P<head>\\resumeSubheading\{[^{}]*\}\{[^{}]*\}\{[^{}]*\}\{[^{}]*\}\s*\n)(?P<body>\\begin\{itemize\}.*?\\end\{itemize\})",
        template,
        re.S,
    ):
        exp_slots.append(len(re.findall(r"\\item\s+", m.group("body"))))

    project_match = re.search(
        r"\\resumeProject\{[^{}]*\}\{[^{}]*\}\s*\n(?P<body>\\begin\{itemize\}.*?\\end\{itemize\})",
        template,
        re.S,
    )
    project_count = len(re.findall(r"\\item\s+", project_match.group("body"))) if project_match else 0

    add_count = 0
    add_pos = template.find(r"\section*{Additional Information}")
    if add_pos >= 0:
        add_end = template.find(r"\end{document}", add_pos)
        add_section = template[add_pos:add_end if add_end >= 0 else len(template)]
        add_match = re.search(r"\\begin\{itemize\}.*?\\end\{itemize\}", add_section, re.S)
        if add_match:
            add_count = len(re.findall(r"\\item\s+", add_match.group(0)))

    def _valid_profile(value: object) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def _valid_skills(value: object) -> bool:
        return (
            isinstance(value, list)
            and len(value) == len(expected_skills)
            and all(
                isinstance(row, dict)
                and bool(str(row.get("label") or "").strip())
                and bool(str(row.get("text") or "").strip())
                for row in value
            )
        )

    def _valid_experience(value: object) -> bool:
        return (
            isinstance(value, list)
            and len(value) == len(exp_slots)
            and all(
                isinstance(row, dict)
                and isinstance(row.get("bullets"), list)
                and len(row.get("bullets", [])) == exp_slots[i]
                and all(str(x).strip() for x in row.get("bullets", []))
                for i, row in enumerate(value)
            )
        )

    def _valid_array(value: object, count: int) -> bool:
        return isinstance(value, list) and len(value) == count and all(str(x).strip() for x in value)

    defective: list[str] = []
    if not _valid_profile(data.get("profile")):
        defective.append("profile")
    if expected_skills and not _valid_skills(data.get("skills")):
        defective.append("skills")
    if exp_slots and not _valid_experience(data.get("experience")):
        defective.append("experience")
    if project_count and not _valid_array(data.get("project_bullets"), project_count):
        defective.append("project_bullets")
    if add_count and not _valid_array(data.get("additional_information"), add_count):
        defective.append("additional_information")

    if not defective:
        return data

    # Keep the evidence small.  In particular, do not resend the full LaTeX
    # blueprint during repair; that consumes context without helping bullet writing.
    evidence_prompt = str(source_prompt or "")
    marker_pairs = [
        ("TARGET JOB", "JOB DESCRIPTION"),
        ("JOB DESCRIPTION", "CANDIDATE PROFILE"),
        ("CANDIDATE PROFILE", "REFERENCE CV MATERIAL"),
    ]
    compact_parts = []
    for start_marker, end_marker in marker_pairs:
        a = evidence_prompt.find(start_marker)
        b = evidence_prompt.find(end_marker, a + len(start_marker)) if a >= 0 else -1
        if a >= 0:
            chunk = evidence_prompt[a:b if b > a else len(evidence_prompt)].strip()
            compact_parts.append(chunk[:12000])
    if compact_parts:
        evidence_prompt = "\n\n---\n\n".join(compact_parts)
    else:
        evidence_prompt = evidence_prompt[:30000]

    # ---------------------------------------------------------------
    # EXPERIENCE: plain-text repair, not JSON.
    # ---------------------------------------------------------------
    if "experience" in defective:
        current = data.get("experience") if isinstance(data.get("experience"), list) else []
        repaired_entries: list[dict] = []

        # Build one compact request for the whole experience section.  The master
        # template currently has one six-bullet experience block, but this also
        # supports multiple blocks without making six/several sequential model calls.
        entry_specs = []
        for i, required_count in enumerate(exp_slots):
            existing = current[i] if i < len(current) and isinstance(current[i], dict) else {}
            existing_bullets = existing.get("bullets") if isinstance(existing.get("bullets"), list) else []
            existing_bullets = [str(x).strip() for x in existing_bullets if str(x).strip()]
            if len(existing_bullets) == required_count:
                entry_specs.append((i, required_count, existing_bullets))
            else:
                entry_specs.append((i, required_count, existing_bullets))

        needs_ai = any(len(existing) != required for _, required, existing in entry_specs)
        if needs_ai:
            if progress:
                progress(f"{model or 'Local AI'} is generating Professional Experience bullets…")

            lines = [
                "JOBSYNC PROFESSIONAL EXPERIENCE REPAIR",
                "Return ONLY plain text. Do NOT return JSON, Markdown, LaTeX, explanations, or reasoning.",
                "Write the missing/incomplete professional-experience bullets for the locked CV template.",
                "Use the exact entry and bullet counts below.",
                "",
            ]
            for i, required_count, existing in entry_specs:
                lines.append(f"ENTRY {i + 1}: exactly {required_count} bullets")
                if existing:
                    lines.append("Existing valid bullets to preserve/improve:")
                    for j, bullet in enumerate(existing, 1):
                        lines.append(f"{j}. {bullet}")
                else:
                    lines.append("Existing bullets: none")
                lines.append("")
            lines.extend([
                "OUTPUT FORMAT — mandatory:",
                "ENTRY 1",
                "1. bullet text",
                "2. bullet text",
                "...",
                "ENTRY 2",
                "1. bullet text",
                "...",
                "",
                "RULES:",
                "- Output exactly the requested number of bullets for every entry.",
                "- Each bullet should be concise, specific and ATS-friendly.",
                "- Use only facts explicitly supported by the candidate evidence.",
                "- Never invent metrics, software, tools, dates, employers, qualifications, responsibilities or achievements.",
                "- Do not copy reference-CV sentences verbatim.",
                "- Do not use filler such as results-driven, dynamic, leveraged, passionate, proven track record, or spearheaded.",
                "",
                "JOB + CANDIDATE EVIDENCE:",
                evidence_prompt,
            ])
            repair_prompt = "\n".join(lines)

            last_error = None
            response_text = ""
            for attempt in range(1, 3):
                try:
                    if progress:
                        progress(f"{model or 'Local AI'} is generating Professional Experience bullets… (attempt {attempt}/2)")
                    response = _ollama_chat_request(
                        {
                            "model": model or _local_ai_config(LOCAL_AI_DEFAULT)["model"],
                            "think": False,
                            "messages": [
                                {"role": "system", "content": "Write only the requested numbered CV bullets. No JSON. No LaTeX. No explanation."},
                                {"role": "user", "content": repair_prompt + "\n/no_think"},
                            ],
                            "options": {
                                "temperature": 0.15,
                                "num_predict": max(1400, sum(required for _, required, _ in entry_specs) * 120 + 300),
                                "top_p": 0.8,
                            },
                        },
                        stream=False,
                        timeout=900,
                        progress=progress,
                    )
                    response.raise_for_status()
                    response_text = str((response.json().get("message") or {}).get("content") or "").strip()
                    if not response_text:
                        raise RuntimeError("local model returned an empty experience response")
                    break
                except Exception as exc:
                    last_error = exc
                    response_text = ""
            if not response_text:
                raise RuntimeError(f"Qwen3 could not generate Professional Experience bullets: {last_error}")

            # Remove leaked Qwen reasoning if present.
            if "</think>" in response_text:
                response_text = response_text.rsplit("</think>", 1)[1].strip()

            def parse_entry_bullets(text: str, entry_number: int, expected: int) -> list[str]:
                # Isolate the requested ENTRY block. Accept both `ENTRY 1` and
                # `ENTRY 1:` because local models vary on punctuation.
                pat = re.compile(
                    rf"(?:^|\n)\s*ENTRY\s+{entry_number}\s*:?[ \t]*(.*?)(?=(?:\n\s*ENTRY\s+\d+\s*:?)|\Z)",
                    re.I | re.S,
                )
                m = pat.search(text)
                block = m.group(1) if m else text
                bullets = []
                for bm in re.finditer(r"(?:^|\n)\s*(?:[-•*]|\d+[.)])\s+(.+?)(?=\n\s*(?:[-•*]|\d+[.)])\s+|\Z)", block, re.S):
                    value = re.sub(r"\s+", " ", bm.group(1)).strip()
                    if value and not value.upper().startswith("ENTRY "):
                        bullets.append(value)
                # If numbering was flattened into a single line, try a simple
                # split on numbered markers as a second parser.
                if len(bullets) != expected:
                    simple = re.split(r"\s+(?=\d+[.)]\s+)", block.strip())
                    alt = []
                    for item in simple:
                        item = re.sub(r"^\d+[.)]\s+", "", item).strip()
                        if item and not item.upper().startswith(("OUTPUT FORMAT", "RULES:", "JOB + CANDIDATE")):
                            alt.append(re.sub(r"\s+", " ", item))
                    if len(alt) == expected:
                        bullets = alt
                if len(bullets) != expected or any(not x.strip() for x in bullets):
                    raise RuntimeError(f"model returned {len(bullets)} bullets for experience entry {entry_number}; expected {expected}")
                return bullets

            parsed_entries = []
            for i, required_count, existing in entry_specs:
                try:
                    bullets = parse_entry_bullets(response_text, i + 1, required_count)
                except Exception as exc:
                    raise RuntimeError(f"Professional Experience repair could not be parsed for entry {i + 1}: {exc}") from exc
                parsed_entries.append({"bullets": bullets})
            repaired_entries = parsed_entries
        else:
            repaired_entries = [{"bullets": existing} for _, _, existing in entry_specs]

        data["experience"] = repaired_entries
        defective = [x for x in defective if x != "experience"]

    # Other small structured sections can still use JSON repair.
    schema = {
        "profile": "a concise tailored profile paragraph",
        "skills": f'exactly {len(expected_skills)} objects in this exact label order: {", ".join(expected_skills)}; each object has label and text',
        "project_bullets": f"exactly {project_count} concise string bullets",
        "additional_information": f"exactly {add_count} concise string bullets",
    }

    def _post_repair(repair_prompt: str, label: str, num_predict: int = 900) -> dict:
        last_exc: Exception | None = None
        for attempt in range(1, 3):
            if progress:
                progress(f"{model or 'Local AI'} is repairing {label}… (attempt {attempt}/2)")
            try:
                response = _ollama_chat_request(
                    {
                        "model": model or _local_ai_config(LOCAL_AI_DEFAULT)["model"],
                        "think": False,
                        "format": "json",
                        "messages": [
                            {"role": "system", "content": "Return only valid JSON. No Markdown, LaTeX, explanations, or reasoning."},
                            {"role": "user", "content": repair_prompt + "\n/no_think"},
                        ],
                        "options": {"temperature": 0.05, "num_predict": num_predict, "top_p": 0.75},
                    },
                    stream=False,
                    timeout=900,
                    progress=progress,
                )
                response.raise_for_status()
                content = str((response.json().get("message") or {}).get("content") or "").strip()
                repaired = _json_from_output(content)
                if not isinstance(repaired, dict):
                    raise RuntimeError("repair did not return a JSON object")
                return repaired
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(f"Local AI could not repair {label}: {last_exc}")

    for section in defective:
        requirement = schema[section]
        if section == "skills":
            shape = '{"skills":[{"label":"skill category","text":"tailored evidence"}]}'
        elif section in {"project_bullets", "additional_information"}:
            shape = '{"' + section + '":["item 1","item 2"]}'
        else:
            shape = '{"profile":"tailored profile"}'
        repair_prompt = f"""JOBSYNC CV CONTENT REPAIR

Generate ONLY the {section} section.

REQUIRED SHAPE:
{shape}

REQUIREMENT:
{requirement}

STRICT RULES:
- Return ONLY one valid JSON object with ONLY the key \"{section}\".
- Do not return Markdown, LaTeX, explanations, analysis, or reasoning.
- Use only facts supported by the JobSync evidence below.
- Never invent facts, employers, dates, qualifications, software, technologies, metrics, achievements, or responsibilities.
- Every required item must be present, non-empty, concise, and distinct.
- For skills, preserve non-blank template labels exactly and in order. Fill blank labels with an evidence-supported category.

JOB + CANDIDATE EVIDENCE:
{evidence_prompt}
"""
        repaired = _post_repair(repair_prompt, section.replace("_", " "), num_predict=1100)
        if section not in repaired:
            raise RuntimeError(f"repair did not return the required {section} key")
        value = repaired.get(section)
        if section == "profile":
            if not _valid_profile(value):
                raise RuntimeError("repair returned an empty profile")
            data[section] = str(value).strip()
        elif section == "skills":
            if not _valid_skills(value):
                raise RuntimeError(f"repair returned the wrong number of skill rows; expected {len(expected_skills)}")
            cleaned = []
            for i, expected_label in enumerate(expected_skills):
                row = value[i]
                generated_label = str(row.get("label") or "").strip()
                text = str(row.get("text") or "").strip()
                if not generated_label or not text:
                    raise RuntimeError(f"repair returned incomplete skill row {i + 1}")
                if expected_label and generated_label != expected_label:
                    raise RuntimeError(f"repair changed locked skill label {i + 1}")
                cleaned.append({"label": expected_label or generated_label, "text": text})
            data[section] = cleaned
        else:
            count = project_count if section == "project_bullets" else add_count
            if not _valid_array(value, count):
                raise RuntimeError(f"repair returned the wrong count for {section}; expected {count}")
            data[section] = [str(x).strip() for x in value]

    return data


# Backward-compatible name retained for any existing callers.
def _repair_local_cv_skills(data: dict, template: str, source_prompt: str, progress=None, model: str | None = None) -> dict:
    return _repair_local_cv_structure(data, template, source_prompt, progress=progress, model=model)



def _render_overleaf_login_button(key: str) -> None:
    """Open the normal Overleaf sign-in page in a new browser tab.

    JobSync does not call Overleaf's API or project/snippet import endpoints.
    The generated LaTeX remains visible in JobSync for manual copying.
    """
    st.link_button(
        "Continue to Overleaf ↗",
        "https://www.overleaf.com/login",
        type="primary",
        width="stretch",
        help="Sign in to Overleaf, create/open your project, and paste the copied LaTeX source.",
    )

def _open_local_path(path_value: str, select_file: bool = False) -> bool:
    """Open a managed local file/folder in Windows Explorer."""
    try:
        path = Path(str(path_value or "")).resolve()
        if select_file:
            if not path.exists() or not path.is_file():
                return False
            subprocess.Popen(["explorer.exe", "/select,", str(path)], close_fds=True)
        else:
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["explorer.exe", str(path)], close_fds=True)
        return True
    except Exception:
        return False


def _render_pdf_preview(pdf_path: str, title: str = "PDF preview") -> None:
    """Preview a local PDF without requiring the optional streamlit-pdf package."""
    path = Path(str(pdf_path or "")).resolve()
    if not path.exists() or not path.is_file() or path.suffix.lower() != ".pdf":
        st.info("No local PDF is available yet. Open the document in Overleaf to compile it, then save the PDF locally in JobSync.")
        return
    st.markdown(f"**{html.escape(title)}**")
    try:
        pdf_bytes = path.read_bytes()
        encoded = base64.b64encode(pdf_bytes).decode("ascii")
        iframe = (
            f'<iframe src="data:application/pdf;base64,{encoded}" width="100%" height="760" '
            f'style="border:1px solid #263241;border-radius:12px;background:#111" '
            f'title="{html.escape(title, quote=True)}"></iframe>'
        )
        components.html(iframe, height=780, scrolling=True)
    except Exception as exc:
        st.warning(f"Could not preview this PDF inside JobSync: {exc}")

def _ollama_chat_request(payload: dict, *, stream: bool, timeout: int, progress=None) -> requests.Response:
    """Call Ollama with compatibility fallbacks and useful server diagnostics.

    Some Ollama builds reject newer request fields such as ``think`` or structured
    ``format`` and return HTTP 500. JobSync retries once with those optional fields
    removed so CV generation still works on older local installations.
    """
    url = f"{OLLAMA_BASE_URL}/api/chat"
    body = dict(payload)
    body["stream"] = bool(stream)

    def _post(data: dict) -> requests.Response:
        try:
            response = requests.post(url, json=data, stream=stream, timeout=timeout)
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not reach the local AI engine at {OLLAMA_BASE_URL}. "
                f"Make sure Ollama is running and try again. Details: {exc}"
            ) from exc
        return response

    response = _post(body)
    if response.ok:
        return response

    raw = response.text.strip()
    detail = raw
    try:
        parsed = response.json()
        if isinstance(parsed, dict) and parsed.get("error"):
            detail = str(parsed.get("error"))
    except Exception:
        pass

    # Retry once for older/incompatible Ollama servers. Keep the model and messages,
    # but remove optional fields that commonly trigger 500 errors on older builds.
    retry_body = dict(body)
    changed = False
    if "think" in retry_body:
        retry_body.pop("think", None)
        changed = True
    if isinstance(retry_body.get("options"), dict):
        opts = dict(retry_body["options"])
        # Keep conservative generation controls only.
        for key in ("top_p",):
            opts.pop(key, None)
        retry_body["options"] = opts
    if isinstance(retry_body.get("format"), str):
        # Preserve JSON generation when the server advertises support; if it rejects
        # the field, the fallback removes it and lets the parser validate the output.
        retry_body.pop("format", None)
        changed = True

    if changed and response.status_code >= 500:
        if progress:
            progress("The local AI engine rejected an advanced request format. Retrying in compatibility mode…")
        retry = _post(retry_body)
        if retry.ok:
            return retry
        retry_raw = retry.text.strip()
        retry_detail = retry_raw
        try:
            parsed = retry.json()
            if isinstance(parsed, dict) and parsed.get("error"):
                retry_detail = str(parsed.get("error"))
        except Exception:
            pass
        raise RuntimeError(
            f"Local AI server returned HTTP {retry.status_code}: {retry_detail or 'Internal server error'}. "
            f"Model: {body.get('model', 'unknown')}."
        )

    raise RuntimeError(
        f"Local AI server returned HTTP {response.status_code}: {detail or 'Internal server error'}. "
        f"Model: {body.get('model', 'unknown')}."
    )


def _generate_with_auto_router(prompt: str, document_type: str = "CV", template: str = "") -> str:
    """Free-tier router: try each connected free model in order, and switch to
    the next one automatically when the current one is rate-limited, missing
    its key, or otherwise fails — instead of making the user pick a model and
    manually retry after every 429."""
    progress = st.session_state.get("cv_ai_progress_callback")
    tried: list[str] = []
    errors: list[str] = []
    for candidate_name in AUTO_FREE_MODEL_CHAIN:
        cfg = HOSTED_AI_MODELS.get(candidate_name)
        if not cfg:
            continue
        candidate_provider = cfg["provider"]
        if not _ai_api_key(candidate_provider):
            continue  # no key configured for this provider — skip silently
        tried.append(candidate_name)
        if progress:
            progress(f"Free AI router: trying {candidate_name}…")
        try:
            return _generate_latex_with_ai(candidate_name, prompt, document_type=document_type, template=template)
        except Exception as exc:
            errors.append(f"{candidate_name}: {exc}")
            if progress:
                progress(f"{candidate_name} failed — switching to the next free model…")
            continue
    if not tried:
        raise RuntimeError(
            "The free AI router has no connected key yet. Add a free Gemini or Groq key once in "
            "Settings → AI generation."
        )
    raise RuntimeError(
        "Every connected free AI model failed or is rate-limited right now: "
        + " | ".join(errors)
        + ". Wait a minute and try again, or connect another free provider key in Settings."
    )


def _generate_latex_with_ai(provider: str, prompt: str, document_type: str = "CV", template: str = "") -> str:
    """Generate LaTeX inside JobSync. Local Qwen3 is the no-key default."""
    resolved_selection, _ = _resolve_ai_selection(provider)
    if resolved_selection == "Auto":
        return _generate_with_auto_router(prompt, document_type=document_type, template=template)

    system = (
        "Return exactly one complete LaTeX document in a single latex code block and nothing else. "
        "Follow the user's prompt exactly. Never invent facts, employers, dates, qualifications, "
        "or experience. Preserve the supplied template structure and commands."
    )

    if _is_local_ai_provider(provider):
        local_key = _local_ai_key(provider)
        local_cfg = _local_ai_config(local_key)
        local_model = local_cfg["model"]
        _ensure_local_ai(local_key, progress=st.session_state.get("cv_ai_progress_callback"))
        progress = st.session_state.get("cv_ai_progress_callback")
        if progress:
            progress(f"{local_key} is ready. Writing the document content…")

        if document_type == "CV":
            # SIMPLE CV MODE
            # ----------------
            # The model receives the complete generation prompt, the uploaded/reference
            # CV evidence, the target vacancy and the LaTeX template. It writes ONE
            # complete LaTeX document. JobSync does not ask the model to produce a
            # secondary JSON representation and never runs an experience-repair loop.
            # This is intentionally a single AI -> LaTeX handoff.
            cv_instructions = load_ai_cv_generation_prompt()
            local_prompt = prompt + r"""

==================== JOBSYNC SIMPLE CV MODE ====================

You are generating the FINAL CV source directly.

TASK
Read the candidate/reference CV data, the target job description, the candidate profile,
and the master HR-focused CV instructions above. Then write ONE complete, finished LaTeX
CV tailored to the target job.

OUTPUT
- Return ONLY the complete LaTeX source.
- Start with \\documentclass and finish with \\end{document}.
- Put it in exactly one ```latex``` code block.
- Do not return JSON.
- Do not return explanations.
- Do not return a content summary.
- Do not return analysis or reasoning.
- Do not ask for missing information.

FACTS
- The uploaded/reference CV is the factual source of truth.
- Use the target vacancy only to decide what genuine candidate evidence to emphasize.
- Never invent experience, employers, titles, dates, technologies, metrics, education,
  certifications, projects, responsibilities, achievements or seniority.
- Never turn a job-description requirement into candidate experience.
- Never copy the reference CV unchanged; rewrite relevant content naturally for the target job.

SECTIONS
- First determine which sections are actually supported by the uploaded/reference CV.
- Generate only sections supported by the candidate evidence.
- If the reference CV has no section (for example Professional Experience, Projects,
  Skills, Certifications, etc.), do NOT invent content for that section.
- A missing source section must never cause generation to fail.
- If the supplied template contains a section that is absent from the reference CV, leave
  that section without generated candidate content or omit that section cleanly, whichever
  is required by the template to produce valid LaTeX. Never fill it with fabricated text.
- Do not invent placeholder content merely to make a section look complete.

LATEX
- Use the supplied master LaTeX template as the visual/design framework.
- Preserve its document class, preamble, packages, commands, typography, geometry,
  spacing, colors, header design and visual hierarchy.
- Change candidate/job content only.
- Keep LaTeX syntactically valid and compilable.
- Escape LaTeX special characters correctly.
- The result must be ready to copy directly into Overleaf.

FINAL CHECK BEFORE OUTPUT
Silently verify that every claim is supported by the reference CV/profile, every selected
job keyword is truthful, missing source sections were not invented, and the LaTeX is a
complete document. Then output only the final LaTeX.

/no_think
"""

            if progress:
                progress(f"{local_key} is reading the reference CV and job requirements…")

            response = _ollama_chat_request(
                {
                    "model": local_model,
                    "stream": True,
                    "think": False,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                cv_instructions
                                + "\\n\\nJOBSYNC RULE: For this request, write the FINAL COMPLETE LATEX CV directly. "
                                  "Do not output JSON, do not split the work into sections, and do not create "
                                  "a repair request. The reference CV is the factual source of truth. "
                                  "If a source section is missing, do not invent it."
                            ),
                        },
                        {"role": "user", "content": local_prompt},
                    ],
                    "options": {
                        "temperature": 0.18,
                        "num_predict": 12000,
                        "top_p": 0.85,
                    },
                },
                stream=True,
                timeout=1800,
                progress=progress,
            )
            response.raise_for_status()

            chunks: list[str] = []
            for raw in response.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                try:
                    streamed = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if streamed.get("error"):
                    raise RuntimeError(str(streamed["error"]))
                msg = streamed.get("message") or {}
                piece = str(msg.get("content") or "")
                if piece:
                    chunks.append(piece)
                    total_chars = sum(len(x) for x in chunks)
                    st.session_state["cv_local_ai_chars"] = total_chars
                    if progress and (len(chunks) == 1 or len(chunks) % 12 == 0):
                        progress(f"{local_key} is writing the complete LaTeX CV… ({total_chars:,} characters)")
                if streamed.get("done"):
                    break

            content = "".join(chunks).strip()
            if "</think>" in content:
                content = content.rsplit("</think>", 1)[1].strip()
            latex = extract_latex_code(content)
            if not latex:
                # One lightweight retry only: ask for the same complete document again,
                # never a JSON/experience repair. This protects against a model that
                # stopped before emitting the code fence or document boundaries.
                if progress:
                    progress(f"{local_key} did not return a complete LaTeX document. Retrying once…")
                retry = _ollama_chat_request(
                    {
                        "model": local_model,
                        "stream": False,
                        "think": False,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "Return ONLY one complete LaTeX CV. Start with \\documentclass and "
                                    "finish with \\end{document}. Do not output JSON, explanations, "
                                    "or reasoning. Use only facts in the supplied reference CV."
                                ),
                            },
                            {
                                "role": "user",
                                "content": local_prompt + "\\n\\nFINAL RETRY: Output the complete LaTeX document now.\\n/no_think",
                            },
                        ],
                        "options": {"temperature": 0.1, "num_predict": 12000, "top_p": 0.8},
                    },
                    stream=False,
                    timeout=1800,
                    progress=progress,
                )
                retry.raise_for_status()
                retry_json = retry.json()
                retry_content = str((retry_json.get("message") or {}).get("content") or "").strip()
                if "</think>" in retry_content:
                    retry_content = retry_content.rsplit("</think>", 1)[1].strip()
                latex = extract_latex_code(retry_content)

            if not latex:
                raise RuntimeError(
                    f"{local_key} did not return a complete LaTeX CV. "
                    "The model must return the final LaTeX source directly."
                )

            if progress:
                progress(f"{local_key} returned the complete CV source. Validating LaTeX…")
            return latex

        # Cover letters still use complete LaTeX, but explicitly disable thinking.
        local_prompt = prompt + r"""

IMPORTANT JOBSYNC LOCAL-COVER-LETTER MODE — OVERRIDE ANY EARLIER OUTPUT INSTRUCTIONS:
Return only the complete LaTeX document in one ```latex``` block. Do not return analysis or explanations.
/no_think
"""
        response = _ollama_chat_request(
            {
                "model": local_model,
                "think": False,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": local_prompt},
                ],
                "options": {"temperature": 0.2, "num_predict": 5000},
            },
            stream=True,
            timeout=1200,
            progress=progress,
        )
        response.raise_for_status()
        chunks = []
        for raw in response.iter_lines(decode_unicode=True):
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if data.get("error"):
                raise RuntimeError(str(data["error"]))
            msg = data.get("message") or {}
            piece = str(msg.get("content") or "")
            if piece:
                chunks.append(piece)
            if data.get("done"):
                break
        content = "".join(chunks).strip()
        if "</think>" in content:
            content = content.rsplit("</think>", 1)[1].strip()
        if not content:
            raise RuntimeError("Qwen3 returned no visible document content. Please try Generate again.")
        return content
    resolved_provider, model_override = _resolve_ai_selection(provider)
    key = _ai_api_key(provider)
    if not key:
        raise RuntimeError(f"Connect {provider} first by supplying its API key in this page, or set the provider API key in the JobSync environment.")

    if resolved_provider == "ChatGPT":
        model = model_override or os.getenv("JOBSYNC_OPENAI_MODEL", "gpt-4o-mini")
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "instructions": system, "input": prompt}, timeout=180,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("output_text"):
            return str(data["output_text"])
        parts=[]
        for item in data.get("output", []):
            for content in item.get("content", []) if isinstance(item, dict) else []:
                if isinstance(content, dict) and content.get("text"):
                    parts.append(str(content["text"]))
        return "\n".join(parts)

    if resolved_provider == "Groq":
        model = model_override or os.getenv("JOBSYNC_GROQ_MODEL", "llama-3.3-70b-versatile")
        attempt = 0
        max_retries = 4
        while True:
            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}], "temperature": 0.2}, timeout=180,
            )
            if response.status_code == 429 and attempt < max_retries:
                retry_after = response.headers.get("Retry-After")
                try:
                    wait_s = float(retry_after) if retry_after else (2 ** attempt) * 3
                except ValueError:
                    wait_s = (2 ** attempt) * 3
                wait_s = min(wait_s, 60)
                progress = st.session_state.get("cv_ai_progress_callback")
                if progress:
                    progress(f"Groq rate limit hit — retrying in {int(wait_s)}s ({attempt + 1}/{max_retries})…")
                time.sleep(wait_s)
                attempt += 1
                continue
            break
        if response.status_code == 429:
            raise RuntimeError(
                "Groq's free-tier rate limit is still exceeded after several retries. "
                "Wait a minute and try again, or switch to Gemini for this generation."
            )
        if not response.ok:
            # raise_for_status() alone only reports the status code ("400
            # Bad Request") with no indication of what was actually wrong —
            # Groq's error body names the real cause (e.g. a decommissioned
            # model id, or the request exceeding the model's context
            # window), so surface that instead of a bare status code.
            detail = ""
            try:
                detail = str((response.json().get("error") or {}).get("message") or "")
            except Exception:
                detail = response.text[:300]
            raise RuntimeError(f"Groq rejected the request ({response.status_code}): {detail or 'no further detail returned.'}")
        data = response.json()
        choices = data.get("choices", [])
        return str((choices[0].get("message") or {}).get("content") or "") if choices else ""

    if resolved_provider == "Claude":
        model = model_override or os.getenv("JOBSYNC_ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            json={"model": model, "max_tokens": 20000, "system": system, "messages": [{"role": "user", "content": prompt}]}, timeout=180,
        )
        response.raise_for_status()
        data = response.json()
        return "\n".join(str(x.get("text", "")) for x in data.get("content", []) if isinstance(x, dict))

    if resolved_provider == "Gemini":
        # Google periodically retires pinned model ids (e.g. gemini-2.0-flash,
        # gemini-1.5-pro), which turns into a hard 404 for every user on the
        # old id. Try the "-latest" alias Google keeps pointed at whatever is
        # current first, then fall back through a short list of known ids so
        # one retirement never breaks generation outright.
        preferred = model_override or os.getenv("JOBSYNC_GEMINI_MODEL", "")
        candidates = [m for m in [preferred, "gemini-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash"] if m]
        last_error: Exception | None = None
        # Free-tier Gemini keys hit a per-minute rate limit under normal use;
        # a bare 429 used to hard-fail generation immediately. Retry a few
        # times with backoff (honoring Retry-After when Google sends one)
        # before giving up, so a transient rate limit self-resolves instead
        # of forcing the user to click "Try generation again" by hand.
        max_retries = 4
        for candidate_model in dict.fromkeys(candidates):
            attempt = 0
            while True:
                response = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{candidate_model}:generateContent",
                    headers={"Content-Type": "application/json"}, params={"key": key},
                    json={"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.2}}, timeout=180,
                )
                if response.status_code == 429 and attempt < max_retries:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        wait_s = float(retry_after) if retry_after else (2 ** attempt) * 3
                    except ValueError:
                        wait_s = (2 ** attempt) * 3
                    wait_s = min(wait_s, 60)
                    progress = st.session_state.get("cv_ai_progress_callback")
                    if progress:
                        progress(f"Gemini rate limit hit — retrying in {int(wait_s)}s ({attempt + 1}/{max_retries})…")
                    time.sleep(wait_s)
                    attempt += 1
                    continue
                break
            if response.status_code == 404:
                last_error = RuntimeError(f"Gemini model '{candidate_model}' returned 404 Not Found.")
                continue
            if response.status_code == 429:
                raise RuntimeError(
                    "Gemini's free-tier rate limit is still exceeded after several retries. "
                    "Wait a minute and try again, or enable billing on this API key at "
                    "aistudio.google.com/apikey for a much higher limit."
                )
            if not response.ok:
                # raise_for_status() alone would only report the bare status
                # code — Gemini's error body names the real cause (invalid
                # request, safety block, quota, disabled API, etc).
                detail = ""
                try:
                    detail = str((response.json().get("error") or {}).get("message") or "")
                except Exception:
                    detail = response.text[:300]
                raise RuntimeError(f"Gemini rejected the request ({response.status_code}) for '{candidate_model}': {detail or 'no further detail returned.'}")
            data = response.json()
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            return "\n".join(str(x.get("text", "")) for x in parts if isinstance(x, dict))
        raise RuntimeError(
            f"None of Gemini's known model ids are available for this API key ({last_error}). "
            "Check that the Generative Language API is enabled for the key's project at "
            "aistudio.google.com/apikey, or set JOBSYNC_GEMINI_MODEL to a current model id "
            "from ai.google.dev/gemini-api/docs/models."
        )

    raise RuntimeError(f"Unsupported AI provider: {provider}")

def save_upload(uploaded_file, folder: Path) -> Path:
    target = folder / Path(uploaded_file.name).name
    target.write_bytes(uploaded_file.getbuffer())
    return target


def delete_managed_file(path_value: str | Path) -> bool:
    """Delete a file only when it is inside JobSync-managed folders."""
    if not path_value:
        return False
    try:
        path = Path(path_value).resolve()
        managed_roots = [
            UPLOAD_CV.resolve(),
            UPLOAD_CL.resolve(),
            UPLOAD_REFERENCES.resolve(),
            OUTPUT_CV.resolve(),
            OUTPUT_CL.resolve(),
            CV_LIBRARY_DIR.resolve(),
            USER_BLUEPRINT_DIR.resolve(),
        ]
        if not any(path == root or root in path.parents for root in managed_roots):
            return False
        if path.exists() and path.is_file():
            path.unlink()
            return True
    except Exception:
        return False
    return False


def remove_document(doc: dict) -> None:
    """Remove a document record and any managed companion PDF, then clear tracker links."""
    doc_path = doc.get("path", "")
    delete_managed_file(doc_path)
    delete_managed_file(doc.get("library_path", ""))
    delete_managed_file(doc.get("pdf_path", ""))
    for application in state.get("applied", []):
        if application.get("cv_path") == doc_path:
            application["cv_path"] = ""
        if application.get("cover_letter_path") == doc_path:
            application["cover_letter_path"] = ""
    state["documents"] = [
        d for d in state.get("documents", [])
        if d is not doc and not (d.get("path") == doc_path and d.get("kind") == doc.get("kind"))
    ]
    save_state(state)


def generated_cvs():
    return [d for d in state["documents"] if d.get("kind") == "generated_cv"]


def reference_cvs():
    return [d for d in state["documents"] if d.get("kind") == "reference_cv"]


def generated_letters():
    return [d for d in state["documents"] if d.get("kind") == "generated_coverletter"]


def reference_coverletters():
    return [d for d in state["documents"] if d.get("kind") == "reference_coverletter"]


def clear_temporary_cv_references() -> int:
    """Clear temporary CV Studio reference uploads after a successful generation.

    Persistent CVs uploaded from the Folders page use ``uploaded_cv`` and are kept.
    The unified CV Studio uploader uses ``reference_*`` kinds as temporary evidence
    for the current generation, so removing them prevents stale CV/cover-letter data
    from leaking into the next prompt.
    """
    temporary_kinds = {"reference_cv", "reference_coverletter", "reference_document"}
    removed = 0
    kept = []
    for doc in state.get("documents", []):
        if doc.get("kind") in temporary_kinds:
            delete_managed_file(doc.get("path", ""))
            delete_managed_file(doc.get("library_path", ""))
            delete_managed_file(doc.get("pdf_path", ""))
            delete_managed_file(doc.get("pdf_text_path", ""))
            removed += 1
        else:
            kept.append(doc)
    if removed:
        state["documents"] = kept
        save_state(state)
    return removed


def _bookmark_key(job: dict) -> str:
    """Stable key for a bookmarked vacancy, preferring its posting URL."""
    url = str(job.get("url") or "").strip().lower()
    if url:
        return f"url:{url}"
    return "job:" + "|".join([
        str(job.get("title") or "").strip().lower(),
        str(job.get("company") or "").strip().lower(),
        str(job.get("location") or "").strip().lower(),
    ])


def _is_bookmarked(job: dict) -> bool:
    key = _bookmark_key(job)
    return any(_bookmark_key(item) == key for item in (state.get("bookmarks") or []) if isinstance(item, dict))


def _toggle_bookmark(job: dict) -> tuple[bool, str]:
    """Add a job to bookmarks or remove the existing bookmark."""
    bookmarks = state.setdefault("bookmarks", [])
    key = _bookmark_key(job)
    for idx, item in enumerate(bookmarks):
        if isinstance(item, dict) and _bookmark_key(item) == key:
            bookmarks.pop(idx)
            save_state(state)
            return False, "Removed from bookmarks."
    bookmarked = dict(job)
    bookmarked["bookmarked_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bookmarks.insert(0, bookmarked)
    del bookmarks[100:]
    save_state(state)
    return True, "Saved to bookmarks."


def reset_cv_studio_for_new_preparation(*, keep_selected_job: bool = False, selected_job: dict | None = None) -> None:
    """Start a completely fresh CV/cover-letter preparation cycle.

    Widget keys are versioned by ``cv_studio_cycle``, so clearing the backing
    session values and advancing the cycle prevents Streamlit from restoring
    the previous vacancy's text inputs, choices, prompts, or generated state.
    """
    cv_keys = (
        "external_ai_prompt", "external_ai_provider",
        "external_document_type_snapshot", "external_job_snapshot",
        "external_template_snapshot", "cv_wizard_doc", "cv_wizard_ai",
        "cv_wizard_source", "cv_wizard_job", "cv_wizard_manual_job",
        "cv_wizard_template", "cv_latex_draft", "cv_latex_path",
        "cv_compiled_pdf", "cv_saved_pdf", "cv_compile_error",
        "cv_generation_status", "cv_generation_error", "cv_local_ai_chars",
        "cv_generation_percent", "cv_ai_progress_callback", "cv_generation_running", "cv_blueprint_name",
        "cv_generated_base", "cv_last_download_name",
    )
    for key in cv_keys:
        st.session_state.pop(key, None)
    st.session_state["cv_studio_cycle"] = int(st.session_state.get("cv_studio_cycle", 0)) + 1
    if keep_selected_job and isinstance(selected_job, dict):
        # Prepare CV from a search result: open directly on the job-details step
        # with this exact vacancy pre-filled rather than making the user choose it again.
        st.session_state["cv_entry_job"] = dict(selected_job)
        st.session_state["cv_wizard_doc"] = "CV"
        st.session_state["cv_wizard_step"] = 3
    else:
        st.session_state["cv_entry_job"] = {}
        st.session_state["cv_wizard_step"] = 1


def latest_reference_documents(max_each_chars: int = 14000) -> list[dict]:
    """Return the newest stored CV and cover-letter references for prompt assembly."""
    docs = state.get("documents", [])
    selected = []
    seen_types = set()
    for doc in reversed(docs):
        if doc.get("kind") not in {"reference_cv", "reference_coverletter", "reference_document", "uploaded_cv"}:
            continue
        path = Path(str(doc.get("path") or ""))
        if not path.exists():
            continue
        ref_type = str(doc.get("reference_type") or "document").lower()
        if ref_type in {"cv", "cover_letter"} and ref_type in seen_types:
            continue
        try:
            text = extract_text(path).strip()
        except Exception:
            text = str(doc.get("text") or "").strip()
        if not text:
            continue
        selected.append({
            "name": str(doc.get("name") or path.name),
            "text": text[:max_each_chars],
            "reference_type": ref_type,
        })
        if ref_type in {"cv", "cover_letter"}:
            seen_types.add(ref_type)
        if {"cv", "cover_letter"}.issubset(seen_types):
            break
    return list(reversed(selected))


def save_reference_uploads(uploaded_files: list) -> int:
    """Store one or more latest candidate documents from the unified uploader."""
    saved = 0
    for uploaded in uploaded_files:
        if uploaded is None:
            continue
        original_name = Path(uploaded.name).name
        lower = original_name.lower()
        if any(token in lower for token in ("cover", "letter", "anschreiben")):
            ref_type = "cover_letter"
            kind = "reference_coverletter"
        elif any(token in lower for token in ("cv", "resume", "lebenslauf")):
            ref_type = "cv"
            kind = "reference_cv"
        else:
            ref_type = "document"
            kind = "reference_document"
        target = unique_doc_path(UPLOAD_REFERENCES, Path(original_name).stem, Path(original_name).suffix or ".bin")
        target.write_bytes(uploaded.getbuffer())
        try:
            text = extract_text(target).strip()
        except Exception as exc:
            text = ""
        state.setdefault("documents", []).append({
            "name": target.name,
            "kind": kind,
            "reference_type": ref_type,
            "path": str(target),
            "pdf_path": "",
            "pdf_text_path": "",
            "text_chars": len(text),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })
        saved += 1
    if saved:
        save_state(state)
    return saved


def _normalize_generated_pdf_records() -> bool:
    """Keep generated-document records PDF-only. Never delete user-uploaded source files."""
    changed = False
    for doc in state.get("documents", []):
        if doc.get("kind") not in {"generated_cv", "generated_coverletter"}:
            continue
        # New generated records are already PDF-only. Legacy records may still point
        # at generated .tex files; clear those fields so the library does not expose
        # stale generated source files. The physical legacy source is left untouched
        # rather than risking deletion of a user's data.
        if str(doc.get("tex_path") or "").strip():
            doc["tex_path"] = ""
            changed = True
        if str(doc.get("path") or "").lower().endswith(".tex"):
            doc["path"] = str(doc.get("pdf_path") or "")
            doc["name"] = Path(str(doc.get("path") or "")).name if doc.get("path") else ""
            changed = True
    if changed:
        save_state(state)
    return changed


def cv_document_records() -> list[dict]:
    """Return every locally managed CV record, generated or uploaded."""
    _normalize_generated_pdf_records()
    allowed = {"generated_cv", "reference_cv", "uploaded_cv"}
    return [d for d in state.get("documents", []) if d.get("kind") in allowed]


def application_for_cv(doc: dict) -> dict | None:
    """Find the most recent application associated with a CV path or job metadata."""
    doc_path = str(doc.get("path") or "")
    job_title = str(doc.get("job_title") or "").strip().lower()
    company = str(doc.get("company") or "").strip().lower()
    candidates = []
    for app in state.get("applied", []):
        if doc_path and str(app.get("cv_path") or "") == doc_path:
            candidates.append(app)
            continue
        if job_title and str(app.get("title") or "").strip().lower() == job_title:
            if not company or str(app.get("company") or "").strip().lower() == company:
                candidates.append(app)
    if not candidates:
        return None
    return sorted(candidates, key=lambda a: str(a.get("applied_date") or ""), reverse=True)[0]


def cv_position_and_date(doc: dict) -> tuple[str, str]:
    app = application_for_cv(doc)
    if app:
        pos = str(app.get("title") or doc.get("job_title") or "").strip()
        date = str(app.get("applied_date") or doc.get("created_at") or "")[:10]
        return pos or "Application CV", date
    pos = str(doc.get("job_title") or "").strip()
    if not pos and doc.get("reference_type") == "cv":
        pos = "Reference / uploaded CV"
    date = str(doc.get("created_at") or "")[:10]
    return pos or "General CV", date


def cv_kind_label(doc: dict) -> str:
    kind = doc.get("kind")
    if kind == "generated_cv":
        return "Generated"
    if kind == "uploaded_cv":
        return "Uploaded"
    return "Reference"


def open_file_anchor(path_value: str, label: str = "Open CV") -> str:
    """Create a browser-open link for managed PDF/TEX/TXT files."""
    path = Path(str(path_value or "")).resolve()
    managed_roots = [UPLOAD_CV.resolve(), UPLOAD_REFERENCES.resolve(), OUTPUT_CV.resolve(), CV_LIBRARY_DIR.resolve()]
    if not path.exists() or not path.is_file() or not any(path == root or root in path.parents for root in managed_roots):
        return ""
    try:
        data = path.read_bytes()
        import base64
        ext = path.suffix.lower()
        mime = {
            ".pdf": "application/pdf",
            ".tex": "text/plain;charset=utf-8",
            ".txt": "text/plain;charset=utf-8",
        }.get(ext)
        if not mime:
            return ""
        href = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        return f'<a href="{href}" target="_blank" rel="noopener" style="display:inline-block;padding:7px 11px;border:1px solid #2d3742;border-radius:8px;background:#111418;color:#f5f7fa;text-decoration:none;font-weight:700;font-size:12px">↗ {html.escape(label)}</a>'
    except Exception:
        return ""

def status_class(status: str) -> str:
    s = (status or "Applied").lower()
    if "interview" in s:
        return "status-interview"
    if "offer" in s:
        return "status-offer"
    if "reject" in s or "withdraw" in s:
        return "status-rejected"
    return "status-applied"


def latest_updates(limit=7):
    items = []
    for h in state.get("search_history", [])[-10:]:
        items.append((h.get("searched_at", ""), "search", f"Searched for {h.get('field') or 'jobs'} in {h.get('location') or 'your target area'} — {h.get('count', 0)} jobs found."))
    for a in state.get("applied", [])[-10:]:
        items.append((a.get("applied_date", ""), "apply", f"Application recorded: {a.get('title', 'Job')} at {a.get('company', 'company')}."))
    for d in state.get("documents", [])[-10:]:
        if d.get("kind") == "generated_cv":
            items.append((d.get("created_at", ""), "cv", f"CV generated for {d.get('job_title', 'job')} at {d.get('company', 'company')}."))
        elif d.get("kind") == "generated_coverletter":
            items.append((d.get("created_at", ""), "letter", f"Cover letter generated for {d.get('job_title', 'job')} at {d.get('company', 'company')}."))
    items.sort(key=lambda x: x[0] or "", reverse=True)
    return items[:limit]


# ---------------- Sidebar ----------------
# Automatic update check (start + periodically) and Supabase presence heartbeat/cache.
_maybe_auto_update_check()
_presence_heartbeat()
_refresh_online_cache()

# Digest banner shown when a newer release is available.
if st.session_state.get("_update_banner"):
    _update_tag = html.escape(str(st.session_state["_update_banner"]))
    st.markdown(
        f'<div class="card" style="border-left:4px solid var(--jf-green);">'
        f'<div class="section-title">⬆ Update available — v{_update_tag}</div>'
        '<div style="color:#c8ced6;font-size:.8rem;line-height:1.5;">A newer version of JobSync is ready. '
        'Go to <b>Updates → Software update</b> and press <b>Check for updates</b> to download it.</div>'
        '</div>',
        unsafe_allow_html=True,
    )

with st.sidebar:
    # JOBSYNC NAV RAIL — auto-hide, hover-expand sidebar.
    #
    # Replaces the earlier stack of three separate, width-breakpoint-driven nav
    # systems (a static 248px sidebar above 1200px, a "compact rail" between
    # 760-1200px that faked icon-only via font-size:0 + ::first-letter, and a
    # "final narrow mode" fixed-overlay variant below that) which fought each
    # other for section[data-testid="stSidebar"]'s width/position on every
    # resize — the actual cause of the reported unsynced layout and stray
    # black bar. There is now a single rail, at every window width: it rests
    # collapsed to an icon-only strip and expands on hover/keyboard-focus,
    # overlaying the page rather than reflowing it, then collapses again the
    # moment the pointer leaves.
    #
    # Note on the ::first-letter icon trick it replaces: for a label typed as
    # "<icon>   <text>" (e.g. "⌂   Home"), CSS ::first-letter selects the
    # first *letter*, not the first character — for a symbol/emoji icon like
    # ⌂ or ↪ it skips straight to "H" or the sign-out label's first letter,
    # so the icon was invisible in the old compact rail. Simple left-aligned
    # overflow clipping has no such problem: the icon is always first in
    # reading order, so it is always the part that stays visible when the
    # rail is narrow.
    st.markdown("""
    <style>
      :root {
        --jsync-nav-collapsed: 76px;
        --jsync-nav-expanded: min(258px, 90vw);
      }

      /* NOTE ON SPECIFICITY: Streamlit does not place a component's <style>
         tags in the document in Python call order — this block, although the
         last sidebar-related markdown the script emits, actually lands
         *earlier* in the DOM than several older width/position rules further
         up this file (an leftover "small windows/tablets/phones" overlay
         system at ~900px that predates this rail). Source order alone would
         let those older, narrower-breakpoint rules win below 900px width. The
         selectors below repeat the same attribute match
         ([data-testid="stSidebar"][data-testid="stSidebar"]) purely to add
         one extra attribute-selector's worth of specificity — a harmless,
         valid CSS way to make sure the rail's own sizing always wins,
         independent of where either rule happens to land in the DOM. */
      section[data-testid="stSidebar"][data-testid="stSidebar"] {
        position: fixed !important;
        left: 0 !important; top: 0 !important; bottom: 0 !important;
        height: 100dvh !important;
        width: var(--jsync-nav-collapsed) !important;
        min-width: var(--jsync-nav-collapsed) !important;
        max-width: var(--jsync-nav-collapsed) !important;
        flex: 0 0 var(--jsync-nav-collapsed) !important;
        z-index: 999999 !important;
        overflow: hidden !important;
        background: linear-gradient(180deg, rgba(6,12,25,.97), rgba(4,7,14,.98)) !important;
        border-right: 1px solid rgba(120,151,201,.12) !important;
        box-shadow: none !important;
        transition: width .28s cubic-bezier(.22,.9,.32,1), box-shadow .28s ease !important;
      }
      section[data-testid="stSidebar"][data-testid="stSidebar"]:hover,
      section[data-testid="stSidebar"][data-testid="stSidebar"]:focus-within {
        width: var(--jsync-nav-expanded) !important;
        min-width: var(--jsync-nav-expanded) !important;
        max-width: var(--jsync-nav-expanded) !important;
        overflow-y: auto !important; overflow-x: hidden !important;
        box-shadow: 20px 0 60px rgba(0,0,0,.5), 0 0 40px rgba(75,216,255,.05) !important;
      }
      /* The inner content wrapper must track the section's own width at all
         times — if it jumped straight to the expanded width while the
         section stayed clipped to the collapsed width, every button would
         lay out off-screen and no icon would be visible at all. */
      section[data-testid="stSidebar"][data-testid="stSidebar"] > div:first-child {
        width: var(--jsync-nav-collapsed) !important;
        min-width: var(--jsync-nav-collapsed) !important;
        padding: .75rem .4rem !important;
        transition: width .28s cubic-bezier(.22,.9,.32,1), padding .28s ease !important;
      }
      section[data-testid="stSidebar"][data-testid="stSidebar"]:hover > div:first-child,
      section[data-testid="stSidebar"][data-testid="stSidebar"]:focus-within > div:first-child {
        width: var(--jsync-nav-expanded) !important;
        min-width: var(--jsync-nav-expanded) !important;
        padding: .9rem .8rem !important;
      }
      /* Content always reserves only the collapsed width — the expanded
         rail floats above it as an overlay so hovering never reflows it. */
      div[data-testid="stAppViewContainer"] > .main,
      .stMain {
        margin-left: var(--jsync-nav-collapsed) !important;
      }

      /* Brand row: logo mark stays put, wordmark fades/slides in on expand. */
      section[data-testid="stSidebar"] .jobsync-brand-row {
        justify-content: center !important; margin: .25rem 0 .85rem !important;
      }
      section[data-testid="stSidebar"]:hover .jobsync-brand-row,
      section[data-testid="stSidebar"]:focus-within .jobsync-brand-row {
        justify-content: flex-start !important; margin: .35rem 0 1rem !important;
      }
      section[data-testid="stSidebar"] .jobsync-logo-mark {
        width: 42px !important; height: 42px !important; flex: 0 0 42px !important;
        transition: width .22s ease, height .22s ease, flex-basis .22s ease !important;
      }
      section[data-testid="stSidebar"]:hover .jobsync-logo-mark,
      section[data-testid="stSidebar"]:focus-within .jobsync-logo-mark {
        width: 46px !important; height: 46px !important; flex: 0 0 46px !important;
      }
      section[data-testid="stSidebar"] .brand-copy { display: none !important; }
      section[data-testid="stSidebar"]:hover .brand-copy,
      section[data-testid="stSidebar"]:focus-within .brand-copy { display: block !important; }
      section[data-testid="stSidebar"] .brand-copy .brand-sub {
        font-family: "Arial Rounded MT Bold", "Trebuchet MS", Inter, system-ui, sans-serif !important;
        font-size: .61rem !important; font-weight: 700 !important; letter-spacing: -.01em !important;
      }

      /* Category labels + spacing: only meaningful once the rail is expanded. */
      section[data-testid="stSidebar"] .nav-category-label { display: none !important; }
      section[data-testid="stSidebar"]:hover .nav-category-label,
      section[data-testid="stSidebar"]:focus-within .nav-category-label {
        display: block !important; margin: 2px 6px 7px; color: #5f718b;
        font-size: .50rem; font-weight: 950; letter-spacing: .18em; text-transform: uppercase;
      }
      section[data-testid="stSidebar"] .nav-category-gap { height: 9px; transition: height .22s ease; }
      section[data-testid="stSidebar"] .nav-divider-space { height: 5px; transition: height .22s ease; }
      section[data-testid="stSidebar"]:hover .nav-category-gap,
      section[data-testid="stSidebar"]:focus-within .nav-category-gap { height: 18px; }
      section[data-testid="stSidebar"]:hover .nav-divider-space,
      section[data-testid="stSidebar"]:focus-within .nav-divider-space { height: 8px; }

      /* Nav buttons */
      section[data-testid="stSidebar"] .stButton { margin: .16rem 0 !important; transition: margin .22s ease !important; }
      section[data-testid="stSidebar"]:hover .stButton,
      section[data-testid="stSidebar"]:focus-within .stButton { margin: .20rem 0 !important; }
      section[data-testid="stSidebar"] .stButton > button {
        position: relative !important; overflow: hidden !important;
        width: 100% !important;
        min-height: 50px !important; height: 50px !important;
        padding: 0 0 0 1.05rem !important;
        border-radius: 14px !important; display: flex !important; align-items: center !important;
        justify-content: flex-start !important; gap: 12px !important;
        white-space: nowrap !important;
        font-family: "Arial Rounded MT Bold", "Trebuchet MS", Inter, system-ui, -apple-system, sans-serif !important;
        font-size: .94rem !important; font-weight: 900 !important; letter-spacing: -.032em !important;
        line-height: 1 !important; text-rendering: geometricPrecision !important;
        color: #f0f5ff !important; background: linear-gradient(135deg,rgba(13,24,46,.92),rgba(10,18,35,.94)) !important;
        border: 1px solid rgba(132,156,197,.15) !important;
        box-shadow: inset 0 1px 0 rgba(255,255,255,.025), 0 7px 18px rgba(0,0,0,.10) !important;
        transition: transform .22s cubic-bezier(.22,.8,.26,1), border-color .22s ease,
                    box-shadow .22s ease, background .22s ease, padding .22s ease !important;
      }
      section[data-testid="stSidebar"]:hover .stButton > button,
      section[data-testid="stSidebar"]:focus-within .stButton > button {
        padding: 0 15px !important;
      }
      section[data-testid="stSidebar"] .stButton > button::before {
        content: ""; position: absolute; inset: 0; pointer-events: none;
        background: linear-gradient(115deg,transparent 0%,rgba(255,255,255,.04) 45%,transparent 60%);
        transform: translateX(-120%); transition: transform .55s ease;
      }
      section[data-testid="stSidebar"] .stButton > button:hover::before { transform: translateX(120%); }
      section[data-testid="stSidebar"] .stButton > button p {
        font-family: "Arial Rounded MT Bold", "Trebuchet MS", Inter, system-ui, -apple-system, sans-serif !important;
        font-size: .94rem !important; line-height: 1 !important; margin: 0 !important;
        font-weight: 900 !important; letter-spacing: -.032em !important; color: #f0f5ff !important;
        text-rendering: geometricPrecision !important;
        white-space: nowrap !important; overflow: hidden !important; text-align: left !important;
      }
      section[data-testid="stSidebar"] .stButton > button[kind="primary"] {
        background: linear-gradient(135deg,rgba(35,194,255,.16),rgba(108,72,255,.22) 58%,rgba(227,78,211,.12)) !important;
        border-color: rgba(170,103,255,.72) !important;
        box-shadow: 0 0 18px rgba(103,76,255,.14), inset 3px 0 0 #c44dff, inset 0 1px 0 rgba(255,255,255,.06) !important;
        animation: jobsyncNavActive 2.8s ease-in-out infinite !important;
      }
      section[data-testid="stSidebar"] .stButton > button:hover {
        transform: translateX(3px) scale(1.008) !important; border-color: rgba(67,213,255,.72) !important;
        box-shadow: 0 0 20px rgba(55,190,255,.14), inset 0 1px 0 rgba(255,255,255,.05) !important;
      }
      section[data-testid="stSidebar"] .stButton > button:active { transform: translateX(1px) scale(.995) !important; }
      @keyframes jobsyncNavActive {
        0%,100% { box-shadow: 0 0 14px rgba(103,76,255,.12), inset 3px 0 0 #c44dff, inset 0 1px 0 rgba(255,255,255,.04); }
        50% { box-shadow: 0 0 24px rgba(64,205,255,.15), inset 3px 0 0 #43d5ff, inset 0 1px 0 rgba(255,255,255,.06); }
      }
      @media (prefers-reduced-motion: reduce) {
        section[data-testid="stSidebar"] .stButton > button,
        section[data-testid="stSidebar"] .stButton > button::before { animation: none !important; transition: none !important; }
      }

      /* User card */
      section[data-testid="stSidebar"] .sidebar-userbar {
        display: flex !important; align-items: center !important;
        justify-content: center !important; gap: 0 !important;
        padding: .55rem !important; margin: .65rem 0 !important; min-height: 46px !important;
        transition: justify-content 0s, gap .22s ease, padding .22s ease, margin .22s ease !important;
      }
      section[data-testid="stSidebar"]:hover .sidebar-userbar,
      section[data-testid="stSidebar"]:focus-within .sidebar-userbar {
        justify-content: flex-start !important; gap: .6rem !important;
        padding: .7rem !important; margin: 1rem 0 .25rem !important;
      }
      section[data-testid="stSidebar"] .sidebar-userbar .jobsync-user-avatar { margin: 0 !important; }
      section[data-testid="stSidebar"] .sidebar-usercopy,
      section[data-testid="stSidebar"] .sidebar-userbar .sidebar-role { display: none !important; }
      section[data-testid="stSidebar"]:hover .sidebar-usercopy,
      section[data-testid="stSidebar"]:focus-within .sidebar-usercopy,
      section[data-testid="stSidebar"]:hover .sidebar-userbar .sidebar-role,
      section[data-testid="stSidebar"]:focus-within .sidebar-userbar .sidebar-role { display: block !important; }
      section[data-testid="stSidebar"] .sidebar-useremail { display: none !important; }
      section[data-testid="stSidebar"]:hover .sidebar-useremail,
      section[data-testid="stSidebar"]:focus-within .sidebar-useremail { display: block !important; }

      @media (max-width: 560px) {
        :root { --jsync-nav-collapsed: 62px; }
        section[data-testid="stSidebar"] .stButton > button { min-height: 46px !important; height: 46px !important; }
      }

      /* Flyout restyle: a flatter, darker row list closer to a browser's
         hover-out tab/menu panel — solid near-black rows, a simple left
         highlight bar on the active item, no colored gradients or glow. */
      section[data-testid="stSidebar"][data-testid="stSidebar"] {
        background: #17181c !important;
        border-right: 1px solid rgba(255,255,255,.06) !important;
      }
      section[data-testid="stSidebar"]:hover,
      section[data-testid="stSidebar"]:focus-within {
        box-shadow: 18px 0 46px rgba(0,0,0,.45) !important;
      }
      section[data-testid="stSidebar"] .stButton > button {
        min-height: 40px !important; height: 40px !important;
        border-radius: 10px !important;
        background: transparent !important;
        border: 1px solid transparent !important;
        box-shadow: none !important;
        font-size: .82rem !important; font-weight: 600 !important; letter-spacing: 0 !important;
        color: #d7d9dc !important;
      }
      section[data-testid="stSidebar"] .stButton > button p {
        font-size: .82rem !important; font-weight: 600 !important; letter-spacing: 0 !important;
        color: #d7d9dc !important;
      }
      section[data-testid="stSidebar"] .stButton > button::before { display: none !important; }
      section[data-testid="stSidebar"] .stButton > button:hover {
        transform: none !important;
        background: rgba(255,255,255,.07) !important;
        border-color: transparent !important;
        box-shadow: none !important;
      }
      section[data-testid="stSidebar"] .stButton > button:active { transform: none !important; }
      section[data-testid="stSidebar"] .stButton > button[kind="primary"] {
        background: rgba(255,255,255,.1) !important;
        border-color: transparent !important;
        box-shadow: inset 3px 0 0 #8ab4f8 !important;
        animation: none !important;
      }
      section[data-testid="stSidebar"] .stButton > button[kind="primary"] p { color: #fff !important; }
      section[data-testid="stSidebar"] .nav-category-label {
        font-size: .62rem !important; font-weight: 600 !important; letter-spacing: .02em !important;
        text-transform: none !important; color: #8a8d93 !important;
      }
    </style>
    """, unsafe_allow_html=True)

    is_authed = bool(st.session_state.get("_authed"))
    profile_done = bool(st.session_state.get("_profile_completed"))

    if not is_authed:
        main_items = [("Home", "⌂", "Home")]
        tool_items = [("Login", "🔐", "Login")]
    else:
        # v1.7.0: the full workspace is available immediately after account
        # creation/sign-in. Profile completion is optional and can be finished
        # from Profile without blocking navigation.
        navigation_groups = [
            ("WORKSPACE", [
                ("Home", "⌂", "Home"),
                ("Dashboard", "▦", "Dashboard"),
            ]),
            ("JOB SEARCH", [
                ("CV & Cover Letter", "CV", "CV & Cover Letter"),
                ("New Search", "🔍", "New Search"),
                ("Applied Jobs", "✓", "Applied Jobs"),
                ("Folders", "📁", "Folders"),
            ]),
            ("SETTINGS", [
                ("Updates", "↗", "Updates"),
                ("Profile", "👤", "Profile"),
                ("Settings", "⚙", "Settings"),
            ]),
        ]

    # Render the signed-in navigation as spaced category groups. Login/onboarding
    # states keep their existing compact navigation.
    if is_authed:
        for group_index, (group_label, group_items) in enumerate(navigation_groups):
            if group_index:
                st.markdown('<div class="nav-category-gap"></div>', unsafe_allow_html=True)
            st.markdown(f'<div class="nav-category-label">{html.escape(group_label)}</div>', unsafe_allow_html=True)
            for p, icon, label in group_items:
                active = st.session_state.nav == p
                if st.button(f"{icon}   {label}", key=f"nav_{p}", width="stretch", type="primary" if active else "secondary"):
                    go(p)
    else:
        for p, icon, label in main_items:
            active = st.session_state.nav == p
            if st.button(f"{icon}   {label}", key=f"nav_{p}", width="stretch", type="primary" if active else "secondary"):
                go(p)
        if tool_items:
            st.markdown('<div class="nav-divider-space"></div>', unsafe_allow_html=True)
            for p, icon, label in tool_items:
                active = st.session_state.nav == p
                if st.button(f"{icon}   {label}", key=f"nav_{p}", width="stretch", type="primary" if active else "secondary"):
                    go(p)

    custom = custom_sections() if (is_authed and profile_done) else []
    if custom:
        st.markdown('<div class="nav-divider-space"></div>', unsafe_allow_html=True)
        for item in custom:
            p = item["name"]
            active = st.session_state.nav == p
            icon = item.get("icon") or "•"
            if st.button(f"{icon}   {p}", key=f"nav_custom_{safe_name(p,'section')}", width="stretch", type="primary" if active else "secondary"):
                go(p)

    st.markdown(
        f'<div class="sidebar-userbar"><div class="jobsync-user-avatar" style="width:34px;height:34px;flex-basis:34px;flex-shrink:0" title="{html.escape(profile.get("name") or "User", quote=True)}"></div><div class="sidebar-usercopy"><div class="sidebar-username">{html.escape(profile.get("name") or "User")}</div><div class="sidebar-useremail">{html.escape(profile.get("email") or "")}</div></div></div>',
        unsafe_allow_html=True,
    )
    if is_authed:
        if st.button("↪   Sign out", key="sidebar_sign_out", help="Sign out", width="stretch", type="secondary"):
            _clear_remembered_login()
            st.session_state["local_user_id"] = None
            st.session_state["local_user_email"] = None
            st.session_state["_auth_started_at"] = None
            st.session_state["_remembered_login"] = False
            st.session_state["_authed"] = False
            st.session_state["_user_id"] = None
            st.session_state["_user_email"] = ""
            st.session_state["_profile_completed"] = False
            st.session_state.nav = "Home"
            set_active_user(None)
            st.rerun()


page = st.session_state.nav
# v1.6.0 navigation migration: former standalone update pages now live under Updates.
if page in {"Gmail Updates", "LinkedIn Updates"}:
    page = "Updates"
    st.session_state.nav = "Updates"


def master_reset() -> None:
    """Reset all JobSync user data while preserving job-source and Google OAuth application settings."""
    # Clear user-uploaded and generated documents.
    folders_to_clear = [
        UPLOAD_CV,
        UPLOAD_CL,
        UPLOAD_REFERENCES,
        OUTPUT_CV,
        OUTPUT_CL,
        USER_BLUEPRINT_DIR,
    ]
    for folder in folders_to_clear:
        folder.mkdir(parents=True, exist_ok=True)
        for child in list(folder.iterdir()):
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            except Exception:
                pass

    # Remove the Excel application tracker.
    try:
        if TRACKER.exists():
            TRACKER.unlink()
    except Exception:
        pass

    # Remove saved application/search/profile state, local account and Gmail OAuth session.
    state_file = BASE_DIR / "data" / "state.json"
    account_file = BASE_DIR / "data" / "account.json"
    gmail_token_file = BASE_DIR / "data" / "gmail_token.json"
    remembered_login_file = BASE_DIR / "data" / "remembered_login.json"
    for file_path in (state_file, account_file, gmail_token_file, remembered_login_file):
        try:
            if file_path.exists():
                file_path.unlink()
        except Exception:
            pass

    # IMPORTANT: preserve .env and all API/provider/job-source settings.
    # Recreate only a clean application state. Settings are preserved by
    # taking the current state settings object before rebuilding.
    saved_settings = json.loads(json.dumps(state.get("settings", {})))
    fresh_state = json.loads(json.dumps(DEFAULT_STATE))
    fresh_state["settings"] = saved_settings
    save_state(fresh_state)

    # Return to Home after reset.
    st.session_state.nav = "Home"
    st.session_state.selected_job_index = 0
    st.session_state.pop("master_reset_text", None)


def confirm_master_reset() -> None:
    """Display a destructive-action confirmation dialog."""
    @st.dialog("Reset JobSync")
    def _reset_dialog():
        notify_error(
            "Everything listed below will be permanently deleted:"
        )
        st.markdown(
            "- local account and profile\n"
            "- saved job results and search history\n"
            "- all application records and Excel tracker\n"
            "- uploaded CVs and cover letters\n"
            "- generated CVs and cover letters\n"
            "- saved user base CV and cover-letter templates"
        )
        st.warning("This action cannot be undone.")
        st.info("Job-source settings and Google OAuth application settings will be kept.")

        confirmation = st.text_input(
            'Type "RESET" to confirm',
            key="master_reset_text",
            placeholder="RESET",
        )
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Cancel", width="stretch"):
                st.rerun()
        with c2:
            if st.button("Delete everything", type="primary", width="stretch"):
                if confirmation.strip().upper() != "RESET":
                    notify_error('Type "RESET" exactly to confirm.')
                else:
                    master_reset()
                    st.rerun()

    _reset_dialog()

# ──────────────── AUTH GUARDS ─────────────────
if not st.session_state.get("_authed"):
    if page not in {"Home", "Login"}:
        go("Home")
        st.stop()
else:
    # v1.7.0: account creation is no longer blocked by profile completion.
    # Users can enter the full workspace immediately and complete their profile
    # later from Profile.
    pass

# ---------------- LOGIN ----------------
if page == "Login":
    if page == "Login" and st.session_state.pop("_auth_expired", False):
        notify_error("Your 3-hour session has expired. Please sign in again.")
    # Modern, focused authentication screen. The underlying local account
    # behavior and forms remain unchanged.
    st.markdown(
        """
        <div class="jobsync-login-shell">
          <div class="jobsync-login-brand">
            <div class="jobsync-login-logo jobsync-logo-mark">
              <svg viewBox="0 0 48 48" aria-hidden="true">
              <defs><linearGradient id="jobsyncJGradient" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#7deaff"/><stop offset=".55" stop-color="#35a9ff"/><stop offset="1" stop-color="#7c5cff"/></linearGradient><linearGradient id="jobsyncOrbitGradient" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#35d8ff"/><stop offset=".55" stop-color="#7c5cff"/><stop offset="1" stop-color="#ec4fd1"/></linearGradient></defs>
              <path class="jobsync-logo-j" d="M17 8h8v20.5c0 5.9-3.7 9.5-9.2 9.5-4.4 0-7.5-2.2-8.8-5.8l6.1-3.2c.7 1.6 1.6 2.3 2.9 2.3 1.9 0 3-1.1 3-3.2V8z"/><ellipse class="jobsync-logo-orbit" cx="24" cy="24" rx="18" ry="10" transform="rotate(-19 24 24)"/><circle class="jobsync-logo-dot" cx="37" cy="15" r="2.2"/><path class="jobsync-logo-case" d="M29 22h12v9H29z M32 22v-2.3c0-.9.7-1.7 1.7-1.7h2.6c.9 0 1.7.8 1.7 1.7V22"/>
              </svg></div>
            <div>
              <div class="jobsync-login-name jobsync-logo-wordmark">Job<span class="sync">Sync</span></div>
              <div class="jobsync-login-tagline">Your local job-search workspace</div>
            </div>
          </div>
          <div class="jobsync-login-heading">Welcome back</div>
          <div class="jobsync-login-copy">Sign in to continue to your workspace.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    login_left, login_card, login_right = st.columns([1, 2.2, 1], gap="large")
    with login_card:
        tab_login, tab_signup, tab_forgot = st.tabs(["Sign in", "Create account", "Forgot password"])

        with tab_login:
            with st.form("login_form"):
                email = st.text_input("Email", autocomplete="email")
                password = st.text_input("Password", type="password", autocomplete="current-password")
                remember_me = st.checkbox("Remember me on this device", value=False, help="Keeps you signed in beyond the normal 3-hour session. JobSync does not store your password; it uses a revocable local sign-in token.")
                submit = st.form_submit_button("Sign in to JobSync", type="primary", width="stretch")
            if submit:
                try:
                    uid, account_email = _local_sign_in(email, password)
                    if remember_me:
                        _remember_local_login(uid, account_email)
                    else:
                        _clear_remembered_login()
                    st.session_state["local_user_id"] = uid
                    st.session_state["local_user_email"] = account_email
                    set_active_user(uid)
                    state = load_state(uid)
                    profile = state["profile"]
                    profile["email"] = profile.get("email") or account_email
                    save_state(state, uid)
                    st.session_state["_authed"] = True
                    st.session_state["_user_id"] = uid
                    st.session_state["_user_email"] = account_email
                    st.session_state["_profile_completed"] = bool(
                        state.get("settings", {}).get("profile_completed", False)
                    )
                    st.session_state["_auth_started_at"] = time.time()
                    st.session_state["_remembered_login"] = bool(remember_me)
                    st.session_state["_remembered_login_message"] = (
                        "Remember me is enabled for this device." if remember_me else "Remember me is off for this sign-in."
                    )
                    st.session_state.pop("_auth_expired", None)
                    st.session_state.nav = "Home"
                    st.rerun()
                except Exception as exc:
                    notify_error(f"Login failed: {exc}")

        with tab_signup:
            with st.form("signup_form"):
                email2 = st.text_input("Email", autocomplete="email")
                password2 = st.text_input("Password", type="password", autocomplete="new-password")
                submit2 = st.form_submit_button("Create my account", type="primary", width="stretch")
            if submit2:
                try:
                    uid, recovery_code = _local_sign_up(email2, password2)
                    account_email2 = email2.strip().lower()
                    st.session_state["local_user_id"] = uid
                    st.session_state["local_user_email"] = account_email2
                    set_active_user(uid)
                    state = load_state(uid)
                    state["profile"]["email"] = account_email2
                    state.setdefault("settings", {})["profile_completed"] = False
                    save_state(state, uid)
                    st.session_state["_authed"] = True
                    st.session_state["_user_id"] = uid
                    st.session_state["_user_email"] = account_email2
                    # v1.7.0: account creation opens the full workspace immediately.
                    # Profile can be completed later and never blocks navigation.
                    st.session_state["_profile_completed"] = True
                    state.setdefault("settings", {})["profile_completed"] = True
                    save_state(state, uid)
                    st.session_state["_auth_started_at"] = time.time()
                    st.session_state["_remembered_login"] = False
                    st.session_state["_show_recovery_code"] = recovery_code
                    st.session_state.nav = "Dashboard"
                    st.rerun()
                except Exception as exc:
                    notify_error(f"Account creation failed: {exc}")

        with tab_forgot:
            st.markdown("### Reset your password")
            st.caption("JobSync accounts are local. Use the recovery code you received when the account was created.")
            with st.form("forgot_password_form"):
                forgot_email = st.text_input("Email", autocomplete="email", key="forgot_email")
                recovery_code = st.text_input("Recovery code", placeholder="XXXX-XXXX-XXXX", key="forgot_recovery")
                new_password = st.text_input("New password", type="password", autocomplete="new-password", key="forgot_new_password")
                confirm_password = st.text_input("Confirm new password", type="password", autocomplete="new-password", key="forgot_confirm_password")
                reset_submit = st.form_submit_button("Reset password", type="primary", width="stretch")
            if reset_submit:
                if new_password != confirm_password:
                    notify_error("The new passwords do not match.")
                else:
                    try:
                        _local_reset_password(forgot_email, recovery_code, new_password)
                        notify_success("Password reset successfully. You can now sign in with the new password.")
                    except Exception as exc:
                        notify_error(f"Password reset failed: {exc}")

        st.markdown(
            '<div class="jobsync-login-footnote">Your account and workspace data stay on this computer.</div>',
            unsafe_allow_html=True,
        )

    if st.button("Back to Home", width="stretch", type="secondary", key="login_back_home"):
        go("Home")
        st.rerun()
    st.stop()


def _render_home_authenticated_content():
    '''Apple-glass Home: what JobSync is for, plus who else is online right now.'''
    display_name = (profile.get("name") or "").strip() or (profile.get("email") or st.session_state.get("_user_email") or "User").split("@",1)[0].strip() or "User"
    display_name = display_name[:80]
    h = datetime.now().hour
    greeting = "Good morning" if 5 <= h < 12 else "Good afternoon" if 12 <= h < 18 else "Good evening" if 18 <= h < 23 else "Good night"
    jobs = state.get("jobs", []) or []
    applied = state.get("applied", []) or []
    target_raw = profile.get("target_titles") or profile.get("job_titles") or profile.get("target_job_titles") or ""
    if isinstance(target_raw, list): targets = [str(x).strip() for x in target_raw if str(x).strip()]
    else: targets = [x.strip() for x in re.split(r"[,;|]", str(target_raw)) if x.strip()]
    location = str(profile.get("location") or profile.get("city") or "").strip()
    search_hint = html.escape(", ".join(targets[:3]) if targets else "Set your target roles")
    location_hint = html.escape(location or "Set your preferred location")

    _presence_heartbeat(); _refresh_online_cache()
    raw_online_users = st.session_state.get("_online_users") or []
    online_users = []
    seen_presence = set()
    for user in raw_online_users:
        uid = str(user.get("presence_id") or "").strip()
        if uid and uid in seen_presence:
            continue
        if uid: seen_presence.add(uid)
        raw_nm = str(user.get("display_name") or "").strip()
        if not raw_nm or raw_nm.lower() in {"user", "unknown", "none"}:
            seed = str(user.get("avatar_seed") or "").strip()
            raw_nm = seed.split("@", 1)[0].strip() if seed else "User"
        online_users.append({"display_name": raw_nm or "User", "presence_id": uid})

    # Overlapping avatar stack (up to 6, Apple-presence style) + a "+N" bubble
    # for the rest, plus the full scrollable name list below it.
    avatar_stack = "".join(
        f'<div class="ag-avatar" title="{html.escape(str(u["display_name"]))}">{html.escape(_presence_initials(str(u["display_name"])))}</div>'
        for u in online_users[:6]
    )
    if len(online_users) > 6:
        avatar_stack += f'<div class="ag-avatar-more">+{len(online_users) - 6}</div>'
    presence_rows = "".join(
        f'<div class="ag-presence-row"><div class="ag-presence-avatar">{html.escape(_presence_initials(str(u["display_name"])))}</div>'
        f'<div class="ag-presence-name">{html.escape(str(u["display_name"]))[:60]}</div></div>'
        for u in online_users
    )
    presence_body = avatar_stack or ""
    presence_error = str(st.session_state.get("_presence_error") or "").strip()
    if presence_error:
        presence_list_html = f'<div class="ag-presence-empty">Presence is unavailable right now.<br><span style="opacity:.6;font-size:.85em">{html.escape(presence_error)}</span></div>'
    else:
        presence_list_html = presence_rows or '<div class="ag-presence-empty">No one else online right now — you have JobSync to yourself.</div>'

    features = [
        ("⌕", "Discover", "Search real listings across your configured job sources."),
        ("▣", "Create", "Generate an AI-tailored CV and cover letter for each role."),
        ("✓", "Track", "Keep every application organized in one private workspace."),
    ]
    feature_html = "".join(
        f'<div class="ag-feature"><div class="ag-feature-icon">{icon}</div><div class="ag-feature-name">{name}</div><div class="ag-feature-sub">{html.escape(sub)}</div></div>'
        for icon, name, sub in features
    )

    st.markdown('''<style>
      /* One-viewport Home: a fixed-height flex column, its children stacked
         and CENTERED as a group (not pinned to the top), so short content
         reads as one composed screen instead of a cluster in the corner
         above a dead black gap. The page itself never scrolls — only the
         two content boxes (About, Online now) get their own internal
         scrollbar if their content runs long.

         Every block is a real st.container(key=...) wrapper, not a raw
         <div> spanning multiple st.markdown calls — Streamlit renders each
         markdown call into its own isolated wrapper element, so an unclosed
         tag in one call does not nest around widgets from a later call, it
         only produces broken HTML. Nesting st.columns's own horizontal
         block inside one half of an outer st.columns row (tried in an
         earlier pass, for a brand+shortcuts single row) also proved fragile
         — the inner block's flex sizing fought the outer column's width and
         the shortcut icons rendered shifted out over the greeting text.
         Stacked, full-width blocks avoid that column-in-column case. */
      /* Margin-based positioning instead of viewport-height flex centering:
         predictable regardless of exact browser chrome/viewport quirks. The
         hero sits with generous top margin so it reads as vertically
         centered in the upper-middle of the screen; the content grid
         follows immediately below it, filling the rest of the page. */
      .st-key-home_shell {
        display: flex !important; flex-direction: column !important;
        align-items: stretch !important;
      }
      .jobsync-launch-hero{text-align:center; margin: 9vh 0 5vh; animation: jobsync-home-fade .6s cubic-bezier(.22,1,.36,1) both;}
      .jobsync-launch-logo{width:56px;height:56px;margin:0 auto 12px;display:grid;place-items:center;border-radius:16px;background:radial-gradient(circle at 32% 25%,rgba(65,223,255,.24),rgba(86,64,255,.16) 38%,rgba(21,18,50,.9) 72%);border:1px solid rgba(111,215,255,.24);box-shadow:0 0 20px rgba(54,190,255,.14);animation:bigLogoFloat 4.2s ease-in-out infinite;}
      .jobsync-launch-logo svg{width:32px;height:32px;}
      .jobsync-launch-greeting{
        font-size:clamp(1.3rem,2.3vw,1.75rem);font-weight:700;letter-spacing:-.02em;
        background:linear-gradient(90deg,#eef2f7,#9fd8ff,#c9a9ff,#eef2f7);
        background-size:300% 100%; -webkit-background-clip:text; background-clip:text; color:transparent;
        animation: jobsyncGreetingShimmer 6s ease-in-out infinite;
      }
      @keyframes jobsyncGreetingShimmer{0%{background-position:0% 50%}50%{background-position:100% 50%}100%{background-position:0% 50%}}
      .jobsync-launch-sub{margin-top:6px;color:rgba(226,233,247,.6);font-size:.82rem;}
      .st-key-home_content{animation: jobsync-home-fade .6s cubic-bezier(.22,1,.36,1) .1s both;}
      .st-key-home_content .ag-about,.st-key-home_content .ag-presence{min-height:280px;}
      .st-key-home_content .ag-presence-list{max-height:24vh;overflow-y:auto;}
      @media(prefers-reduced-motion:reduce){.jobsync-launch-greeting{animation:none !important;}}
    </style>''', unsafe_allow_html=True)

    with st.container(key="home_shell"):
        st.markdown(f'''<div class="jobsync-launch-hero">
          <div class="jobsync-launch-logo" aria-hidden="true">
            <svg viewBox="0 0 48 48"><defs><linearGradient id="jsyncLaunchJ" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#8ff1ff"/><stop offset=".48" stop-color="#3fb8ff"/><stop offset="1" stop-color="#8b62ff"/></linearGradient></defs>
            <path style="fill:url(#jsyncLaunchJ)" d="M17 8h8v20.5c0 5.9-3.7 9.5-9.2 9.5-4.4 0-7.5-2.2-8.8-5.8l6.1-3.2c.7 1.6 1.6 2.3 2.9 2.3 1.9 0 3-1.1 3-3.2V8z"/></svg>
          </div>
          <div class="jobsync-launch-greeting">{html.escape(greeting)}, {html.escape(display_name)}.</div>
          <div class="jobsync-launch-sub">{search_hint} · {location_hint}</div>
        </div>''', unsafe_allow_html=True)

        with st.container(key="home_content"):
            markup = f'''<div class="ag-grid">
              <section class="ag-glass ag-about">
                <div class="ag-about-kicker">WHAT THIS IS</div>
                <div class="ag-about-title">Your private job-search command center</div>
                <div class="ag-about-copy">JobSync keeps discovery, tailored documents and application tracking in one calm, local workspace — no scattered tabs, no copy-pasting between five different tools.</div>
                <div class="ag-feature-row">{feature_html}</div>
              </section>
              <aside class="ag-glass ag-presence">
                <div class="ag-presence-head"><span class="ag-live-dot"></span><span class="ag-presence-title">Online now</span><span class="ag-presence-count">{len(online_users)}</span></div>
                <div class="ag-presence-sub">People currently using JobSync</div>
                <div class="ag-avatar-stack">{presence_body}</div>
                <div class="ag-presence-list">{presence_list_html}</div>
              </aside>
            </div>'''
            st.markdown(markup, unsafe_allow_html=True)

def _render_home_authenticated():
    """Render the authenticated Home overview in a 10-second fragment.

    Keeping the Online now card inside this fragment makes the embedded card
    the single live presence surface; there is no separate fixed duplicate.
    """
    fragment = getattr(st, "fragment", None)
    if fragment is None:
        _render_home_authenticated_content()
        return

    @fragment(run_every="10s")
    def _home_fragment():
        _render_home_authenticated_content()

    _home_fragment()



# ---------------- HOME ----------------
# ---------------- MODERN WORKSPACE UI ----------------
def render_modern_page_header(page_name: str) -> None:
    """Shared fixed-widget header used across JobSync's secondary workspaces."""
    st.markdown("""<style>
      .ux-page-shell{margin:0 0 12px;animation:uxPageReveal .46s cubic-bezier(.2,.7,.2,1) both;}
      @keyframes uxPageReveal{from{opacity:0;transform:translateY(7px)}to{opacity:1;transform:translateY(0)}}
      .ux-page-hero{position:relative;min-height:96px;box-sizing:border-box;padding:16px 18px;border:1px solid rgba(105,120,255,.22);border-radius:20px;background:radial-gradient(circle at 86% 12%,rgba(236,79,209,.14),transparent 28%),radial-gradient(circle at 55% 0%,rgba(56,216,255,.12),transparent 32%),linear-gradient(120deg,rgba(8,20,38,.98),rgba(24,15,55,.96) 62%,rgba(43,13,50,.94));box-shadow:0 16px 40px rgba(0,0,0,.20),inset 0 1px 0 rgba(255,255,255,.055);overflow:hidden;}
      .ux-page-hero:after{content:"JOBSYNC";position:absolute;right:14px;bottom:-18px;font-size:4.8rem;line-height:1;font-weight:950;letter-spacing:-.08em;color:rgba(255,255,255,.025);pointer-events:none;}
      .ux-page-copy{max-width:78%;position:relative;z-index:1;}
      .ux-page-kicker{font-size:.56rem;font-weight:950;letter-spacing:.18em;color:#65dcff;text-transform:uppercase;}
      .ux-page-title{font-size:1.65rem;line-height:1.05;font-weight:950;letter-spacing:-.05em;color:#f5f7ff;margin-top:5px;background:linear-gradient(100deg,#f5f7ff 0%,#d9e8ff 32%,#a7e8ff 50%,#f0b8ff 72%,#f5f7ff 100%);background-size:220% auto;-webkit-background-clip:text;background-clip:text;color:transparent;animation:uxTitleFlow 5.5s ease-in-out infinite;}
      .ux-page-kicker{animation:uxKickerPulse 3.2s ease-in-out infinite;}
      @keyframes uxTitleFlow{0%,100%{background-position:0% 50%;transform:translateY(0)}50%{background-position:100% 50%;transform:translateY(-1px)}}
      @keyframes uxKickerPulse{0%,100%{opacity:.78;letter-spacing:.18em}50%{opacity:1;letter-spacing:.205em}}
      @media (prefers-reduced-motion:reduce){.ux-page-title,.ux-page-kicker,.ux-page-shell{animation:none!important;transform:none!important;opacity:1!important;}}
      .ux-page-subtitle{font-size:.68rem;color:#8d9bb1;line-height:1.35;margin-top:5px;max-width:720px;}
      .ux-live-pill{position:absolute;right:16px;top:15px;z-index:2;padding:5px 9px;border:1px solid rgba(82,238,170,.18);border-radius:999px;background:rgba(14,35,38,.45);font-size:.51rem;font-weight:900;letter-spacing:.08em;color:#75efb0;}
      .ux-live-pill span{display:inline-block;width:6px;height:6px;border-radius:50%;background:#51e99d;box-shadow:0 0 10px rgba(81,233,157,.8);margin-right:5px;animation:uxpulse 1.6s ease-in-out infinite;}
      @keyframes uxpulse{50%{opacity:.35;transform:scale(.72)}}
      .ux-stat-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-top:8px;}
      .ux-stat{height:62px;box-sizing:border-box;padding:9px 11px;border-radius:14px;border:1px solid rgba(255,255,255,.075);background:linear-gradient(145deg,rgba(18,30,49,.82),rgba(7,14,27,.78));box-shadow:inset 0 1px 0 rgba(255,255,255,.035);position:relative;overflow:hidden;transition:border-color .35s ease,background .35s ease,box-shadow .35s ease;}
      .ux-stat:before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:#ff3f55;box-shadow:0 0 12px rgba(255,63,85,.42);animation:uxSignalRed 1.25s ease-in-out infinite;}
      .ux-stat:after{content:"";position:absolute;left:-45%;right:auto;top:0;height:2px;width:45%;background:linear-gradient(90deg,transparent,#ff7180,transparent);animation:uxSignalSweep 1.8s linear infinite;}
      .ux-stat-label{font-size:.50rem;font-weight:900;letter-spacing:.11em;color:#7b8aa0;}
      .ux-stat-value{font-size:1.03rem;font-weight:950;color:#f1f5ff;line-height:1.08;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
      .ux-stat-note{font-size:.49rem;color:#68768a;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
      .ux-stat.signal-ready{border-color:rgba(69,239,157,.30);background:linear-gradient(145deg,rgba(13,48,48,.90),rgba(7,22,29,.84));box-shadow:inset 0 1px 0 rgba(255,255,255,.045),0 0 22px rgba(63,229,153,.07);}
      .ux-stat.signal-ready:before{background:#45ef9d;box-shadow:0 0 14px rgba(69,239,157,.68);animation:uxSignalGreen 1.35s ease-in-out infinite;}
      .ux-stat.signal-ready:after{background:linear-gradient(90deg,transparent,#67ffb0,transparent);animation-duration:1.35s;}
      .ux-stat.signal-ready .ux-stat-label{color:#71efb1;}
      .ux-signal-status{display:inline-flex;align-items:center;gap:5px;margin-left:6px;font-size:.45rem;font-weight:950;letter-spacing:.08em;vertical-align:middle;}
      .ux-signal-dot{width:6px;height:6px;border-radius:50%;display:inline-block;background:#ff3f55;box-shadow:0 0 9px rgba(255,63,85,.72);animation:uxDotPulse 1.1s ease-in-out infinite;}
      .signal-ready .ux-signal-dot{background:#45ef9d;box-shadow:0 0 10px rgba(69,239,157,.85);}
      @keyframes uxSignalRed{0%,100%{opacity:.65;transform:scaleY(.72)}50%{opacity:1;transform:scaleY(1)}}
      @keyframes uxSignalGreen{0%,100%{opacity:.72;transform:scaleY(.75)}50%{opacity:1;transform:scaleY(1)}}
      @keyframes uxSignalSweep{from{left:-45%}to{left:110%}}
      @keyframes uxDotPulse{0%,100%{transform:scale(.78);opacity:.65}50%{transform:scale(1.18);opacity:1}}
      @media (prefers-reduced-motion:reduce){.ux-stat:before,.ux-stat:after,.ux-signal-dot{animation:none!important;}.ux-stat{transition:none;}}
      @media(max-width:850px){.ux-stat-grid{grid-template-columns:repeat(2,1fr)}.ux-page-copy{max-width:100%}.ux-live-pill{display:none}}

      .jobsync-search-command-title,.an-title{animation:pageHeadingFloat 4.8s ease-in-out infinite;}
      @keyframes pageHeadingFloat{0%,100%{transform:translateY(0);filter:drop-shadow(0 0 0 rgba(72,214,255,0))}50%{transform:translateY(-2px);filter:drop-shadow(0 0 14px rgba(96,126,255,.16))}}
      @media (prefers-reduced-motion:reduce){.jobsync-search-command-title,.an-title{animation:none!important;}}
    </style>""", unsafe_allow_html=True)
    jobs = state.get("jobs", []) or []
    applied = state.get("applied", []) or []
    interviews = sum(1 for r in applied if r.get("status") == "Interview")
    offers = sum(1 for r in applied if r.get("status") == "Offer")
    rejected = sum(1 for r in applied if r.get("status") == "Rejected")
    cvs = generated_cvs()
    letters = generated_letters()
    configs = {
        "New Search": ("SEARCH CENTER", "Find your next opportunity", "Configure sources, profile and freshness without losing sight of your results.", [("MATCHES", len(jobs), "available"), ("TRACKED", len(applied), "applications"), ("PROFILE", "READY" if profile.get("field") else "SET UP", "matching signal"), ("ATS", "ON" if state.get("ats_urls") else "OFF", "board filters")]),
        "Applied Jobs": ("APPLICATION PIPELINE", "Move opportunities forward", "A focused command deck for every job you have decided to track.", [("TRACKED", len(applied), "applications"), ("INTERVIEW", interviews, "next stage"), ("OFFERS", offers, "wins"), ("REJECTED", rejected, "closed")]),
        "Gmail Updates": ("INBOX SIGNAL", "Stay ahead of replies", "Turn mailbox activity into a clean stream of job-search signals.", [("TRACKED", len(applied), "applications"), ("INTERVIEWS", interviews, "pipeline"), ("OFFERS", offers, "pipeline"), ("STATUS", "LIVE", "workspace")]),
        "LinkedIn Updates": ("NETWORK SIGNAL", "See what changed", "A compact space for LinkedIn notification and profile signals.", [("JOBS", len(jobs), "in workspace"), ("TRACKED", len(applied), "applications"), ("CVS", len(cvs), "ready"), ("STATUS", "LIVE", "workspace")]),
        "CV & Cover Letter": ("DOCUMENT STUDIO", "Create application documents", "Generate tailored LaTeX, inspect the source, and continue to Overleaf when ready.", [("CVS", len(cvs), "saved"), ("LETTERS", len(letters), "saved"), ("PDF", "READY", "download"), ("ENGINE", "ONLINE", "generation")]),
        "Folders": ("DOCUMENT LIBRARY", "Everything in one place", "Browse your generated documents with compact actions beside each file.", [("CVS", len(cvs), "documents"), ("LETTERS", len(letters), "documents"), ("PDF", "READY", "preview"), ("STORAGE", "LOCAL", "workspace")]),
        "Profile": ("PROFILE CONTROL", "Tune your job-search identity", "Keep the information JobSync uses to match opportunities accurate and current.", [("TARGET", profile.get("field") or "NOT SET", "role"), ("CITY", profile.get("city") or profile.get("location") or "NOT SET", "location"), ("LANG", profile.get("language") or "ANY", "preference"), ("SIGNAL", "READY" if profile.get("field") else "INCOMPLETE", "match quality")]),
        "Settings": ("CONTROL CENTER", "Configure JobSync", "Manage integrations, updates, notifications and workspace behavior from one place.", [("VERSION", APP_VERSION, "current"), ("DATA", "LOCAL", "workspace"), ("BROWSER", "READY", "automation"), ("UPDATE", "READY", "software")]),
    }
    kicker, title, copy, cards = configs.get(page_name, ("JOBSYNC", page_name, "Workspace controls and activity.", [("JOBS", len(jobs), "available"), ("APPLIED", len(applied), "tracked"), ("CVS", len(cvs), "ready"), ("STATUS", "LIVE", "workspace")]))

    # New Search gets a live configuration signal deck instead of generic KPI cards.
    # Every search signal starts red/pulsing until the user explicitly applies that
    # setting. Once applied, the same card transitions to an animated green/ready state.
    if page_name == "New Search":
        signal_specs = [
            ("sources", "SOURCES", "Job sources", "Configured"),
            ("profile", "PROFILE", "Role + location", "Configured"),
            ("date", "FRESHNESS", "Posting window", "Configured"),
            ("ats", "ATS", "Board filters", "Configured"),
        ]
        stat_parts = []
        for key, label, note, ready_note in signal_specs:
            ready = bool(st.session_state.get(f"search_signal_{key}", False))
            state_label = "READY" if ready else "SET"
            state_note = ready_note if ready else "Needs setup"
            stat_parts.append(
                f'<div class="ux-stat {"signal-ready" if ready else "signal-pending"}" aria-label="{html.escape(label)} {state_label}">'
                f'<div class="ux-stat-label">{html.escape(label)} <span class="ux-signal-status"><span class="ux-signal-dot"></span>{state_label}</span></div>'
                f'<div class="ux-stat-value">{html.escape(state_label)}</div>'
                f'<div class="ux-stat-note">{html.escape(note)} · {html.escape(state_note)}</div></div>'
            )
        stat_html = "".join(stat_parts)
    else:
        stat_html = "".join(f'<div class="ux-stat"><div class="ux-stat-label">{html.escape(str(label))}</div><div class="ux-stat-value">{html.escape(str(value))}</div><div class="ux-stat-note">{html.escape(str(note))}</div></div>' for label, value, note in cards)
    # Updates is intentionally a clean utility surface: no dashboard/KPI cards above it.
    # The other workspaces keep their existing stat deck.
    if page_name == "Updates":
        st.markdown(
            f'<div class="ux-page-shell"><div class="ux-page-hero"><div class="ux-page-copy"><div class="ux-page-kicker">{html.escape(kicker)}</div><div class="ux-page-title">{html.escape(title)}</div><div class="ux-page-subtitle">{html.escape(copy)}</div></div><div class="ux-live-pill"><span></span> WORKSPACE LIVE</div></div></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(f'<div class="ux-page-shell"><div class="ux-page-hero"><div class="ux-page-copy"><div class="ux-page-kicker">{html.escape(kicker)}</div><div class="ux-page-title">{html.escape(title)}</div><div class="ux-page-subtitle">{html.escape(copy)}</div></div><div class="ux-live-pill"><span></span> WORKSPACE LIVE</div></div><div class="ux-stat-grid">{stat_html}</div></div>', unsafe_allow_html=True)

if page == "Home":
    if st.session_state.get("_show_recovery_code"):
        code = st.session_state.pop("_show_recovery_code")
        st.success("Account created successfully. Save your recovery code somewhere safe.")
        st.code(code, language=None)
        st.caption("You will need this code in Forgot password if you ever lose your local password.")
    # Authenticated Home keeps the clean welcome treatment while retaining the
    # existing responsive workspace donut and Online Now overview.
    if not st.session_state.get("_authed"):
        st.markdown(
            """
            <div class="jobsync-public-landing">
              <div class="jobsync-public-card">
                <div class="jobsync-big-logo" aria-label="Animated JobSync logo">
                  <svg viewBox="0 0 48 48" aria-hidden="true">
                    <defs>
                      <linearGradient id="jobsyncBigJGradient" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#8ff1ff"/><stop offset=".48" stop-color="#3fb8ff"/><stop offset="1" stop-color="#8b62ff"/></linearGradient>
                      <linearGradient id="jobsyncBigOrbitGradient" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#35e1ff"/><stop offset=".48" stop-color="#765cff"/><stop offset="1" stop-color="#f05bd9"/></linearGradient>
                    </defs>
                    <path class="jobsync-logo-j" style="fill:url(#jobsyncBigJGradient)" d="M17 8h8v20.5c0 5.9-3.7 9.5-9.2 9.5-4.4 0-7.5-2.2-8.8-5.8l6.1-3.2c.7 1.6 1.6 2.3 2.9 2.3 1.9 0 3-1.1 3-3.2V8z"/>
                    <ellipse class="jobsync-logo-orbit" style="stroke:url(#jobsyncBigOrbitGradient)" cx="24" cy="24" rx="18" ry="10" transform="rotate(-19 24 24)"/>
                    <circle class="jobsync-logo-dot" cx="37" cy="15" r="2.2"/>
                    <path class="jobsync-logo-case" d="M29 22h12v9H29z M32 22v-2.3c0-.9.7-1.7 1.7-1.7h2.6c.9 0 1.7.8 1.7 1.7V22"/>
                  </svg>
                </div>
                <div class="jobsync-public-kicker">JOBSYNC · LOCAL WORKSPACE</div>
                <div class="jobsync-public-title">Your job search.<br>One intelligent workspace.</div>
                <div class="jobsync-public-copy">Discover opportunities, track applications, build tailored CVs and keep everything organized in one private desktop workspace.</div>
                <div class="jobsync-public-feature-row">
                  <div class="jobsync-public-feature"><b>✦ Discover</b><span>Find and organize matching opportunities.</span></div>
                  <div class="jobsync-public-feature"><b>✓ Track</b><span>Keep your application pipeline in one place.</span></div>
                  <div class="jobsync-public-feature"><b>▣ Create</b><span>Generate tailored documents for every role.</span></div>
                </div>
              </div>
            </div>
            """, unsafe_allow_html=True,
        )
        st.stop()

    _render_home_authenticated()

# ---------------- DASHBOARD ----------------
elif page == "Dashboard":
    # v1.3.64 — Analytics Dashboard. This is intentionally different from
    # Home: Home remains the calm workspace landing page, while Dashboard is
    # the data/decision surface.
    name = profile.get("name", "").strip()
    fallback_username = (profile.get("email") or "").split("@", 1)[0].strip() or "User"
    raw_display_name = name or fallback_username
    display_name = html.escape(raw_display_name[:60])
    jobs = state.get("jobs", []) or []
    applied = state.get("applied", []) or []
    cvs = generated_cvs()
    letters = generated_letters()
    interviews = sum(1 for r in applied if r.get("status") == "Interview")
    offers = sum(1 for r in applied if r.get("status") == "Offer")
    rejected = sum(1 for r in applied if r.get("status") == "Rejected")
    response_rate = (interviews / len(applied) * 100) if applied else 0
    counts = Counter((r.get("status") or "Applied") for r in applied)
    updates = latest_updates() or []
    profile_fields = [profile.get("field"), profile.get("city") or profile.get("location"), profile.get("industry"), profile.get("language")]
    profile_score = round(sum(1 for x in profile_fields if str(x or "").strip()) / 4 * 100)
    source_counts = Counter(str(j.get("source") or "Other") for j in jobs)
    top_source = source_counts.most_common(1)[0] if source_counts else ("—", 0)
    top_jobs = jobs[:5]

    st.markdown('''
    <style>
      .an-shell{width:100%;max-width:1240px;margin:0 auto;padding:2px 0 22px;color:#eef4ff}
      .an-hero{height:122px;box-sizing:border-box;position:relative;overflow:hidden;border:1px solid rgba(255,255,255,.10);border-radius:22px;padding:18px 22px;background:radial-gradient(circle at 90% 0%,rgba(229,67,204,.20),transparent 27%),radial-gradient(circle at 42% 100%,rgba(44,211,255,.14),transparent 35%),linear-gradient(125deg,#0a1429,#111d42 52%,#25133c);box-shadow:0 20px 55px rgba(0,0,0,.25),inset 0 1px 0 rgba(255,255,255,.055)}
      .an-hero:after{content:"COMMAND";position:absolute;right:-8px;bottom:-26px;font-size:5.8rem;font-weight:950;letter-spacing:-.1em;color:rgba(255,255,255,.022);pointer-events:none}
      .an-kicker{font-size:.55rem;font-weight:950;letter-spacing:.19em;color:#57ddff;text-transform:uppercase}.an-title{font-size:1.9rem;font-weight:950;letter-spacing:-.06em;line-height:1.02;margin-top:5px}.an-copy{font-size:.65rem;color:#8f9eb5;margin-top:5px;max-width:720px}.an-live{position:absolute;right:18px;top:18px;padding:6px 9px;border-radius:999px;border:1px solid rgba(71,231,164,.22);background:rgba(35,205,133,.07);color:#70eeae;font-size:.49rem;font-weight:900;letter-spacing:.08em;z-index:2}.an-live i{display:inline-block;width:6px;height:6px;border-radius:50%;background:#4be6a0;box-shadow:0 0 10px #4be6a0;margin-right:5px;animation:anpulse 1.5s infinite}@keyframes anpulse{50%{opacity:.3;transform:scale(.7)}}
      .an-kpi-row{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px;margin:10px 0}.an-kpi{height:72px;box-sizing:border-box;padding:10px 11px;border-radius:15px;border:1px solid rgba(255,255,255,.075);background:linear-gradient(145deg,rgba(18,29,53,.92),rgba(7,14,29,.95));position:relative;overflow:hidden}.an-kpi:before{content:"";position:absolute;left:0;top:0;right:0;height:2px;background:var(--c)}.an-kpi-label{font-size:.49rem;font-weight:900;letter-spacing:.11em;color:#71819a;text-transform:uppercase}.an-kpi-value{font-size:1.35rem;font-weight:950;line-height:1;margin-top:7px}.an-kpi-note{font-size:.48rem;color:#61718a;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      .an-grid{display:grid;grid-template-columns:1.15fr .85fr;gap:9px}.an-card{border:1px solid rgba(255,255,255,.075);border-radius:17px;background:linear-gradient(145deg,rgba(14,25,48,.94),rgba(7,13,27,.96));box-shadow:0 16px 40px rgba(0,0,0,.17),inset 0 1px 0 rgba(255,255,255,.035);overflow:hidden}.an-card-head{height:48px;padding:10px 13px 7px;box-sizing:border-box;border-bottom:1px solid rgba(255,255,255,.05);display:flex;align-items:flex-start;justify-content:space-between}.an-card-title{font-size:.73rem;font-weight:900}.an-card-sub{font-size:.48rem;color:#63728a;margin-top:3px}.an-tag{font-size:.46rem;color:#62dcff;border:1px solid rgba(70,219,255,.16);background:rgba(70,219,255,.05);border-radius:999px;padding:4px 7px;font-weight:900}
      .an-momentum{height:236px;padding:14px;box-sizing:border-box}.an-momentum-top{display:grid;grid-template-columns:150px 1fr;gap:16px;height:190px;align-items:center}.an-gauge{width:132px;height:132px;position:relative;display:grid;place-items:center;filter:drop-shadow(0 0 18px rgba(65,132,255,.13));}.an-gauge svg{width:132px;height:132px;display:block;overflow:visible}.an-gauge-track{fill:none;stroke:#27354d;stroke-width:12}.an-gauge-progress{fill:none;stroke:url(#anGaugeGradient);stroke-width:12;stroke-linecap:round;stroke-dasharray:327;stroke-dashoffset:327;transform:rotate(-90deg);transform-origin:50% 50%;animation:anGaugeDraw 1.65s cubic-bezier(.2,.85,.3,1) .12s forwards;filter:drop-shadow(0 0 9px rgba(57,217,255,.28));}.an-gauge-inner{position:absolute;inset:14px;border-radius:50%;display:flex;flex-direction:column;align-items:center;justify-content:center;background:#091224;border:1px solid rgba(255,255,255,.05);box-shadow:inset 0 0 26px rgba(67,92,255,.08)}.an-gauge-value{font-size:1.6rem;font-weight:950}.an-gauge-label{font-size:.45rem;color:#64748c;letter-spacing:.08em;text-transform:uppercase}.an-bars{display:flex;height:150px;align-items:flex-end;gap:12px;padding:0 8px 20px;border-bottom:1px solid rgba(255,255,255,.05)}.an-bar-col{height:100%;flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:5px}.an-bar{width:100%;max-width:24px;border-radius:8px 8px 3px 3px;background:linear-gradient(180deg,#39d9ff,#7958ff,#d84ed0);box-shadow:0 0 15px rgba(100,94,255,.16);min-height:7px;transform:scaleY(0);transform-origin:bottom;animation:anBarRise 1.15s cubic-bezier(.18,.88,.27,1.15) var(--delay,0s) forwards;}.an-bar-num{font-size:.45rem;color:#7f8da2;opacity:0;transform:translateY(6px);animation:anBarLabel .45s ease-out var(--label-delay,.65s) forwards}.an-bar-day{font-size:.46rem;color:#596a82}.an-bars .an-bar-col:nth-child(1){--delay:.08s;--label-delay:.72s}.an-bars .an-bar-col:nth-child(2){--delay:.18s;--label-delay:.82s}.an-bars .an-bar-col:nth-child(3){--delay:.28s;--label-delay:.92s}.an-bars .an-bar-col:nth-child(4){--delay:.38s;--label-delay:1.02s}.an-bars .an-bar-col:nth-child(5){--delay:.48s;--label-delay:1.12s}.an-bars .an-bar-col:nth-child(6){--delay:.58s;--label-delay:1.22s}@keyframes anBarRise{0%{transform:scaleY(0)}68%{transform:scaleY(1.08)}86%{transform:scaleY(.94)}100%{transform:scaleY(1)}}@keyframes anBarLabel{to{opacity:1;transform:translateY(0)}}@keyframes anGaugeDraw{to{stroke-dashoffset:var(--gauge-dash,327)}}
      .an-funnel{padding:12px 13px 13px;height:236px;box-sizing:border-box}.an-funnel-row{display:grid;grid-template-columns:67px 1fr 28px;gap:8px;align-items:center;margin:12px 0}.an-funnel-label{font-size:.53rem;color:#8a98ab;font-weight:800}.an-track{height:9px;border-radius:999px;background:rgba(255,255,255,.055);overflow:hidden}.an-fill{height:100%;border-radius:999px;background:linear-gradient(90deg,#39d9ff,#7a59ff,#df4fd0);transform:scaleX(0);transform-origin:left;animation:anFunnelGrow .95s cubic-bezier(.2,.84,.3,1.08) var(--delay,0s) forwards;box-shadow:0 0 10px rgba(91,133,255,.18)}.an-funnel-row:nth-child(1){--delay:.18s}.an-funnel-row:nth-child(2){--delay:.34s}.an-funnel-row:nth-child(3){--delay:.50s}.an-funnel-row:nth-child(4){--delay:.66s}.an-funnel-value{ text-align:right;font-size:.55rem;font-weight:900}@keyframes anFunnelGrow{0%{transform:scaleX(0);opacity:.35}68%{transform:scaleX(1.04)}88%{transform:scaleX(.97)}100%{transform:scaleX(1);opacity:1}}
      .an-lower{display:grid;grid-template-columns:1.12fr .88fr .78fr;gap:9px;margin-top:9px}.an-tall{height:252px}.an-jobs{padding:0 12px 10px}.an-job{display:grid;grid-template-columns:1.6fr .9fr 72px;gap:8px;align-items:center;padding:9px 0;border-bottom:1px solid rgba(255,255,255,.045)}.an-job:last-child{border-bottom:0}.an-job-title{font-size:.57rem;font-weight:850;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.an-job-meta{font-size:.47rem;color:#687890;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.an-chip{justify-self:end;font-size:.44rem;color:#6fe7a7;border:1px solid rgba(71,231,164,.14);background:rgba(71,231,164,.05);border-radius:999px;padding:4px 6px;max-width:66px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .an-insights{padding:11px 12px}.an-insight{padding:10px;border-radius:11px;background:rgba(255,255,255,.025);border:1px solid rgba(255,255,255,.05);margin-bottom:7px}.an-insight:last-child{margin-bottom:0}.an-insight-title{font-size:.56rem;font-weight:900}.an-insight-copy{font-size:.48rem;color:#687891;line-height:1.35;margin-top:3px}.an-meter{height:6px;background:rgba(255,255,255,.055);border-radius:999px;margin-top:7px;overflow:hidden}.an-meter span{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,#39d9ff,#7b5aff,#e44fd2)}
      .an-activity{padding:3px 12px}.an-event{display:flex;gap:8px;align-items:center;padding:9px 0;border-bottom:1px solid rgba(255,255,255,.045)}.an-event:last-child{border-bottom:0}.an-event-icon{width:25px;height:25px;display:grid;place-items:center;border-radius:8px;background:linear-gradient(135deg,#32cfff,#8456ff);font-size:.5rem;font-weight:950}.an-event-copy{min-width:0}.an-event-title{font-size:.53rem;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.an-event-meta{font-size:.45rem;color:#62718a;margin-top:2px}
      .an-quick{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin-top:9px}.an-quick .stButton>button{height:34px!important;min-height:34px!important;border-radius:10px!important;font-size:.56rem!important;font-weight:850!important}
      @media(max-width:1050px){.an-kpi-row{grid-template-columns:repeat(3,1fr)}.an-lower{grid-template-columns:1fr 1fr}.an-lower .an-card:last-child{grid-column:1/-1}.an-grid{grid-template-columns:1fr}}
      @media(max-width:700px){.an-kpi-row{grid-template-columns:repeat(2,1fr)}.an-lower{grid-template-columns:1fr}.an-lower .an-card:last-child{grid-column:auto}.an-momentum-top{grid-template-columns:1fr}.an-hero{height:auto;min-height:120px}.an-live{display:none}}
      /* v1.6.0 dashboard readability refinement: larger type and touch targets while remaining fluid. */
      .an-shell{max-width:1400px;padding-bottom:28px}
      .an-hero{height:auto;min-height:136px;padding:22px 26px}
      .an-kicker{font-size:clamp(.62rem,.62vw,.76rem)}
      .an-title{font-size:clamp(2.05rem,2.35vw,2.65rem)}
      .an-copy{font-size:clamp(.72rem,.72vw,.9rem);line-height:1.45}
      .an-live{font-size:clamp(.52rem,.5vw,.64rem);padding:7px 11px}
      .an-kpi-row{gap:10px;margin:12px 0}
      .an-kpi{height:88px;padding:13px 14px;border-radius:16px}
      .an-kpi-label{font-size:clamp(.58rem,.56vw,.7rem);letter-spacing:.1em}
      .an-kpi-value{font-size:clamp(1.55rem,1.7vw,2rem);margin-top:8px}
      .an-kpi-note{font-size:clamp(.56rem,.55vw,.68rem);margin-top:5px}
      .an-grid,.an-lower{gap:11px}
      .an-card{border-radius:18px}
      .an-card-head{height:62px;padding:13px 16px 9px}
      .an-card-title{font-size:clamp(.86rem,.85vw,1.05rem)}
      .an-card-sub{font-size:clamp(.56rem,.58vw,.7rem);margin-top:4px}
      .an-tag{font-size:clamp(.52rem,.52vw,.64rem);padding:5px 8px}
      .an-momentum{height:285px;padding:18px}
      .an-momentum-top{grid-template-columns:175px 1fr;gap:20px;height:230px}
      .an-gauge{width:158px;height:158px}
      .an-gauge svg{width:158px;height:158px}
      .an-gauge-inner{inset:16px}
      .an-gauge-value{font-size:clamp(1.8rem,2vw,2.25rem)}
      .an-gauge-label{font-size:clamp(.5rem,.52vw,.65rem)}
      .an-bars{height:180px;gap:14px;padding:0 8px 22px}
      .an-bar{max-width:30px;min-height:9px}
      .an-bar-num,.an-bar-day{font-size:clamp(.52rem,.52vw,.66rem)}
      .an-funnel{height:285px;padding:16px}
      .an-funnel-row{grid-template-columns:78px 1fr 36px;gap:10px;margin:15px 0}
      .an-funnel-label,.an-funnel-value{font-size:clamp(.62rem,.62vw,.76rem)}
      .an-track{height:11px}
      .an-lower .an-card{min-height:265px}
      .an-job{padding:12px 14px;gap:10px}
      .an-job-title{font-size:clamp(.7rem,.72vw,.88rem)}
      .an-job-meta{font-size:clamp(.56rem,.58vw,.7rem)}
      .an-chip{font-size:clamp(.5rem,.5vw,.62rem)}
      .an-insight{padding:13px 14px}
      .an-insight-title{font-size:clamp(.62rem,.62vw,.76rem)}
      .an-insight-copy{font-size:clamp(.54rem,.55vw,.68rem);line-height:1.4}
      .an-event-title{font-size:clamp(.62rem,.62vw,.76rem)}
      .an-event-meta{font-size:clamp(.52rem,.52vw,.64rem)}
      .an-quick .stButton>button{height:42px!important;min-height:42px!important;font-size:clamp(.62rem,.62vw,.76rem)!important}
      @media(max-width:1050px){.an-momentum-top{grid-template-columns:145px 1fr}.an-gauge{width:140px;height:140px}.an-gauge svg{width:140px;height:140px}.an-kpi{height:84px}}
      @media(max-width:700px){.an-shell{padding-bottom:20px}.an-hero{min-height:126px;padding:18px}.an-title{font-size:1.85rem}.an-kpi-row{gap:8px}.an-kpi{height:82px;padding:11px}.an-kpi-value{font-size:1.45rem}.an-momentum{height:auto;min-height:420px}.an-momentum-top{grid-template-columns:1fr;height:auto;justify-items:center}.an-bars{width:100%;margin-top:18px}.an-funnel{height:auto;min-height:250px}.an-lower .an-card{min-height:220px}}
      @media (prefers-reduced-motion:reduce){.an-gauge-progress,.an-bar,.an-bar-num,.an-fill{animation:none!important;transform:none!important;opacity:1!important}.an-gauge-progress{stroke-dashoffset:var(--gauge-dash,327)!important;}}
    </style>
    ''', unsafe_allow_html=True)

    progress = max(0, min(100, profile_score))
    # A simple visual activity profile from current workspace counts. It is not
    # presented as historical data; the labels explicitly describe it as load.
    activity_values = [len(jobs), len(applied), interviews, offers, len(cvs), len(letters)]
    activity_max = max(activity_values + [1])
    activity_days = ["Jobs", "Apps", "Int.", "Offers", "CV", "Letters"]
    bars = ''.join(f'<div class="an-bar-col"><div class="an-bar-num">{v}</div><div class="an-bar" style="height:{max(7, int((v/activity_max)*112))}px"></div><div class="an-bar-day">{d}</div></div>' for v,d in zip(activity_values,activity_days))
    funnel = [("Found", len(jobs)), ("Applied", len(applied)), ("Interview", interviews), ("Offer", offers)]
    fmax = max([v for _,v in funnel] + [1])
    funnel_html = ''.join(f'<div class="an-funnel-row"><span class="an-funnel-label">{label}</span><div class="an-track"><div class="an-fill" style="width:{max(5,int(v/fmax*100))}%"></div></div><span class="an-funnel-value">{v}</span></div>' for label,v in funnel)
    job_html = ''.join(f'<div class="an-job"><div><div class="an-job-title">{html.escape(str(j.get("title") or "Untitled"))}</div><div class="an-job-meta">{html.escape(str(j.get("company") or "Unknown"))} · {html.escape(str(j.get("location") or ""))}</div></div><div class="an-job-meta">{html.escape(str(j.get("posted_date") or "—"))}</div><span class="an-chip">{html.escape(str(j.get("source") or "Job"))}</span></div>' for j in top_jobs) or '<div class="dash-empty">No jobs yet. Run a search to populate the dashboard.</div>'
    events = updates[:4]
    activity_html = ''.join(f'<div class="an-event"><div class="an-event-icon">{html.escape(str(kind)[:1].upper())}</div><div class="an-event-copy"><div class="an-event-title">{html.escape(str(text))}</div><div class="an-event-meta">JobSync workspace activity</div></div></div>' for _,kind,text in events) or '<div class="dash-empty">No recent workspace events.</div>'
    profile_status = "Ready" if progress == 100 else "Needs attention" if progress < 75 else "Good"

    st.markdown(f'''
      <div class="an-shell">
        <div class="an-hero"><div class="an-kicker">JOBSYNC · ANALYTICS</div><div class="an-title">Decision dashboard</div><div class="an-copy">A different view of your workspace — momentum, pipeline health, search coverage and the actions that need attention.</div><div class="an-live"><i></i> DATA LIVE</div></div>
        <div class="an-kpi-row">
          <div class="an-kpi" style="--c:#ff5261"><div class="an-kpi-label">Fresh jobs</div><div class="an-kpi-value">{len(jobs)}</div><div class="an-kpi-note">Current workspace</div></div>
          <div class="an-kpi" style="--c:#3ce69b"><div class="an-kpi-label">Applications</div><div class="an-kpi-value">{len(applied)}</div><div class="an-kpi-note">Tracked</div></div>
          <div class="an-kpi" style="--c:#39d9ff"><div class="an-kpi-label">Interviews</div><div class="an-kpi-value">{interviews}</div><div class="an-kpi-note">Next stage</div></div>
          <div class="an-kpi" style="--c:#a77cff"><div class="an-kpi-label">Offers</div><div class="an-kpi-value">{offers}</div><div class="an-kpi-note">Outcomes</div></div>
          <div class="an-kpi" style="--c:#e44fd2"><div class="an-kpi-label">Documents</div><div class="an-kpi-value">{len(cvs)+len(letters)}</div><div class="an-kpi-note">CVs + letters</div></div>
          <div class="an-kpi" style="--c:#f5bb49"><div class="an-kpi-label">Response</div><div class="an-kpi-value">{response_rate:.0f}%</div><div class="an-kpi-note">Interview / applied</div></div>
        </div>
        <div class="an-grid">
          <div class="an-card"><div class="an-card-head"><div><div class="an-card-title">Workspace momentum</div><div class="an-card-sub">Current workload by JobSync area</div></div><span class="an-tag">LIVE</span></div><div class="an-momentum"><div class="an-momentum-top"><div class="an-gauge" style="--gauge-dash:{327 - round(327 * progress / 100, 2)}"><svg viewBox="0 0 132 132" aria-label="Profile readiness {progress}%"><defs><linearGradient id="anGaugeGradient" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#39d9ff"/><stop offset="55%" stop-color="#7958ff"/><stop offset="100%" stop-color="#d84ed0"/></linearGradient></defs><circle class="an-gauge-track" cx="66" cy="66" r="52"/><circle class="an-gauge-progress" cx="66" cy="66" r="52"/></svg><div class="an-gauge-inner"><div class="an-gauge-value">{progress}%</div><div class="an-gauge-label">profile ready</div></div></div><div class="an-bars">{bars}</div></div></div></div>
          <div class="an-card"><div class="an-card-head"><div><div class="an-card-title">Application funnel</div><div class="an-card-sub">Where opportunities sit right now</div></div><span class="an-tag">{len(applied)} TRACKED</span></div><div class="an-funnel">{funnel_html}</div></div>
        </div>
        <div class="an-lower">
          <div class="an-card an-tall"><div class="an-card-head"><div><div class="an-card-title">Priority opportunities</div><div class="an-card-sub">Latest jobs available for action</div></div><span class="an-tag">{len(jobs)} FOUND</span></div><div class="an-jobs">{job_html}</div></div>
          <div class="an-card an-tall"><div class="an-card-head"><div><div class="an-card-title">Smart workspace signals</div><div class="an-card-sub">Useful indicators from your data</div></div></div><div class="an-insights"><div class="an-insight"><div class="an-insight-title">Profile readiness · {profile_status}</div><div class="an-insight-copy">Complete your search identity to improve matching quality.</div><div class="an-meter"><span style="width:{progress}%"></span></div></div><div class="an-insight"><div class="an-insight-title">Top source · {html.escape(str(top_source[0]))}</div><div class="an-insight-copy">{top_source[1]} jobs currently come from this source.</div></div><div class="an-insight"><div class="an-insight-title">Documents · {len(cvs)} CV / {len(letters)} letters</div><div class="an-insight-copy">Your document workspace is ready for the next application.</div></div><div class="an-insight"><div class="an-insight-title">Pipeline attention · {rejected} rejected</div><div class="an-insight-copy">Keep moving active applications toward interview and offer stages.</div></div></div></div>
          <div class="an-card an-tall"><div class="an-card-head"><div><div class="an-card-title">Live activity</div><div class="an-card-sub">Recent workspace events</div></div><span class="an-tag">NOW</span></div><div class="an-activity">{activity_html}</div></div>
        </div>
      </div>
    ''', unsafe_allow_html=True)

    q1,q2,q3,q4 = st.columns(4, gap="small")
    for col,label,target,key in [(q1,"⌕ Find jobs","New Search","an_find"),(q2,"✓ Applications","Applied Jobs","an_apps"),(q3,"▣ Create CV","CV & Cover Letter","an_cv"),(q4,"◉ Profile","Profile","an_profile")]:
        with col:
            if st.button(label,key=key,width="stretch"):
                go(target)

# ---------------- NEW SEARCH ----------------
elif page == "New Search":
    render_modern_page_header("New Search")

    if "search_wheel" not in st.session_state:
        st.session_state["search_wheel"] = "profile"
    active_setting = st.session_state.get("search_wheel", "profile")

    configured_ids = state.get("settings", {}).get("actor_ids") or [ACTOR_CATALOG[name]["id"] for name in DEFAULT_ACTOR_NAMES]
    configured_names = [ACTOR_ID_TO_NAME.get(x, x) for x in configured_ids]
    saved_mode = state.get("settings", {}).get("job_search_mode", "free")
    if saved_mode not in JOB_SEARCH_MODE_LABELS:
        saved_mode = "free"
    saved_preset = state.get("settings", {}).get("job_source_preset", "open")
    if saved_preset not in {"open", "linkedin", "apify"}:
        saved_preset = "apify" if saved_mode == "apify" else "open"
    configured_free = state.get("settings", {}).get("free_sources") or FREE_SOURCE_NAMES
    preset_labels = ["Open source", "LinkedIn", "Apify"]
    preset_values = ["open", "linkedin", "apify"]

    # v1.6.0: the animated signal/radar panel is replaced by a live interactive job map.
    left, results_col = st.columns([0.98, 1.72], gap="small")
    with left:
        # Keep the search controls in the first viewport: the map is compact and
        # the settings/find controls sit immediately below it, without an
        # internal vertical scrollbar on the left side.
        render_live_job_map(state.get("jobs") or [], profile.get("location") or profile.get("city") or "")

        # v1.6.0 modal search settings: tabs stay compact and each tab opens
        # its settings in a centered Streamlit dialog.
        wheel_options = ["Sources", "Profile", "Freshness", "ATS"]
        selected_map = {"Sources": "sources", "Profile": "profile", "Freshness": "date", "ATS": "ats"}
        active_setting = st.session_state.get("search_wheel", "profile")
        current_label = {"sources":"Sources", "profile":"Profile", "date":"Freshness", "ats":"ATS"}.get(active_setting, "Profile")

        def _safe_choice(options, value, fallback=0):
            try:
                return options.index(value)
            except (ValueError, AttributeError):
                return fallback

        @st.dialog("Search settings", width="large")
        def _search_settings_dialog(setting):
            setting = setting if setting in {"sources", "profile", "date", "ats"} else "profile"
            st.caption("Configure this search signal. Press Enter or use Apply & close when finished.")

            if setting == "sources":
                st.markdown("### 🌐 Sources")
                st.caption("Choose where JobSync collects openings from.")
                saved = st.session_state.get("search_source_preset", saved_preset)
                if saved not in preset_values:
                    saved = saved_preset if saved_preset in preset_values else "open"
                with st.form("search_sources_modal", clear_on_submit=False):
                    preset_label = st.radio("Job scraping option", preset_labels, index=_safe_choice(preset_values, saved), horizontal=True, key="modal_source_preset")
                    chosen = preset_values[_safe_choice(preset_labels, preset_label)]
                    if chosen == "open":
                        st.multiselect("Open-source collectors", options=OPEN_SOURCE_DEFAULTS, default=[x for x in st.session_state.get("search_free_sources", configured_free) if x in OPEN_SOURCE_DEFAULTS] or OPEN_SOURCE_DEFAULTS, key="modal_free_sources")
                    elif chosen == "linkedin":
                        st.success("LinkedIn selected — direct LinkedIn search.")
                    else:
                        st.multiselect("Apify Actors", options=list(ACTOR_CATALOG.keys()), default=[x for x in st.session_state.get("search_apify_sources", configured_names) if x in ACTOR_CATALOG], key="modal_apify_sources")
                    apply = st.form_submit_button("✓  Apply & close", type="primary", width="stretch")
                if apply:
                    st.session_state["search_source_preset"] = chosen
                    if chosen == "open":
                        st.session_state["search_free_sources"] = st.session_state.get("modal_free_sources", [])
                        st.session_state["search_apify_sources"] = []
                    elif chosen == "linkedin":
                        st.session_state["search_free_sources"] = ["LinkedIn"]
                        st.session_state["search_apify_sources"] = []
                    else:
                        st.session_state["search_free_sources"] = []
                        st.session_state["search_apify_sources"] = st.session_state.get("modal_apify_sources", [])
                    st.session_state["search_signal_sources"] = True
                    st.session_state["search_wheel"] = "sources"
                    st.rerun()
                if st.button("Close", key="close_sources_modal", width="stretch"):
                    st.rerun()

            elif setting == "profile":
                st.markdown("### 🎯 Search profile")
                st.caption("Define the opportunity you want to find.")
                experience_options = ["Any", "Internship", "Entry level", "Associate", "Mid-Senior level", "Director"]
                language_options = ["English", "German", "French", "Spanish", "Italian", "Dutch", "Any"]
                with st.form("search_profile_modal", clear_on_submit=False):
                    st.text_input("Field / job title", value=st.session_state.get("search_field", profile.get("field", "")), placeholder="Mechanical Engineer", key="modal_search_field")
                    st.text_input("Industry", value=st.session_state.get("search_industry", profile.get("industry", "")), placeholder="Manufacturing, optics, automotive", key="modal_search_industry")
                    st.text_input("Location", value=st.session_state.get("search_location", profile.get("location", profile.get("city", ""))), placeholder="Hannover, Germany", key="modal_search_location")
                    current_exp = st.session_state.get("search_experience", profile.get("experience", "Any"))
                    if current_exp not in experience_options: current_exp = "Any"
                    st.selectbox("Experience", experience_options, index=_safe_choice(experience_options, current_exp), key="modal_search_experience")
                    current_language = st.session_state.get("search_language", profile.get("language", "Any"))
                    if current_language not in language_options: current_language = "Any"
                    st.selectbox("Required language", language_options, index=_safe_choice(language_options, current_language), key="modal_search_language")
                    apply = st.form_submit_button("✓  Apply & close", type="primary", width="stretch")
                if apply:
                    st.session_state["search_field"] = st.session_state.get("modal_search_field", "").strip()
                    st.session_state["search_industry"] = st.session_state.get("modal_search_industry", "").strip()
                    st.session_state["search_location"] = st.session_state.get("modal_search_location", "").strip()
                    st.session_state["search_experience"] = st.session_state.get("modal_search_experience", "Any")
                    st.session_state["search_language"] = st.session_state.get("modal_search_language", "Any")
                    st.session_state["search_signal_profile"] = True
                    st.session_state["search_wheel"] = "profile"
                    st.rerun()
                if st.button("Close", key="close_profile_modal", width="stretch"):
                    st.rerun()

            elif setting == "date":
                st.markdown("### 📅 Freshness")
                st.caption("Control how recent the jobs should be.")
                date_options = {"Past 24 hours": 1, "Past 3 days": 3, "Past 7 days": 7}
                current_window = st.session_state.get("search_date_window", profile.get("date_window", "Past 7 days"))
                if current_window not in date_options: current_window = "Past 7 days"
                with st.form("search_freshness_modal", clear_on_submit=False):
                    st.selectbox("Date posted", list(date_options.keys()), index=_safe_choice(list(date_options.keys()), current_window), key="modal_search_date_window")
                    apply = st.form_submit_button("✓  Apply & close", type="primary", width="stretch")
                if apply:
                    st.session_state["search_date_window"] = st.session_state.get("modal_search_date_window", "Past 7 days")
                    st.session_state["search_signal_date"] = True
                    st.session_state["search_wheel"] = "date"
                    st.rerun()
                if st.button("Close", key="close_date_modal", width="stretch"):
                    st.rerun()

            else:
                st.markdown("### 🔗 Company ATS")
                st.caption("Add public career boards to search alongside selected sources.")
                current_ats = "\n".join(state.get("settings", {}).get("ats_urls") or [])
                with st.form("search_ats_modal", clear_on_submit=False):
                    st.text_area("Company ATS career URLs", value=st.session_state.get("search_ats_urls", current_ats), placeholder="https://company.wd5.myworkdayjobs.com/Careers\nhttps://boards.greenhouse.io/company", key="modal_search_ats_urls", height=140)
                    apply = st.form_submit_button("✓  Apply & close", type="primary", width="stretch")
                if apply:
                    st.session_state["search_ats_urls"] = st.session_state.get("modal_search_ats_urls", "")
                    st.session_state["search_signal_ats"] = True
                    st.session_state["search_wheel"] = "ats"
                    st.rerun()
                if st.button("Close", key="close_ats_modal", width="stretch"):
                    st.rerun()

        # Once every search signal has been configured, keep the workspace clean:
        # the four setup tabs disappear and only Find + Reset remain. Reset
        # brings the configuration controls back so the user can start over.
        all_signals_ready = all(st.session_state.get(f"search_signal_{key}", False) for key in ("sources", "profile", "date", "ats"))
        if not all_signals_ready:
            tab_cols = st.columns(4, gap="small")
            for col, label in zip(tab_cols, wheel_options):
                setting_key = selected_map[label]
                with col:
                    is_active = setting_key == active_setting
                    if st.button(label, key=f"search_tab_{setting_key}", type="primary" if is_active else "secondary", width="stretch"):
                        st.session_state["search_wheel"] = setting_key
                        _search_settings_dialog(setting_key)

        # Compact at-a-glance summary; detailed controls are intentionally hidden in the modal.
        source_preset_now = st.session_state.get("search_source_preset", saved_preset)
        if source_preset_now not in preset_values: source_preset_now = "open"
        source_label_now = dict(zip(preset_values, preset_labels)).get(source_preset_now, "Open source")
        profile_field_now = st.session_state.get("search_field", profile.get("field", "")) or "Any role"
        profile_location_now = st.session_state.get("search_location", profile.get("location", profile.get("city", ""))) or "Any location"
        date_now = st.session_state.get("search_date_window", profile.get("date_window", "Past 7 days"))
        ats_now = len([x for x in st.session_state.get("search_ats_urls", "\n".join(state.get("settings", {}).get("ats_urls") or [])).splitlines() if x.strip()])
        st.caption(f"Active search · **{source_label_now}** · **{profile_field_now}** · **{profile_location_now}** · **{date_now}** · **{ats_now} ATS")

        # Resolve all search values from session state so the modal is the single source of truth.
        source_preset = source_preset_now
        if source_preset == "open":
            search_mode = "free"; free_source_names = st.session_state.get("search_free_sources", [x for x in configured_free if x in OPEN_SOURCE_DEFAULTS] or OPEN_SOURCE_DEFAULTS); source_names = []
        elif source_preset == "linkedin":
            search_mode = "free"; free_source_names = ["LinkedIn"]; source_names = []
        else:
            search_mode = "apify"; free_source_names = []; source_names = st.session_state.get("search_apify_sources", [x for x in configured_names if x in ACTOR_CATALOG])

        field = st.session_state.get("search_field", profile.get("field", ""))
        industry = st.session_state.get("search_industry", profile.get("industry", ""))
        location = st.session_state.get("search_location", profile.get("location", profile.get("city", "")))
        experience = st.session_state.get("search_experience", profile.get("experience", "Any"))
        language = st.session_state.get("search_language", profile.get("language", "Any"))
        date_options = {"Past 24 hours": 1, "Past 3 days": 3, "Past 7 days": 7}
        date_window = st.session_state.get("search_date_window", profile.get("date_window", "Past 7 days"))
        if date_window not in date_options: date_window = "Past 7 days"
        ats_urls = [x.strip() for x in st.session_state.get("search_ats_urls", "\n".join(state.get("settings", {}).get("ats_urls") or [])).splitlines() if x.strip()]

        action_cols = st.columns([1, 1], gap="small") if all_signals_ready else st.columns([1], gap="small")
        with action_cols[0]:
            submitted = st.button("🔎  Find matching jobs", key="search_wheel_submit", type="primary", width="stretch")
        if all_signals_ready:
            with action_cols[1]:
                reset_search = st.button("↺  Reset search", key="search_wheel_reset", width="stretch")
        else:
            reset_search = False

        if reset_search:
            # Reset only the current New Search configuration. Existing jobs,
            # applications, CVs and other user data remain untouched.
            for key in (
                "search_source_preset", "search_free_sources", "search_apify_sources",
                "search_field", "search_industry", "search_location",
                "search_experience", "search_language", "search_date_window",
                "search_ats_urls",
            ):
                st.session_state.pop(key, None)
            for key in ("sources", "profile", "date", "ats"):
                st.session_state[f"search_signal_{key}"] = False
            st.session_state["search_wheel"] = "profile"
            st.rerun()

        if submitted:
            profile.update({"field": str(field).strip(), "industry": str(industry).strip(), "location": str(location).strip(), "experience": experience, "language": language, "date_window": date_window})
            save_state(state)
            try:
                actor_ids = [ACTOR_CATALOG[name]["id"] for name in source_names]
                if search_mode in {"apify", "both"} and not actor_ids:
                    raise RuntimeError("Select at least one Apify Actor for the selected search method.")

                # v1.6.0 UX: use a live status surface instead of a tiny
                # notification below the button. The status is deliberately
                # staged for ~3 seconds before the real search begins so the
                # user sees an unmistakable scanning transition.
                with st.status("Scanning job sources…", expanded=True) as search_status:
                    st.write("🔎 Finding fresh matches…")
                    time.sleep(0.75)
                    search_status.update(label="Matching jobs to your profile…", state="running")
                    st.write("🎯 Resolving locations and preparing the live map…")
                    time.sleep(0.75)
                    search_status.update(label="Resolving job locations…", state="running")
                    time.sleep(0.75)
                    search_status.update(label="Building live job map…", state="running")
                    os.environ["JOB_TRACKER_DATE_WINDOW_DAYS"] = str(date_options[date_window])
                    results = search_jobs(str(field).strip(), str(location).strip(), str(industry).strip(), experience, language=language, limit=10000, actor_ids=actor_ids, search_mode=search_mode, free_sources=free_source_names, ats_urls=ats_urls)
                    search_status.update(label=f"Search complete · {len(results)} jobs found", state="complete", expanded=False)
                for _signal in ("sources", "profile", "date", "ats"):
                    st.session_state[f"search_signal_{_signal}"] = True
                state.setdefault("settings", {})["actor_ids"] = actor_ids
                state.setdefault("settings", {})["job_search_mode"] = search_mode
                state.setdefault("settings", {})["job_source_preset"] = source_preset
                state.setdefault("settings", {})["free_sources"] = free_source_names
                state.setdefault("settings", {})["ats_urls"] = ats_urls
                state["jobs"] = results
                state["search_history"].append({"field": str(field).strip(), "industry": str(industry).strip(), "location": str(location).strip(), "experience": experience, "language": language, "count": len(results), "searched_at": datetime.now().isoformat(timespec="seconds")})
                state["search_history"] = state["search_history"][-25:]
                save_state(state)
                notify_success(f"Found {len(results)} jobs from the selected sources.")
                # Re-render so the live map immediately reflects this search.
                st.rerun()
            except Exception as exc:
                notify_error(str(exc))

    with results_col:
        # Results are deliberately contained in a fixed-height internal pane.
        # This keeps the entire search workspace on one screen while allowing
        # hundreds of jobs to scroll inside the results section.
        with st.container(height=610, border=False):
            st.markdown('<div class="jobsync-results-panel-title"><span>LIVE RESULTS</span><b>Search results</b></div>', unsafe_allow_html=True)
            if state["jobs"]:
                source_counts = {}; source_warnings = set()
                for row in state["jobs"]:
                    source_name = row.get("source") or row.get("actor") or "Unknown"
                    source_counts[source_name] = source_counts.get(source_name, 0) + 1
                    for warning in row.get("warnings", []) or []: source_warnings.add(str(warning))
                summary = " · ".join(f"{html.escape(str(k))}: {v}" for k, v in sorted(source_counts.items()))
                st.markdown(f'<div class="jobsync-results-summary">{len(state["jobs"])} jobs · {summary}</div>', unsafe_allow_html=True)
                if source_warnings:
                    with st.expander("Source diagnostics"):
                        for warning in sorted(source_warnings): st.caption("⚠ " + warning)
                for idx, job in enumerate(state["jobs"]):
                    already_applied = any(r.get("url") == job.get("url") for r in state["applied"] if r.get("url"))
                    with st.container(border=True):
                        left_job, right_job = st.columns([1.35, .65], gap="small")
                        with left_job:
                            st.markdown(f"<div class='job-title'>{html.escape(job.get('title','Untitled'))}</div>", unsafe_allow_html=True)
                            st.markdown(f"<div class='job-company'>{html.escape(job.get('company','Unknown company'))} · {html.escape(job.get('location',''))}</div>", unsafe_allow_html=True)
                            tags = [x for x in [job.get('work_type'), job.get('contract_type'), job.get('experience')] if x]
                            if job.get('language_required'): tags.append(f"Language required: {job['language_required']}")
                            if tags: st.markdown(" ".join(f"<span class='pill'>{html.escape(str(x))}</span>" for x in tags), unsafe_allow_html=True)
                            st.caption(f"Posted: {job.get('posted_date','Unknown')} · Source: {job.get('source') or job.get('actor','Apify')} · Salary: {job.get('salary') or 'Not listed'}")
                            raw_desc = str(job.get("description") or "").strip(); desc = html.unescape(raw_desc)
                            for _ in range(2):
                                decoded = html.unescape(desc)
                                if decoded == desc: break
                                desc = decoded
                            if "<" in desc and ">" in desc:
                                from bs4 import BeautifulSoup
                                desc = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)
                            desc = re.sub(r"\s+", " ", desc).strip()
                            if desc:
                                with st.expander("Preview job description"):
                                    st.write(desc[:2800] + ("…" if len(desc) > 2800 else ""))
                        with right_job:
                            if job.get("url"): st.link_button("Open job ↗", job["url"], width="stretch")
                            if already_applied:
                                notify_success("Tracked")
                            elif st.button("Mark applied", key=f"mark_{idx}", width="stretch"):
                                applied_record = {**job, "applied_date": datetime.now().strftime("%Y-%m-%d"), "status": "Applied", "cv_path": "", "cover_letter_path": ""}
                                state["applied"].append(applied_record)
                                save_state(state)
                                # The newest applied vacancy becomes the natural CV prefill candidate.
                                st.session_state["cv_entry_job"] = dict(applied_record)
                                notify_success("Application recorded. This job is ready to pre-fill in CV Studio.")
                                st.rerun()
                            bookmark_label = "🔖 Bookmarked" if _is_bookmarked(job) else "🔖 Save bookmark"
                            if st.button(bookmark_label, key=f"bookmark_{idx}", width="stretch"):
                                added, message = _toggle_bookmark(job)
                                notify_success(message) if added else notify_error(message)
                                st.rerun()
                            if st.button("Prepare CV", key=f"cv_{idx}", width="stretch"):
                                reset_cv_studio_for_new_preparation(keep_selected_job=True, selected_job=job)
                                st.session_state.selected_job_index = idx
                                go("CV & Cover Letter")
            else:
                st.markdown('<div class="jobsync-search-empty"><div class="jobsync-search-empty-icon">⌕</div><div class="jobsync-search-empty-title">Your next opportunity starts here.</div><div class="jobsync-search-empty-copy">Configure the signal on the left, then run your search. Results stay in this panel.</div></div>', unsafe_allow_html=True)

# ---------------- APPLIED ----------------
elif page == "Applied Jobs":
    render_modern_page_header("Applied Jobs")
    applied = state["applied"] or []
    counts = Counter((r.get("status") or "Applied") for r in applied)
    interviews = counts.get("Interview", 0)
    offers = counts.get("Offer", 0)
    rejected = counts.get("Rejected", 0)
    shortlisted = counts.get("Shortlisted", 0)
    active = sum(counts.get(s, 0) for s in ("Applied", "Shortlisted", "Interview"))
    response_rate = (interviews / len(applied) * 100) if applied else 0

    st.markdown(
        f'''<div class="applied-overview"><div class="applied-stat main"><div class="applied-stat-label">Total pipeline</div><div class="applied-stat-value">{len(applied)}</div><div class="applied-stat-note">{active} active-stage applications</div></div><div class="applied-stat interview"><div class="applied-stat-label">Interview</div><div class="applied-stat-value">{interviews}</div><div class="applied-stat-note">{response_rate:.0f}% of total</div></div><div class="applied-stat offer"><div class="applied-stat-label">Offers</div><div class="applied-stat-value">{offers}</div><div class="applied-stat-note">Positive outcomes</div></div><div class="applied-stat"><div class="applied-stat-label">Shortlisted</div><div class="applied-stat-value">{shortlisted}</div><div class="applied-stat-note">Still progressing</div></div><div class="applied-stat rejected"><div class="applied-stat-label">Rejected</div><div class="applied-stat-value">{rejected}</div><div class="applied-stat-note">Closed outcomes</div></div></div>''',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="applied-command"><div class="applied-command-left"><div class="applied-command-icon">↗</div><div><div class="applied-command-title">Tracker & export center</div><div class="applied-command-copy">Keep the local Excel tracker synchronized with your JobSync application records.</div></div></div><span class="applied-command-badge">LOCAL DATA</span></div>', unsafe_allow_html=True)
    st.markdown('<div class="applied-actions">', unsafe_allow_html=True)
    x1, x2, x3 = st.columns([1.15, 1.15, 2.7])
    with x1:
        if st.button("📊 Sync Excel tracker", type="primary", width="stretch", key="applied_export_excel"):
            try:
                out = export_applied_jobs_xlsx(applied, TRACKER)
                notify_success(f"Updated {out.name}")
            except Exception as exc:
                notify_error(f"Could not create Excel tracker: {exc}")
    with x2:
        if TRACKER.exists():
            st.download_button("Download tracker", TRACKER.read_bytes(), file_name=TRACKER.name, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch")
    with x3:
        if TRACKER.exists():
            st.caption(f"Tracker ready · {TRACKER.name}")
    st.markdown('</div>', unsafe_allow_html=True)

    bookmarks = state.get("bookmarks", []) or []
    st.markdown(
        f'<div class="applied-list-head" style="margin-top:.8rem"><div><div class="applied-list-kicker">BOOKMARKED JOBS</div><div class="applied-list-title">Saved opportunities</div></div><div class="applied-list-copy">{len(bookmarks)} saved · linked to the original posting</div></div>',
        unsafe_allow_html=True,
    )
    if not bookmarks:
        st.markdown('<div class="info-card"><div class="card-heading">No bookmarked jobs yet</div><div class="card-body">Use 🔖 Save bookmark under a search result to keep an opportunity here even before you apply.</div></div>', unsafe_allow_html=True)
    else:
        for bidx, bookmark in enumerate(bookmarks):
            btitle = html.escape(str(bookmark.get("title") or "Untitled job"))
            bcompany = html.escape(str(bookmark.get("company") or "Company not entered"))
            blocation = html.escape(str(bookmark.get("location") or "Location not specified"))
            burl = str(bookmark.get("url") or "").strip()
            bsource = html.escape(str(bookmark.get("source") or bookmark.get("actor") or "Source"))
            st.markdown(
                f'<div class="applied-card" style="margin:.38rem 0"><div class="applied-card-top"><div><div class="applied-card-index">BOOKMARK {bidx + 1:02d}</div><div class="applied-card-title">{btitle}</div><div class="applied-card-company"><b>{bcompany}</b> · {blocation}</div><div class="applied-card-meta"><span class="applied-mini-chip">{bsource}</span><span class="applied-mini-chip">Saved {html.escape(str(bookmark.get("bookmarked_at") or ""))}</span></div></div></div></div>',
                unsafe_allow_html=True,
            )
            bc1, bc2 = st.columns([1, 1], gap="small")
            with bc1:
                if burl:
                    st.link_button("Open original job ↗", burl, width="stretch")
                else:
                    st.button("No posting link", disabled=True, width="stretch", key=f"bookmark_nourl_{bidx}")
            with bc2:
                if st.button("Remove bookmark", key=f"remove_bookmark_{bidx}", width="stretch"):
                    key = _bookmark_key(bookmark)
                    state["bookmarks"] = [x for x in (state.get("bookmarks") or []) if _bookmark_key(x) != key]
                    save_state(state)
                    notify_success("Bookmark removed.")
                    st.rerun()

    if not applied:
        st.markdown('<div class="applied-empty"><div class="applied-empty-icon">✓</div><div class="applied-empty-title">Your pipeline is ready for its first application.</div><div class="applied-empty-copy">Use New Search → Mark applied to bring an opportunity into this command deck. Once recorded, status and document controls will appear here.</div></div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="applied-list-head"><div><div class="applied-list-kicker">APPLICATION STREAM</div><div class="applied-list-title">Tracked opportunities</div></div><div class="applied-list-copy">{len(applied)} records · newest entries remain in your existing tracker</div></div>', unsafe_allow_html=True)
        status_values = ["Applied", "Shortlisted", "Interview", "Offer", "Rejected", "Withdrawn"]
        cv_options = [""] + [d["path"] for d in generated_cvs()]
        cl_options = [""] + [d["path"] for d in generated_letters()]
        for idx, row in enumerate(applied):
            old_status = row.get("status", "Applied")
            title = html.escape(row.get("title", "Unknown"))
            company = html.escape(row.get("company", "Unknown"))
            location = html.escape(row.get("location", "") or "Location not specified")
            source = html.escape(row.get("source", "") or "Source not specified")
            date_applied = html.escape(row.get("applied_date", "") or "Date not specified")
            url = html.escape(row.get("url", "") or "")
            st.markdown(f'''<div class="applied-card"><div class="applied-card-top"><div><div class="applied-card-index">APPLICATION {idx + 1:02d}</div><div class="applied-card-title">{title}</div><div class="applied-card-company"><b>{company}</b> · {location}</div><div class="applied-card-meta"><span class="applied-mini-chip">Applied {date_applied}</span><span class="applied-mini-chip">{source}</span>{'<span class="applied-mini-chip">URL linked</span>' if url else ''}</div></div><div class="applied-card-status"><div class="applied-card-status-label">Current stage</div><span class="status-pill {status_class(old_status)}">{html.escape(old_status)}</span></div></div><div class="applied-card-controls">''', unsafe_allow_html=True)
            h1, h2, h3 = st.columns([1.05, 1.05, .7])
            with h1:
                status = st.selectbox("Pipeline status", status_values, index=status_values.index(old_status) if old_status in status_values else 0, key=f"status_{idx}")
            with h2:
                cv_path = st.selectbox("CV used", cv_options, index=cv_options.index(row.get("cv_path", "")) if row.get("cv_path", "") in cv_options else 0, key=f"cvused_{idx}")
            with h3:
                cl_path = st.selectbox("Cover letter", cl_options, index=cl_options.index(row.get("cover_letter_path", "")) if row.get("cover_letter_path", "") in cl_options else 0, key=f"clused_{idx}")
            if status != old_status:
                row["status"] = status
                save_state(state)
            if cv_path != row.get("cv_path") or cl_path != row.get("cover_letter_path"):
                row["cv_path"] = cv_path
                row["cover_letter_path"] = cl_path
                save_state(state)
            st.markdown('<div class="applied-card-link">', unsafe_allow_html=True)
            if row.get("url"):
                st.link_button("Open original job ↗", row["url"], width="stretch")
            st.markdown('</div></div></div>', unsafe_allow_html=True)

# ---------------- UPDATES CENTER ----------------
elif page == "Updates":
    render_modern_page_header("Updates")
    email_tab, software_tab = st.tabs(["✉  EMAILS", "↗  SOFTWARE UPDATE"])
    with email_tab:
        gmail_col, linkedin_col = st.columns(2, gap="large")
        with gmail_col:
            st.markdown('<div class="updates-panel"><div class="updates-panel-head"><div class="updates-panel-icon">G</div><div><div class="updates-panel-title">Gmail</div><div class="updates-panel-sub">Recruitment emails · read-only</div></div></div>', unsafe_allow_html=True)
            gmail_email=state.get("settings",{}).get("gmail_email","")
            if gmail_email:
                st.markdown(f'<div class="updates-status"><span class="dot"></span><span>Connected · {html.escape(gmail_email)}</span></div>', unsafe_allow_html=True)
                if st.button("↻  Sync application emails",width="stretch",key="updates_gmail_sync"):
                    try:
                        with st.spinner("Checking recent recruitment emails…"):
                            updates=sync_gmail(state.get("applied",[]),days=30,max_messages=50)
                        state.setdefault("settings",{})["gmail_last_sync"]=datetime.now().isoformat(timespec="seconds"); state["gmail_updates"]=updates; save_state(state)
                        notify_success(f"Found {len(updates)} relevant email updates."); st.rerun()
                    except Exception as exc: notify_error(f"Gmail sync failed: {exc}")
                if st.button("Disconnect Gmail",width="stretch",key="updates_gmail_disconnect"):
                    disconnect_gmail(); state.setdefault("settings",{})["gmail_email"]=""; state.setdefault("settings",{}).pop("gmail_last_sync",None); state["gmail_updates"]=[]; save_state(state); notify_success("Gmail disconnected locally."); st.rerun()
                last_sync=state.get("settings",{}).get("gmail_last_sync","")
                if last_sync: st.caption(f"Last synced: {last_sync}")
            else:
                st.markdown('<div class="updates-empty">Gmail is not connected yet.<br>Connect your Google account to surface recruitment messages here.</div>',unsafe_allow_html=True)
                if st.button("Connect Gmail",type="primary",width="stretch",key="updates_gmail_connect"):
                    try:
                        with st.spinner("Opening Google login…"):
                            service=get_gmail_service(); profile_info=service.users().getProfile(userId="me").execute()
                        state.setdefault("settings",{})["gmail_email"]=profile_info.get("emailAddress",""); save_state(state); notify_success(f"Gmail connected: {profile_info.get('emailAddress','account')}"); st.rerun()
                    except Exception: notify_error("Could not connect Gmail. Please try again.")
            gmail_updates=state.get("gmail_updates",[])
            st.markdown('<div class="section-kicker" style="margin-top:14px">RECENT GMAIL SIGNALS</div>',unsafe_allow_html=True)
            if gmail_updates:
                for item in gmail_updates[:8]: st.markdown(f'<div class="update-row"><span class="update-dot" style="background:#37d49a"></span><div><b>{html.escape(str(item.get("status","Recruitment update")))}</b><br>{html.escape(str(item.get("subject","(No subject)")))}</div></div>',unsafe_allow_html=True)
            else: st.caption("No recruitment email signals synced yet.")
            st.markdown('</div>',unsafe_allow_html=True)
        with linkedin_col:
            st.markdown('<div class="updates-panel"><div class="updates-panel-head"><div class="updates-panel-icon">in</div><div><div class="updates-panel-title">LinkedIn</div><div class="updates-panel-sub">Network notifications · persistent session</div></div></div>',unsafe_allow_html=True)
            linkedin_profile_url=state.get("settings",{}).get("linkedin_profile_url","").strip()
            if linkedin_profile_url:
                linkedin_notifications_enabled = bool(state.get("settings", {}).get("linkedin_notifications_enabled", False))
                st.markdown('<div class="updates-status"><span class="dot"></span><span>Profile configured</span></div>',unsafe_allow_html=True)
                if linkedin_notifications_enabled:
                    st.markdown('<div class="updates-enabled"><span><strong>Notifications active</strong><br>LinkedIn notification sync is enabled for this workspace.</span><span>● LIVE</span></div>',unsafe_allow_html=True)
                else:
                    st.markdown('<div class="updates-action-note"><strong>Notifications are not active.</strong><br>Open Settings → LinkedIn to connect or activate notification access. This page stays focused on reading and syncing signals.</div>', unsafe_allow_html=True)
                if st.button("↻  Sync LinkedIn notifications",width="stretch",key="updates_linkedin_sync"):
                    try:
                        with st.spinner("Checking LinkedIn notifications…"):
                            linkedin_updates=sync_linkedin_notifications(linkedin_profile_url,limit=30,login_wait_seconds=300)
                        state["linkedin_updates"]=linkedin_updates; state.setdefault("settings",{})["linkedin_last_sync"]=datetime.now().isoformat(timespec="seconds"); save_state(state); notify_success(f"LinkedIn sync completed — {len(linkedin_updates)} notifications found."); st.rerun()
                    except Exception as exc: notify_error(f"LinkedIn sync failed: {exc}")
                st.link_button("Open LinkedIn profile ↗",linkedin_profile_url,width="stretch")
                last_sync=state.get("settings",{}).get("linkedin_last_sync","")
                if last_sync: st.caption(f"Last synced: {last_sync}")
            else: st.markdown('<div class="updates-empty">LinkedIn profile is not configured.<br>Add it in Settings to enable notification sync.</div>',unsafe_allow_html=True)
            linkedin_updates=state.get("linkedin_updates",[])
            st.markdown('<div class="section-kicker" style="margin-top:14px">RECENT LINKEDIN SIGNALS</div>',unsafe_allow_html=True)
            if linkedin_updates:
                for item in linkedin_updates[:8]: st.markdown(f'<div class="update-row"><span class="update-dot" style="background:#62dcff"></span><div>{html.escape(str(item.get("message","LinkedIn notification")))}</div></div>',unsafe_allow_html=True)
            else: st.caption("No LinkedIn notification signals synced yet.")
            st.markdown('</div>',unsafe_allow_html=True)
    with software_tab:
        cfg_root=_find_github_updater_root(); repo_owner="Rep7oR"; repo_name="Job-Tracker"
        if cfg_root:
            try:
                cfg_data=json.loads((cfg_root/"update-config.json").read_text(encoding="utf-8")); repo_owner=str(cfg_data.get("github_owner") or repo_owner); repo_name=str(cfg_data.get("github_repo") or repo_name)
            except Exception: pass
        software_html = (
            '<div class="software-grid"><div class="software-card"><div class="software-label">CURRENT INSTALLATION</div>'
            '<div class="software-version">v' + html.escape(APP_VERSION) + '</div>'
            '<div class="software-meta"><span class="software-chip">JobSync</span><span class="software-chip">Windows desktop</span><span class="software-chip">GitHub releases</span></div>'
            '<div class="updates-copy" style="margin-top:12px">Your installed application is the reference point. Checking only looks for a newer GitHub release — nothing changes unless an update is actually available.</div></div>'
            '<div class="software-card update-check-card"><div class="update-radar"></div><div class="update-check-title">Release radar</div>'
            '<div class="update-check-copy">Scan the official repository for the newest JobSync installer.</div></div></div>'
        )
        st.markdown(software_html, unsafe_allow_html=True)
        st.write("")
        check_placeholder=st.empty()
        if st.button("⌁  Check for software updates",key="updates_github_check",type="primary",width="stretch"):
            check_placeholder.markdown('<div class="software-card update-check-card"><div class="update-radar"></div><div class="update-check-title">Checking GitHub…</div><div class="update-check-copy">Comparing your installed version with the latest release.</div></div>',unsafe_allow_html=True)
            import subprocess
            updater_root=_find_github_updater_root()
            if updater_root is None: notify_error(f"GitHub updater files not found. Expected: {PACKAGE_DIR / 'github'}")
            else:
                try:
                    updater_path=updater_root/"updater.ps1"; cfg_path=updater_root/"update-config.json"
                    completed=subprocess.run(["powershell.exe","-NoLogo","-NoProfile","-ExecutionPolicy","Bypass","-File",str(updater_path),"-InstallDir",str(PACKAGE_DIR),"-ConfigPath",str(cfg_path)],cwd=str(PACKAGE_DIR),capture_output=True,text=True,timeout=120)
                    output_text=(completed.stdout or completed.stderr or "").strip(); result_path=_github_update_state_path(); result={}
                    try:
                        if result_path.exists(): result=json.loads(result_path.read_text(encoding="utf-8-sig"))
                    except Exception: result={}
                    if completed.returncode!=0 or result.get("error"): notify_error(f"Update check failed: {str(result.get('error') or output_text or 'unknown error')[-1000:]}")
                    elif result.get("downloaded"): notify_success(f"Update v{result.get('latest_version')} is ready. The installer will open automatically.")
                    elif result.get("up_to_date"): notify_success(f"You are up to date (v{result.get('current_version',APP_VERSION)}).")
                    else: notify_success(output_text[-1000:] or "Update check completed.")
                    st.rerun()
                except Exception as exc: notify_error(f"Could not run the updater: {exc}")
        updater_root=_find_github_updater_root(); result_path=_github_update_state_path() if updater_root else None; result={}
        if result_path and result_path.exists():
            try: result=json.loads(result_path.read_text(encoding="utf-8"))
            except Exception: result={}
        checked=result.get("checked_at",""); latest=result.get("latest_version","")
        if result.get("downloaded"): st.markdown(f'<div class="updates-result">✓ Update v{html.escape(str(latest))} downloaded and installer started.<br><span style="opacity:.72">Checked: {html.escape(str(checked))}</span></div>',unsafe_allow_html=True)
        elif result.get("up_to_date"): st.markdown(f'<div class="updates-result">✓ You are running the latest available release · v{html.escape(str(result.get("current_version",APP_VERSION)))}<br><span style="opacity:.72">Checked: {html.escape(str(checked))}</span></div>',unsafe_allow_html=True)
        elif result.get("error"): st.error(f"Last check failed: {result.get('error')}")
        else: st.caption("No software update check has been run from this installation yet.")
        st.link_button("View official GitHub releases ↗",f"https://github.com/{repo_owner}/{repo_name}/releases",width="stretch")

# ---------------- GMAIL UPDATES ----------------
elif page == "Gmail Updates":
    render_modern_page_header("Gmail Updates")
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">✉ Gmail connection</div>', unsafe_allow_html=True)
    st.caption("Connect your Gmail account to read recruitment updates in read-only mode.")

    if st.button("Connect Gmail", type="primary", width="stretch", key="gmail_connect"):
        try:
            with st.spinner("Opening Google login…"):
                service = get_gmail_service()
                profile_info = service.users().getProfile(userId="me").execute()
            state.setdefault("settings", {})["gmail_email"] = profile_info.get("emailAddress", "")
            save_state(state)
            notify_success(f"Gmail connected: {profile_info.get('emailAddress', 'account')}")
            st.rerun()
        except Exception as exc:
            # Keep the normal UI simple, but show the real local exception in a
            # collapsible diagnostic so connection failures can be fixed without
            # exposing OAuth configuration in the Gmail workflow.
            notify_error("Could not connect Gmail. Please try again.")
            with st.expander("Connection details", expanded=False):
                st.code(str(exc))

    gmail_email = state.get("settings", {}).get("gmail_email", "")
    if gmail_email:
        notify_success(f"Connected: {gmail_email}")
    else:
        st.info("Not connected. Click Connect Gmail to sign in with Google.")

    if gmail_email:
        c1, c2 = st.columns(2)
        with c1:
            if st.button("↻ Sync application emails", width="stretch", key="gmail_sync"):
                try:
                    with st.spinner("Checking recent recruitment emails…"):
                        updates = sync_gmail(state.get("applied", []), days=30, max_messages=50)
                    state.setdefault("settings", {})["gmail_last_sync"] = datetime.now().isoformat(timespec="seconds")
                    state["gmail_updates"] = updates
                    save_state(state)
                    notify_success(f"Found {len(updates)} relevant email updates.")
                    st.rerun()
                except Exception as exc:
                    notify_error(f"Gmail sync failed: {exc}")
        with c2:
            if st.button("Disconnect Gmail", width="stretch", key="gmail_disconnect"):
                disconnect_gmail()
                state.setdefault("settings", {})["gmail_email"] = ""
                state.setdefault("settings", {}).pop("gmail_last_sync", None)
                state["gmail_updates"] = []
                save_state(state)
                notify_success("Gmail disconnected locally.")
                st.rerun()

    st.markdown('<div class="info-card" style="margin-top:14px;"><div class="card-body">Connect your Google account in the browser. JobSync only reads recruitment emails in read-only mode.</div></div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

    updates = state.get("gmail_updates", [])
    st.write("")
    st.markdown('<div class="section-kicker">APPLICATION UPDATES</div>', unsafe_allow_html=True)

    if not updates:
        st.markdown(
            '<div class="info-card"><div class="card-heading">No synced updates yet</div>'
            '<div class="card-body">Connect Gmail and click “Sync application emails” to find recruitment messages from the last 30 days.</div></div>',
            unsafe_allow_html=True,
        )
    else:
        applications = state.get("applied", [])
        for idx, update in enumerate(updates):
            matched = update.get("matched_application_index")
            confidence = float(update.get("match_confidence") or 0)
            match_text = "No application match"
            if matched is not None and matched < len(applications):
                app_row = applications[matched]
                match_text = f"{app_row.get('title', 'Job')} · {app_row.get('company', 'Company')}"

            st.markdown('<div class="card">', unsafe_allow_html=True)
            st.markdown(
                f"**{html.escape(update.get('status', 'Recruitment update'))}**  \n"
                f"{html.escape(update.get('subject', '(No subject)'))}"
            )
            st.caption(
                f"From: {update.get('sender', 'Unknown')} · Received: {update.get('received_at', 'Unknown')}"
            )
            st.caption(
                f"Application match: {match_text} · Confidence: {confidence:.0%}"
            )
            with st.expander("Email preview"):
                st.write(update.get("body", "")[:2500] or "No readable body found.")
            if matched is not None and matched < len(applications) and confidence >= 0.50:
                status_map = {
                    "Application received": "Applied",
                    "Interview": "Interview",
                    "Offer": "Offer",
                    "Rejected": "Rejected",
                    "Assessment": "Shortlisted",
                }
                suggested_status = status_map.get(update.get("status", ""), "")
                if suggested_status:
                    current_status = applications[matched].get("status", "Applied")
                    st.caption(f"Suggested application status: **{suggested_status}** (current: {current_status})")
                    if suggested_status != current_status:
                        if st.button(
                            f"✓ Apply status: {suggested_status}",
                            key=f"gmail_apply_status_{idx}",
                            width="stretch",
                        ):
                            applications[matched]["status"] = suggested_status
                            save_state(state)
                            notify_success(f"Application updated to {suggested_status}.")
                            st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)


# ---------------- LINKEDIN UPDATES ----------------
elif page == "LinkedIn Updates":
    render_modern_page_header("LinkedIn Updates")
    linkedin_profile_url = state.get("settings", {}).get("linkedin_profile_url", "").strip()

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">in LinkedIn connection</div>', unsafe_allow_html=True)
    if linkedin_profile_url:
        st.caption(f"Profile: {linkedin_profile_url}")
        lc1, lc2 = st.columns([1, 1])
        with lc1:
            if st.button("↻ Sync LinkedIn notifications", width="stretch", key="linkedin_sync"):
                try:
                    with st.spinner("Checking LinkedIn notifications…"):
                        linkedin_updates = sync_linkedin_notifications(
                            linkedin_profile_url, limit=30, login_wait_seconds=300
                        )
                    state["linkedin_updates"] = linkedin_updates
                    state.setdefault("settings", {})["linkedin_last_sync"] = datetime.now().isoformat(timespec="seconds")
                    save_state(state)
                    notify_success(f"LinkedIn sync completed — {len(linkedin_updates)} notifications found.")
                    st.rerun()
                except Exception as exc:
                    notify_error(f"LinkedIn sync failed: {exc}")
        with lc2:
            st.link_button("Open LinkedIn profile ↗", linkedin_profile_url, width="stretch")
        last_sync = state.get("settings", {}).get("linkedin_last_sync", "")
        if last_sync:
            st.caption(f"Last synced: {last_sync}")
    else:
        st.info("Add your LinkedIn profile URL in Settings to enable LinkedIn notification sync.")
    st.markdown('</div>', unsafe_allow_html=True)

    linkedin_updates = state.get("linkedin_updates", [])
    st.write("")
    st.markdown('<div class="section-kicker">RECENT LINKEDIN NOTIFICATIONS</div>', unsafe_allow_html=True)
    if linkedin_updates:
        table_rows = []
        for item in linkedin_updates:
            table_rows.append({
                "Received": item.get("received_at", ""),
                "Notification": item.get("message", ""),
                "Source": item.get("source", "LinkedIn"),
                "Link": item.get("url", ""),
            })
        st.dataframe(table_rows, width="stretch", hide_index=True)
    else:
        st.markdown(
            '<div class="info-card"><div class="card-heading">No LinkedIn updates synced yet</div>'
            '<div class="card-body">Add your profile in Settings, connect LinkedIn once in the browser, then sync notifications here.</div></div>',
            unsafe_allow_html=True,
        )


# ---------------- CV & COVER LETTER ----------------
elif page == "CV & Cover Letter":
    render_modern_page_header("CV & Cover Letter")
    """In-app document studio: one choice at a time, then AI generation and PDF save without leaving JobSync."""
    cv_cycle = int(st.session_state.get("cv_studio_cycle", 0))
    wizard_step = int(st.session_state.get("cv_wizard_step", 1))
    prompt_ready = bool(st.session_state.get("external_ai_prompt"))

    with st.container(key="cvwiz_reset_row"):
        _, reset_col = st.columns([4, 1])
        with reset_col:
            if st.button("↺ Start over", key=f"cvwiz_reset_all_{cv_cycle}", help="Clear the document type, AI model, job details and any generated draft, and return to step 1", width="stretch"):
                reset_cv_studio_for_new_preparation()
                st.rerun()
    st.markdown('<style>.st-key-cvwiz_reset_row{max-width:980px;margin:0 auto 6px;}</style>', unsafe_allow_html=True)

    st.markdown("""
    <style>
      .cvwiz { max-width:980px; margin:0 auto; }
      .cvwiz-hero { position:relative; padding:15px 20px 12px; border:1px solid rgba(255,255,255,.075); border-radius:20px; background:linear-gradient(110deg,rgba(8,20,31,.96),rgba(27,15,57,.94)); overflow:hidden; }
      .cvwiz-hero:after { content:""; position:absolute; width:250px; height:250px; right:-105px; top:-145px; border-radius:50%; border:1px solid rgba(144,113,255,.18); box-shadow:0 0 0 34px rgba(144,113,255,.035),0 0 0 70px rgba(144,113,255,.018); pointer-events:none; }
      .cvwiz-kicker { color:#8aa4ff; font-size:.55rem; font-weight:900; letter-spacing:.18em; }
      .cvwiz-title { margin-top:4px; color:#f8fafc; font-size:1.3rem; font-weight:900; letter-spacing:-.04em; }
      .cvwiz-sub { color:#8995a6; font-size:.65rem; margin-top:3px; }
      /* min-height was 470px with justify-content:center — on the short
         text-only steps (e.g. "Ready to build your CV?" + one ready-card)
         that left a large dead gap above and below the content, which is
         the empty middle box users were seeing. The card now hugs its
         actual content and only grows if a step genuinely has more in it. */
      .cvwiz-card { margin:12px auto 0; padding:22px 30px 20px; min-height:0; display:flex; flex-direction:column; align-items:center; justify-content:flex-start; border:1px solid rgba(255,255,255,.075); border-radius:22px; background:linear-gradient(145deg,rgba(10,17,25,.97),rgba(13,10,28,.96)); box-shadow:0 20px 55px rgba(0,0,0,.20); }
      .cvwiz-eyebrow { color:#8798b0; font-size:.55rem; font-weight:900; letter-spacing:.16em; text-transform:uppercase; text-align:center; }
      .cvwiz-question { color:#f2f5f9; font-size:1.05rem; font-weight:850; margin-top:7px; text-align:center; letter-spacing:-.02em; }
      .cvwiz-copy { color:#718094; font-size:.64rem; line-height:1.5; text-align:center; margin-top:5px; max-width:700px; }
      .cvwiz-choice-grid { width:100%; max-width:720px; margin:20px auto 0; }
      .cvwiz-card .stButton > button { min-height:74px !important; border-radius:16px !important; border:1px solid rgba(255,255,255,.08) !important; background:rgba(10,15,22,.90) !important; color:#e8edf5 !important; font-size:.73rem !important; font-weight:850 !important; }
      .cvwiz-card .stButton > button:hover { border-color:rgba(117,104,255,.55) !important; background:rgba(30,24,55,.92) !important; transform:translateY(-1px); }
      .cvwiz-progress { display:flex; justify-content:center; gap:8px; margin:18px 0 0; }
      .cvwiz-dot { width:7px; height:7px; border-radius:50%; background:#303744; }
      .cvwiz-dot.active { background:#6f59e8; box-shadow:0 0 0 4px rgba(111,89,232,.12); }
      .cvwiz-ready { width:100%; max-width:720px; margin:20px auto 0; padding:16px 18px; border:1px solid rgba(55,211,153,.20); background:rgba(16,31,31,.60); border-radius:15px; display:flex; flex-direction:column; gap:4px; }
      .cvwiz-ready b { color:#e9f3ef; font-size:.72rem; }
      .cvwiz-ready span { color:#718b86; font-size:.58rem; }
      .cvwiz-status { width:100%; max-width:720px; margin:14px auto 0; padding:9px 13px; border-radius:999px; border:1px solid rgba(255,255,255,.065); background:rgba(255,255,255,.018); color:#8190a4; font-size:.55rem; text-align:center; }
      .cvwiz-inline-progress{width:100%;max-width:760px;margin:14px auto 0;padding:15px 17px;border:1px solid rgba(105,91,235,.28);border-radius:16px;background:linear-gradient(145deg,rgba(11,18,30,.96),rgba(28,15,48,.95));box-shadow:0 18px 45px rgba(0,0,0,.18),0 0 28px rgba(90,82,220,.10)}
      .cvwiz-inline-progress-head{display:flex;align-items:center;gap:10px}.cvwiz-inline-orbit{width:34px;height:34px;border-radius:11px;display:grid;place-items:center;font-weight:950;color:#fff;background:linear-gradient(135deg,#2bb9e6,#7057e8,#c24eb9);box-shadow:0 0 22px rgba(91,103,255,.34);animation:cvwizOrbitPulse 1.8s ease-in-out infinite}.cvwiz-inline-title{color:#f1f5fb;font-size:.78rem;font-weight:900}.cvwiz-inline-sub{color:#75859b;font-size:.56rem;margin-top:2px}.cvwiz-inline-percent{margin-left:auto;color:#a9b9d0;font-size:.7rem;font-weight:850}.cvwiz-inline-track{height:9px;margin-top:13px;border-radius:999px;overflow:hidden;background:rgba(255,255,255,.065);border:1px solid rgba(255,255,255,.05)}.cvwiz-inline-track span{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,#2bb4dd,#6255e8,#b946bd);box-shadow:0 0 18px rgba(93,87,235,.42);background-size:200% 100%;animation:cvwizShimmerMove 1.6s linear infinite;transition:width .25s ease}.cvwiz-inline-stages{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin-top:11px}.cvwiz-inline-stage{padding:7px 4px;text-align:center;border:1px solid rgba(255,255,255,.055);border-radius:9px;color:#58667a;font-size:.43rem;font-weight:900;letter-spacing:.11em;background:rgba(255,255,255,.018)}.cvwiz-inline-stage.active{color:#bac8ff;border-color:rgba(106,93,235,.42);background:rgba(99,80,214,.12);animation:cvwizStageGlow 1.2s ease-in-out infinite alternate}.cvwiz-inline-stage.done{color:#75d8be;border-color:rgba(55,211,153,.18)}.cvwiz-inline-now{display:flex;align-items:center;gap:8px;margin-top:10px;color:#a9b7ca;font-size:.57rem}.cvwiz-inline-spinner{width:12px;height:12px;border-radius:50%;border:2px solid rgba(255,255,255,.16);border-top-color:#5cdbff;border-right-color:#7d62ff;animation:cvwizSpin .8s linear infinite}@keyframes cvwizSpin{to{transform:rotate(360deg)}}@keyframes cvwizOrbitPulse{50%{transform:translateY(-1px) scale(1.04);box-shadow:0 0 30px rgba(104,96,255,.42)}}@keyframes cvwizShimmerMove{to{background-position:-200% 0}}@keyframes cvwizStageGlow{to{box-shadow:0 0 16px rgba(103,90,234,.12)}}.cvwiz-blueprint{width:100%;max-width:760px;margin-top:9px;color:#64748a;font-size:.5rem;text-align:left;letter-spacing:.07em;text-transform:uppercase}.cvwiz-source-label{width:100%;max-width:760px;margin:14px auto 6px;color:#93a6bf;font-size:.52rem;font-weight:900;letter-spacing:.14em;text-transform:uppercase}.cvwiz-source-help{width:100%;max-width:760px;margin:0 auto 8px;color:#67778e;font-size:.56rem;line-height:1.45}
      .st-key-cvwiz_latex_box{width:100%;max-width:760px;margin:0 auto;}
      .st-key-cvwiz_latex_box [data-testid="stCode"]{font-size:.68rem!important;}
      .st-key-cvwiz_latex_box pre{max-height:140px!important;}
      .cvwiz-mini { width:100%; max-width:720px; margin:12px auto 0; color:#6f7d90; font-size:.58rem; text-align:center; }
      .cvwiz-modal-status { padding:14px 16px; border:1px solid rgba(111,89,232,.25); border-radius:14px; background:linear-gradient(145deg,rgba(10,17,28,.96),rgba(22,13,42,.96)); }
      .cvwiz-modal-stage { display:flex; justify-content:space-between; gap:16px; color:#eef2f7; font-size:.82rem; }
      .cvwiz-modal-stage span { color:#8aa4ff; font-weight:900; }
      .cvwiz-modal-eta { margin-top:7px; color:#8290a4; font-size:.66rem; }
      .cvwiz-modal-lock { margin-top:10px; color:#6f8096; font-size:.60rem; }
      .jobsync-generation-overlay { position:fixed; inset:0; z-index:2147483647; width:100vw; height:100vh; display:flex; align-items:center; justify-content:center; pointer-events:auto; }
      .jobsync-generation-backdrop { position:absolute; inset:0; background:rgba(2,7,14,.82); backdrop-filter:blur(12px); -webkit-backdrop-filter:blur(12px); }
      .jobsync-generation-dialog { position:relative; z-index:2; width:min(650px,calc(100vw - 42px)); padding:25px 28px 20px; border:1px solid rgba(116,96,255,.30); border-radius:22px; background:linear-gradient(145deg,rgba(10,18,30,.985),rgba(25,13,48,.985)); box-shadow:0 30px 100px rgba(0,0,0,.62),0 0 75px rgba(99,78,220,.14); color:#eef3fa; }
      .jobsync-generation-brand { display:flex; align-items:center; gap:9px; color:#9ab1ff; font-size:.58rem; font-weight:900; letter-spacing:.18em; }
      .jobsync-generation-brand b { margin-left:auto; color:#64758d; font-size:.48rem; letter-spacing:.14em; }
      .jobsync-gen-orbit { display:inline-flex; width:27px; height:27px; align-items:center; justify-content:center; border-radius:9px; color:white; font-size:.9rem; letter-spacing:-.04em; background:linear-gradient(135deg,#2eb7e5,#714ee8,#c347b7); box-shadow:0 0 22px rgba(91,103,255,.34); }
      .jobsync-generation-eyebrow { margin-top:20px; color:#7f92b0; font-size:.52rem; font-weight:900; letter-spacing:.17em; }
      .jobsync-generation-dialog h2 { margin:7px 0 4px; font-size:1.45rem; letter-spacing:-.035em; }
      .jobsync-generation-copy { margin:0; color:#8492a6; font-size:.68rem; line-height:1.5; }
      .jobsync-generation-percent { display:flex; align-items:end; justify-content:space-between; margin-top:20px; }
      .jobsync-generation-percent strong { font-size:1.55rem; letter-spacing:-.06em; background:linear-gradient(90deg,#37b7e4,#6d55e9,#c34db9); -webkit-background-clip:text; background-clip:text; color:transparent; }
      .jobsync-generation-percent span { color:#8c9aaf; font-size:.66rem; padding-bottom:5px; }
      .jobsync-generation-track { position:relative; height:10px; margin-top:10px; overflow:hidden; border-radius:999px; background:rgba(255,255,255,.075); border:1px solid rgba(255,255,255,.06); }
      .jobsync-generation-track div { position:relative; height:100%; border-radius:999px; background:linear-gradient(90deg,#2bb4dd,#6156e8,#b844bd); box-shadow:0 0 22px rgba(94,89,236,.45); transition:width .25s ease; overflow:hidden; }
      .jobsync-generation-track div:after { content:""; position:absolute; inset:0; background:linear-gradient(110deg,transparent 20%,rgba(255,255,255,.48) 48%,transparent 76%); transform:translateX(-100%); animation:jobsyncShimmer 1.25s linear infinite; }
      .jobsync-generation-stages { display:grid; grid-template-columns:repeat(5,1fr); gap:7px; margin-top:17px; }
      .jobsync-gen-stage { padding:8px 5px; text-align:center; border-radius:9px; border:1px solid rgba(255,255,255,.055); background:rgba(255,255,255,.025); color:#56657a; font-size:.46rem; font-weight:900; letter-spacing:.12em; }
      .jobsync-gen-stage.active { color:#b8c6ff; border-color:rgba(104,91,232,.42); background:rgba(95,76,200,.12); }
      .jobsync-gen-stage.done { color:#76d8c0; border-color:rgba(55,211,153,.18); }
      .jobsync-generation-current { display:flex; gap:12px; align-items:center; margin-top:15px; padding:11px 13px; border-radius:12px; border:1px solid rgba(255,255,255,.065); background:rgba(4,9,16,.42); }
      .jobsync-generation-current strong { display:block; font-size:.72rem; color:#e8edf5; }
      .jobsync-generation-current small { display:block; margin-top:3px; color:#708096; font-size:.57rem; line-height:1.4; }
      .jobsync-gen-spinner { flex:0 0 24px; width:24px; height:24px; border:2px solid rgba(255,255,255,.12); border-top-color:#6e5ce9; border-right-color:#38b9e4; border-radius:50%; animation:jobsyncSpin .8s linear infinite; }
      .jobsync-generation-footer { display:flex; justify-content:space-between; gap:10px; margin-top:15px; color:#596a80; font-size:.5rem; }
      @keyframes jobsyncSpin { to { transform:rotate(360deg); } }
      @keyframes jobsyncShimmer { to { transform:translateX(100%); } }
      @media (max-width:700px) { .jobsync-generation-dialog { padding:26px 22px 22px; } .jobsync-generation-brand b { display:none; } .jobsync-generation-footer { flex-direction:column; } }
      .cvwiz-card .stTextInput input, .cvwiz-card .stTextArea textarea { background:#090e15 !important; border:1px solid rgba(255,255,255,.08) !important; color:#e7edf5 !important; border-radius:12px !important; }
      .cvwiz-card .stTextInput, .cvwiz-card .stTextArea, .cvwiz-card .stFileUploader { width:100%; max-width:720px; }
      /* STEP 4's real panel: a genuine st.container() (not a cross-call HTML
         div) so the uploader, blueprint caption, Build button and any error
         are actually inside the same bordered box as the summary text. */
      .st-key-cvwiz_step4_panel { margin:12px auto 0; padding:22px 30px 20px; max-width:900px; display:flex; flex-direction:column; align-items:center; border:1px solid rgba(255,255,255,.075); border-radius:22px; background:linear-gradient(145deg,rgba(10,17,25,.97),rgba(13,10,28,.96)); box-shadow:0 20px 55px rgba(0,0,0,.20); }
      .st-key-cvwiz_step4_panel .stFileUploader,
      .st-key-cvwiz_step4_panel [data-testid="stCaptionContainer"],
      .st-key-cvwiz_step4_panel .stButton,
      .st-key-cvwiz_step4_panel [data-testid="stAlert"] { width:100%; max-width:720px; margin-top:14px; }
      .st-key-cvwiz_step4_panel .stButton > button { min-height:52px !important; border-radius:14px !important; }
    </style>
    """, unsafe_allow_html=True)

    def dots(total: int, active: int):
        html_dots = ''.join(f'<span class="cvwiz-dot {"active" if i == active else ""}"></span>' for i in range(1, total + 1))
        st.markdown(f'<div class="cvwiz-progress">{html_dots}</div>', unsafe_allow_html=True)

    if not prompt_ready and wizard_step == 1:
        st.markdown('<div class="cvwiz-card"><div class="cvwiz-eyebrow">STEP 1 OF 3</div><div class="cvwiz-question">What are you creating?</div><div class="cvwiz-copy">Choose one. JobSync generates it locally with its built-in AI — no account, no API key, nothing else to set up.</div><div class="cvwiz-choice-grid">', unsafe_allow_html=True)
        a,b=st.columns(2,gap="medium")
        with a:
            if st.button("CV\nTailored resume for this vacancy",key=f"cvwiz_cv_{cv_cycle}",width="stretch"):
                st.session_state["cv_wizard_doc"]="CV"; st.session_state["cv_wizard_ai"]=LOCAL_AI_DEFAULT; st.session_state["cv_wizard_step"]=3; st.rerun()
        with b:
            if st.button("Cover Letter\nFocused letter for this vacancy",key=f"cvwiz_cl_{cv_cycle}",width="stretch"):
                st.session_state["cv_wizard_doc"]="Cover Letter"; st.session_state["cv_wizard_ai"]=LOCAL_AI_DEFAULT; st.session_state["cv_wizard_step"]=3; st.rerun()
        st.markdown('</div></div>',unsafe_allow_html=True); dots(3,1)

    elif not prompt_ready and wizard_step == 3:
        provider=st.session_state.get("cv_wizard_ai",LOCAL_AI_DEFAULT); doc=st.session_state.get("cv_wizard_doc","CV")
        st.markdown(f'<div class="cvwiz-card"><div class="cvwiz-eyebrow">STEP 2 OF 3</div><div class="cvwiz-question">Which job should JobSync tailor it to?</div><div class="cvwiz-copy">Pick a saved vacancy or enter the missing details. JobSync auto-fills everything it already knows.</div>',unsafe_allow_html=True)
        jobs=state.get("search_results",[]) or []
        saved_jobs=[]
        for j in jobs:
            if isinstance(j,dict) and j.get("title"): saved_jobs.append(j)
        for j in state.get("applied",[]) or []:
            if isinstance(j,dict) and j.get("title") and j not in saved_jobs: saved_jobs.append(j)
        saved_jobs=saved_jobs[:100]
        entry_job = st.session_state.get("cv_entry_job") or {}
        # Direct preparation should show the chosen job first and only once.
        if entry_job and entry_job.get("title"):
            entry_key = _bookmark_key(entry_job)
            matching = [j for j in saved_jobs if _bookmark_key(j) == entry_key]
            if not matching:
                saved_jobs.insert(0, dict(entry_job))
            else:
                saved_jobs = matching + [j for j in saved_jobs if _bookmark_key(j) != entry_key]
        forced_job = bool(st.session_state.get("cv_entry_job"))
        mode = "Saved job" if forced_job else st.radio("Job source",["Saved job","Enter manually"],horizontal=True,key=f"cvwiz_jobmode_{cv_cycle}",label_visibility="collapsed")
        job={}
        if mode=="Saved job" and saved_jobs:
            labels=[f'{j.get("title","Untitled")} — {j.get("company") or "Company not entered"}' for j in saved_jobs]
            entry_job = st.session_state.get("cv_entry_job") or {}
            default_idx = 0
            if entry_job:
                for candidate_idx, candidate in enumerate(saved_jobs):
                    if (candidate.get("url") and candidate.get("url") == entry_job.get("url")) or (
                        candidate.get("title") == entry_job.get("title") and candidate.get("company") == entry_job.get("company")
                    ):
                        default_idx = candidate_idx
                        break
            idx=st.selectbox("Saved job",range(len(labels)),index=default_idx,format_func=lambda i:labels[i],key=f"cvwiz_saved_{cv_cycle}")
            job=dict(saved_jobs[idx])
            st.session_state["cv_wizard_job"]=job
        else:
            if mode=="Saved job" and not saved_jobs: st.info("No saved vacancy is available yet. Enter the job manually below.")
            job={"title":"","company":"","location":"","url":"","description":""}
        c1,c2=st.columns(2,gap="small")
        with c1: title=st.text_input("Job title",value=job.get("title", ""),placeholder="Job title",key=f"cvwiz_title_{cv_cycle}")
        with c2: company=st.text_input("Company",value=job.get("company", ""),placeholder="Company",key=f"cvwiz_company_{cv_cycle}")
        c1,c2=st.columns(2,gap="small")
        with c1: location=st.text_input("Location",value=job.get("location", ""),placeholder="Location",key=f"cvwiz_location_{cv_cycle}")
        with c2: url=st.text_input("Job posting URL",value=job.get("url", ""),placeholder="Optional URL",key=f"cvwiz_url_{cv_cycle}")
        description=st.text_area("Job description",value=job.get("description", ""),height=120,placeholder="Paste only if the saved vacancy does not already contain it.",key=f"cvwiz_desc_{cv_cycle}")
        st.session_state["cv_wizard_job"]={**job,"title":title,"company":company,"location":location,"url":url,"description":description}
        if st.button("Continue →",key=f"cvwiz_continue_{cv_cycle}",type="primary",width="stretch",disabled=not bool(title.strip())):
            st.session_state["cv_wizard_step"]=4; st.rerun()
        st.markdown('</div>',unsafe_allow_html=True); dots(3,2)

    elif not prompt_ready and wizard_step == 4:
        provider=st.session_state.get("cv_wizard_ai",LOCAL_AI_DEFAULT); doc=st.session_state.get("cv_wizard_doc","CV"); job=st.session_state.get("cv_wizard_job",{}) or {}
        # Everything for this step — the summary text, the uploader, the
        # blueprint caption, the Build button, and any error — renders
        # inside one real st.container() styled as a single panel (see the
        # cvwiz-step4-marker rule below), instead of the old pattern of an
        # HTML <div> opened in one st.markdown call and closed in another:
        # that only ever visually wrapped the first markdown call's own
        # fragment, so the uploader/button/errors always rendered as
        # separate, unstyled elements below an oversized, mostly-empty box.
        with st.container(key="cvwiz_step4_panel"):
            st.markdown(f'<div class="cvwiz-eyebrow">STEP 3 OF 3</div><div class="cvwiz-question">Ready to build your {html.escape(doc)}?</div><div class="cvwiz-copy">JobSync generates this locally with its built-in AI, validates the result, shows the source here, lets you copy it into Overleaf, and keeps the final PDF in the JobSync folder.</div><div class="cvwiz-ready"><b>{html.escape(job.get("title") or "Untitled role")}</b><span>{html.escape(job.get("company") or "Company not entered")} · {html.escape(job.get("location") or "Location not entered")}</span></div>',unsafe_allow_html=True)
            refs=st.file_uploader("Optional reference CV / cover letter",type=["pdf","tex","docx"],accept_multiple_files=True,key=f"cvwiz_refs_{cv_cycle}")
            template=st.session_state.get("cv_wizard_template","")
            blueprint_file = "cv_base.tex" if doc == "CV" else "cover_letter_base.tex"
            st.caption(f"CV blueprint: {blueprint_file}")
            if st.button("Build my document →",key=f"cvwiz_build_{cv_cycle}",type="primary",width="stretch"):
                try:
                    from services.cv_engine import load_builtin_template
                    evidence_refs=[]
                    latest=""
                    for uploaded in refs or []:
                        try:
                            raw=uploaded.getvalue(); suffix=Path(uploaded.name).suffix.lower()
                            if suffix in {".txt",".tex"}: text=raw.decode("utf-8",errors="ignore")
                            elif suffix in {".pdf",".docx"}:
                                temp=UPLOAD_REFERENCES/f"__prompt_{safe_name(Path(uploaded.name).stem)}_{cv_cycle}{suffix}"; temp.write_bytes(raw); text=extract_text(temp); temp.unlink(missing_ok=True)
                            else: text=""
                            if text.strip(): evidence_refs.append({"name":uploaded.name,"text":text.strip()[:14000],"reference_type":"document"})
                        except Exception as exc: notify_error(f"Could not read {uploaded.name}: {exc}")
                    evidence=build_reference_context(evidence_refs) if evidence_refs else ""
                    profile=state.get("profile",{}) or {}
                    if doc=="CV":
                        template = load_builtin_template("CV") if not template else template
                        blueprint_name = "cv_base.tex"
                    else:
                        template = load_builtin_template("Cover Letter") if not template else template
                        blueprint_name = "cover_letter_base.tex"
                    prompt=build_external_ai_prompt(job=job,references=evidence,profile=profile,template=template,document_type=doc,provider=provider)
                    st.session_state.update({"external_ai_prompt":prompt,"external_ai_provider":provider,"external_document_type_snapshot":doc,"external_job_snapshot":job,"external_template_snapshot":template,"cv_blueprint_name":blueprint_name,"cv_reference_names":[str(item.get("name")) for item in evidence_refs if item.get("name")],"cv_generation_status":"prompt_generated","cv_generation_running":False,"cv_generation_error":""})
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not build the document prompt: {exc}")
        dots(3,3)

    else:
        saved_job = st.session_state.get("external_job_snapshot", {}) or {}
        provider = st.session_state.get("external_ai_provider", LOCAL_AI_DEFAULT)
        doc = st.session_state.get("external_document_type_snapshot", "CV")
        prompt = st.session_state.get("external_ai_prompt", "")
        title = saved_job.get("title") or "Untitled role"
        company = saved_job.get("company") or "Company not entered"
        location = saved_job.get("location") or "Location not entered"
        status = st.session_state.get("cv_generation_status", "prompt_generated")
        generation_running = bool(st.session_state.get("cv_generation_running", False))
        cv_blueprint_name = st.session_state.get("cv_blueprint_name") or ("cv_base.tex" if doc == "CV" else "cover_letter_base.tex")
        pdf_path = str(st.session_state.get("cv_saved_pdf") or st.session_state.get("cv_compiled_pdf") or "")

        def _progress_markup(percent: int, message: str, detail: str) -> str:
            p = max(0, min(100, int(percent)))
            safe_msg = html.escape(str(message or "Working…"))
            safe_detail = html.escape(str(detail or "JobSync is processing your document."))
            stages = [("AI", 1, 38), ("CONTENT", 39, 72), ("LATEX", 73, 92), ("READY", 93, 100)]
            stage_html = "".join(
                '<div class="cvwiz-inline-stage {}">{}</div>'.format(
                    "done" if p >= end else "active" if start <= p < end else "",
                    label,
                )
                for label, start, end in stages
            )
            return (
                '<div class="cvwiz-inline-progress">'
                '<div class="cvwiz-inline-progress-head">'
                '<div class="cvwiz-inline-orbit">J</div>'
                f'<div><div class="cvwiz-inline-title">Creating your {html.escape(doc)}</div><div class="cvwiz-inline-sub">{safe_detail}</div></div>'
                f'<div class="cvwiz-inline-percent">{p}%</div>'
                '</div>'
                f'<div class="cvwiz-inline-track"><span style="width:{p}%"></span></div>'
                f'<div class="cvwiz-inline-stages">{stage_html}</div>'
                f'<div class="cvwiz-inline-now"><span class="cvwiz-inline-spinner"></span><span>{safe_msg}</span></div>'
                '</div>'
            )

        def _run_generation_inline() -> None:
            started = time.time()
            slot = st.empty()
            last_percent = 0

            def _eta(p: int) -> str:
                elapsed = max(0.1, time.time() - started)
                if p <= 5:
                    return "Calculating remaining time…"
                total = elapsed * 100.0 / max(p, 1)
                remaining = max(0.0, total - elapsed)
                if remaining < 60:
                    return f"About {max(1, int(remaining))} seconds remaining"
                return f"About {int(remaining // 60)} min {int(remaining % 60):02d} sec remaining"

            def _render_inline(message: str, percent: int, detail: str = "") -> None:
                nonlocal last_percent
                p = max(last_percent, min(100, int(percent)))
                last_percent = p
                eta = "Complete" if p >= 100 else _eta(p)
                slot.markdown(_progress_markup(p, message, f"{detail} · {eta}"), unsafe_allow_html=True)
                time.sleep(0.035)

            def _show_ai_progress(message: str, percent: int | None = None) -> None:
                text_now = str(message or "Working…")
                generated = int(st.session_state.get("cv_local_ai_chars", 0))
                if percent is None:
                    lower = text_now.lower()
                    if "download" in lower and "qwen" in lower:
                        percent = min(38, max(10, int(st.session_state.get("cv_generation_percent", 10))))
                    elif "writing" in lower or "content" in lower:
                        percent = min(72, max(39, 39 + int(generated / 900)))
                    else:
                        percent = int(st.session_state.get("cv_generation_percent", 10))
                st.session_state["cv_generation_percent"] = int(percent)
                _render_inline(text_now, int(percent), "Local AI is tailoring the document")

            st.session_state["cv_ai_progress_callback"] = _show_ai_progress
            st.session_state["cv_local_ai_chars"] = 0
            st.session_state["cv_generation_percent"] = 5
            try:
                _render_inline("Preparing the local AI engine…", 8, "Checking the private JobSync AI runtime")
                template_text = st.session_state.get("external_template_snapshot") or ""
                latex = _generate_latex_with_ai(provider, prompt, document_type=doc, template=template_text)
                _render_inline("AI content received. Validating the document…", 76, "Checking the locked blueprint structure")
                clean = extract_latex_code(latex)
                ok, msg = validate_external_latex(
                    doc, clean, template_text,
                    strict_structure=not (_is_local_ai_provider(provider) and doc == "CV"),
                )
                if not ok:
                    raise RuntimeError(msg)
                _render_inline("LaTeX validated. Finalizing the source…", 92, "The generated source stays in JobSync for review and manual copy to Overleaf")
                base = safe_name(title or doc) or doc.lower().replace(" ", "_")
                _render_inline("Complete — your document is ready.", 100, "Copy the source into Overleaf, then return here with the compiled PDF")
                st.session_state.update({
                    "cv_generation_status": "complete",
                    "cv_generation_running": False,
                    "cv_compiled_pdf": "",
                    "cv_saved_pdf": "",
                    "cv_latex_draft": clean,
                    "cv_latex_path": "",
                    "cv_generated_base": base,
                    "cv_generation_error": "",
                })
                st.session_state.pop("cv_ai_progress_callback", None)
                time.sleep(0.45)
                st.rerun()
            except Exception as exc:
                st.session_state.pop("cv_ai_progress_callback", None)
                st.session_state["cv_generation_running"] = False
                st.session_state["cv_generation_error"] = str(exc)
                st.session_state["cv_generation_status"] = "error"
                st.rerun()

        if status == "prompt_generated":
            st.markdown(
                f'''<div class="cvwiz-card" style="justify-content:flex-start;">
                  <div class="cvwiz-eyebrow">JOBSYNC • DOCUMENT STUDIO</div>
                  <div class="cvwiz-question">{html.escape(doc)} is ready to create</div>
                  <div class="cvwiz-copy">Your prompt is prepared. JobSync will generate content against the locked blueprint using your uploaded reference CV, validate the LaTeX, and keep the source here for copying to Overleaf.</div>
                  <div class="cvwiz-ready"><b>{html.escape(title)}</b><span>{html.escape(company)} · {html.escape(location)}</span></div>
                  <div class="cvwiz-blueprint">Blueprint in use · {html.escape(cv_blueprint_name)}</div>
                </div>''', unsafe_allow_html=True)
            if generation_running:
                _run_generation_inline()
            else:
                key_missing = False
                if _is_local_ai_provider(provider):
                    cfg = _local_ai_config(provider)
                    st.info(f"Local AI: {_local_ai_key(provider)} ({cfg['model']}). No API key is required. JobSync automatically installs Ollama and downloads this model on first use.")
                else:
                    key_missing = not _ai_api_key(provider)
                    if key_missing:
                        resolved_provider, _ = _resolve_ai_selection(provider)
                        setup = FREE_AI_KEY_SETUP.get(resolved_provider)
                        if setup:
                            st.warning(f"{provider} needs a free {resolved_provider} API key before it can generate — this is a one-time, no-billing step, not a JobSync limitation.")
                            st.markdown("\n".join(f"{i + 1}. {step}" for i, step in enumerate(setup["steps"])))
                            link_col, settings_col = st.columns(2, gap="small")
                            with link_col:
                                st.link_button(setup["button"], setup["url"], width="stretch")
                            with settings_col:
                                if st.button("Open Settings →", key=f"cvwiz_open_settings_{cv_cycle}", width="stretch"):
                                    go("Settings"); st.rerun()
                        else:
                            st.warning(f"{provider} isn't connected yet. Add its API key once in Settings → AI generation, and every CV/cover letter from then on will generate automatically — no more pasting a key here each time.")
                            if st.button("Open Settings →", key=f"cvwiz_open_settings_{cv_cycle}", width="stretch"):
                                go("Settings"); st.rerun()
                if st.button(f"Generate {doc} →", key=f"cvwiz_generate_{cv_cycle}", type="primary", width="stretch", disabled=key_missing):
                    st.session_state["cv_generation_running"] = True
                    st.rerun()

        elif status == "complete":
            latex_source = st.session_state.get("cv_latex_draft", "")
            base = st.session_state.get("cv_generated_base") or safe_name(title or doc) or doc.lower().replace(" ", "_")
            pdf_path = str(st.session_state.get("cv_saved_pdf") or st.session_state.get("cv_compiled_pdf") or "")
            pdf_exists = bool(pdf_path and Path(pdf_path).exists())
            st.markdown(
                f'''<div class="cvwiz-card" style="justify-content:flex-start;min-height:0;">
                  <div class="cvwiz-eyebrow">JOBSYNC • DOCUMENT READY</div>
                  <div class="cvwiz-question">{html.escape(doc)} is ready</div>
                  <div class="cvwiz-copy">AI content was tailored using the selected vacancy, your profile, and uploaded reference documents. The locked {html.escape(cv_blueprint_name)} blueprint was used for the final LaTeX source.</div>
                  <div class="cvwiz-ready"><b>{html.escape(title)}</b><span>{html.escape(company)} · {html.escape(location)}</span></div>
                  <div class="cvwiz-blueprint">Blueprint used · {html.escape(cv_blueprint_name)} · Reference CVs · {len(st.session_state.get("cv_reference_names", []))}</div>
                </div>''', unsafe_allow_html=True)

            st.markdown('<div class="cvwiz-source-label">LATEX SNIPPET · COPY TO OVERLEAF</div>', unsafe_allow_html=True)
            with st.container(key="cvwiz_latex_box"):
                st.code(latex_source or "% No LaTeX source is available.", language="latex", wrap_lines=True, height=140)
            st.markdown('<div class="cvwiz-source-help">Use the copy icon on the code frame (top-right on hover) — the full source copies even though the box is small. Then use Continue to Overleaf to sign in.</div>', unsafe_allow_html=True)

            action_col, folder_col = st.columns([1.15, 1.0], gap="small")
            with action_col:
                _render_overleaf_login_button(f"overleaf_login_{cv_cycle}")
            with folder_col:
                if st.button("📁 JobSync folder", width="stretch", key=f"cvwiz_open_folder_{cv_cycle}"):
                    if not _open_local_path(str(OUTPUT_CV if doc == "CV" else OUTPUT_CL)):
                        st.error("Could not open the JobSync folder.")

            if pdf_exists:
                st.success("PDF is saved automatically in the JobSync folder.")
                dl_col, preview_col = st.columns([1.0, 1.0], gap="small")
                with dl_col:
                    try:
                        pdf_bytes = Path(pdf_path).read_bytes()
                        st.download_button(
                            "⬇ Download PDF",
                            data=pdf_bytes,
                            file_name=Path(pdf_path).name,
                            mime="application/pdf",
                            width="stretch",
                            key=f"cvwiz_download_pdf_{cv_cycle}",
                        )
                    except Exception as exc:
                        st.warning(f"PDF download is unavailable: {exc}")
                with preview_col:
                    if st.button("👁 Preview PDF", width="stretch", key=f"cvwiz_preview_pdf_{cv_cycle}"):
                        st.session_state[f"cvwiz_pdf_preview_{cv_cycle}"] = not st.session_state.get(f"cvwiz_pdf_preview_{cv_cycle}", False)
                if st.session_state.get(f"cvwiz_pdf_preview_{cv_cycle}"):
                    _render_pdf_preview(pdf_path, f"{doc} PDF preview")
            else:
                st.markdown('<div class="cvwiz-source-label">FINAL PDF</div>', unsafe_allow_html=True)
                st.info("After compiling the copied LaTeX in Overleaf, upload the resulting PDF here. JobSync will save it automatically into the generated-document folder and make it downloadable from this page.")
                pdf_upload = st.file_uploader("Drop the compiled PDF here", type=["pdf"], key=f"cvwiz_pdf_upload_{cv_cycle}")
                if pdf_upload is not None:
                    try:
                        out_folder = OUTPUT_CV if doc == "CV" else OUTPUT_CL
                        out_folder.mkdir(parents=True, exist_ok=True)
                        pdf_target = unique_doc_path(out_folder, f"{base}_{doc}_generated", ".pdf")
                        pdf_target.write_bytes(pdf_upload.getbuffer())
                        created = datetime.now().isoformat(timespec="seconds")
                        kind = "generated_cv" if doc == "CV" else "generated_coverletter"
                        record = {
                            "name": pdf_target.name,
                            "display_name": pdf_target.stem,
                            "kind": kind,
                            "path": str(pdf_target),
                            "pdf_path": str(pdf_target),
                            "tex_path": "",
                            "company": company,
                            "job_title": title,
                            "job_url": saved_job.get("url", ""),
                            "created_at": created,
                            "updated_at": created,
                            "size_bytes": pdf_target.stat().st_size,
                        }
                        state["documents"].append(record)
                        save_state(state)
                        st.session_state["cv_saved_pdf"] = str(pdf_target)
                        notify_success(f"{doc} PDF saved automatically to JobSync: {pdf_target.name}")
                        st.rerun()
                    except Exception as exc:
                        notify_error(f"Could not save PDF: {exc}")
        else:
            error_text = str(st.session_state.get("cv_generation_error") or "Unknown error")
            resolved_provider, _ = _resolve_ai_selection(provider)
            key_related = (not _is_local_ai_provider(provider)) and (
                not _ai_api_key(provider)
                or any(token in error_text for token in ("401", "403", "API key", "api key", "PERMISSION_DENIED", "API_KEY_INVALID"))
            )
            st.markdown(
                f'''<div class="cvwiz-card">
                  <div class="cvwiz-eyebrow">JOBSYNC • GENERATION ERROR</div>
                  <div class="cvwiz-question">The document could not be completed</div>
                  <div class="cvwiz-copy">{html.escape(error_text)}</div>
                </div>''', unsafe_allow_html=True)
            if key_related:
                setup = FREE_AI_KEY_SETUP.get(resolved_provider)
                if setup:
                    st.warning(f"{provider} needs a free {resolved_provider} API key before it can generate — this is a one-time, no-billing step, not a JobSync limitation.")
                    st.markdown("\n".join(f"{i + 1}. {step}" for i, step in enumerate(setup["steps"])))
                    link_col, settings_col = st.columns(2, gap="small")
                    with link_col:
                        st.link_button(setup["button"], setup["url"], width="stretch")
                    with settings_col:
                        if st.button("Open Settings →", key=f"cvwiz_error_settings_{cv_cycle}", width="stretch"):
                            go("Settings"); st.rerun()
                else:
                    st.warning(f"{provider} needs a valid API key. Add or fix it once in Settings → AI generation.")
                    if st.button("Open Settings →", key=f"cvwiz_error_settings_{cv_cycle}", width="stretch"):
                        go("Settings"); st.rerun()
            if st.button("Try generation again", key=f"cvwiz_retry_{cv_cycle}", type="primary", width="stretch"):
                st.session_state["cv_generation_error"] = ""
                st.session_state["cv_generation_status"] = "prompt_generated"
                st.session_state["cv_generation_running"] = False
                st.rerun()

elif page == "Folders":
    render_modern_page_header("Folders")
    library_path = cv_library_location()
    sync_cv_library()

    # Two-panel folder workspace. The library is deliberately a fixed-height
    # Streamlit container so only the document list scrolls, never the page.
    st.markdown('<div class="jobsync-folder-workspace">', unsafe_allow_html=True)
    left_col, right_col = st.columns([0.38, 0.62], gap="large")

    with left_col:
        st.markdown(
            '<div class="jobsync-folder-pane jobsync-folder-upload-pane">'
            '<div class="jobsync-folder-pane-kicker">01 • ADD</div>'
            '<div class="jobsync-folder-pane-title">Upload a CV</div>'
            '<div class="jobsync-folder-pane-copy">Add a PDF, DOCX, LaTeX or TXT. JobSync stores it in your local CV folder automatically.</div>'
            '</div>',
            unsafe_allow_html=True,
        )
        folder_upload_key = f"folder_cv_uploads_v5_{st.session_state.folder_upload_cycle}"
        folder_upload = st.file_uploader(
            "Choose CV files",
            type=["pdf", "docx", "tex", "txt"],
            accept_multiple_files=True,
            key=folder_upload_key,
            label_visibility="collapsed",
        )
        st.markdown('<div class="jobsync-folder-upload-hint">SELECTED FILES ARE READY TO ADD</div>', unsafe_allow_html=True)
        if st.button("＋  Add to CV library", type="primary", width="stretch", key="folders_upload_cv_v5"):
            count = 0
            for uploaded in folder_upload or []:
                original_name = Path(uploaded.name).name
                suffix = Path(original_name).suffix or ".bin"
                stem = Path(original_name).stem
                target = unique_doc_path(UPLOAD_CV, stem, suffix)
                target.write_bytes(uploaded.getbuffer())
                try:
                    text = extract_text(target).strip()
                except Exception:
                    text = ""

                display_name = stem
                library_target = library_path / f"{safe_name(display_name)}{suffix}"
                counter = 2
                while library_target.exists():
                    library_target = library_path / f"{safe_name(display_name)}_{counter}{suffix}"
                    counter += 1
                shutil.copy2(target, library_target)
                created_at = datetime.now().isoformat(timespec="seconds")
                state.setdefault("documents", []).append({
                    "name": target.name,
                    "display_name": display_name,
                    "job_title": "",
                    "kind": "uploaded_cv",
                    "reference_type": "cv",
                    "path": str(target),
                    "library_path": str(library_target),
                    "pdf_path": "",
                    "pdf_text_path": "",
                    "text_chars": len(text),
                    "created_at": created_at,
                    "date_applied": "",
                })
                count += 1
            if count:
                create_cv_library_backup("upload")
                notify_success(f"Added {count} CV(s) to your library.")
                st.session_state.folder_upload_cycle += 1
                st.rerun()
            else:
                notify_error("Choose at least one CV file first.")

        st.markdown(
            '<div class="jobsync-folder-upload-note">'
            '<span>LOCAL STORAGE</span>'
            '<p>Your files stay in JobSync. Nothing is uploaded to an external service by this folder.</p>'
            '</div>',
            unsafe_allow_html=True,
        )

    with right_col:
        current_docs = sorted(
            cv_document_records(),
            key=lambda d: str(d.get("created_at") or ""),
            reverse=True,
        )
        st.markdown(
            f'<div class="jobsync-folder-pane jobsync-folder-library-pane">'
            f'<div><div class="jobsync-folder-pane-kicker">02 • LIBRARY</div>'
            f'<div class="jobsync-folder-pane-title">Saved CVs <span>{len(current_docs)}</span></div>'
            f'<div class="jobsync-folder-pane-copy">Open, download or delete a CV from the ⋯ menu beside its filename. Scroll inside this library when there are more files.</div></div>'
            f'<div class="jobsync-folder-live">● LOCAL</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # Only this container scrolls. The rest of the page remains fixed.
        with st.container(height=610, border=True):
            if not current_docs:
                st.markdown(
                    '<div class="jobsync-folder-empty jobsync-folder-empty-large">'
                    '<div class="jobsync-folder-empty-icon">CV</div>'
                    '<div><b>Your library is empty</b><span>Upload your first CV from the panel on the left.</span></div>'
                    '</div>',
                    unsafe_allow_html=True,
                )
            else:
                for idx, doc in enumerate(current_docs):
                    inferred_position, inferred_date = cv_position_and_date(doc)
                    position_value = str(doc.get("job_title") or inferred_position or "General CV")
                    name_value = str(doc.get("display_name") or Path(str(doc.get("path") or "CV")).stem)
                    kind_label = cv_kind_label(doc)
                    file_path = Path(str(doc.get("library_path") or doc.get("path") or ""))
                    if not file_path.exists():
                        fallback = Path(str(doc.get("path") or ""))
                        if fallback.exists():
                            file_path = fallback
                    suffix = file_path.suffix.upper().lstrip(".") if file_path.suffix else "FILE"
                    date_text = inferred_date or str(doc.get("created_at") or "")[:10]

                    st.markdown('<div class="jobsync-folder-item">', unsafe_allow_html=True)
                    header_col, action_col = st.columns([0.92, 0.08], gap="small")
                    with header_col:
                        st.markdown(
                            f'<div class="jobsync-folder-item-head">'
                            f'<div class="jobsync-folder-file-icon">{html.escape(suffix[:4])}</div>'
                            f'<div class="jobsync-folder-item-main">'
                            f'<div class="jobsync-folder-item-name">{html.escape(name_value)}</div>'
                            f'<div class="jobsync-folder-item-meta"><span>{html.escape(position_value)}</span><i>•</i><span>{html.escape(kind_label)}</span>{f"<i>•</i><span>{html.escape(date_text)}</span>" if date_text else ""}</div>'
                            f'</div></div>',
                            unsafe_allow_html=True,
                        )
                    # Compact per-file action menu immediately beside the filename.
                    with action_col:
                        with st.popover("⋯", use_container_width=False):
                            if file_path.exists():
                                st.download_button(
                                    "↓ Download",
                                    data=file_path.read_bytes(),
                                    file_name=file_path.name,
                                    mime="application/pdf" if file_path.suffix.lower()==".pdf" else "application/octet-stream",
                                    key=f"folder_download_menu_v8_{idx}",
                                    on_click=_cv_download_clicked,
                                    args=(file_path.name,),
                                    width="stretch",
                                )
                                if st.button("📂 Folder", key=f"folder_open_menu_v8_{idx}", width="stretch"):
                                    if not _open_local_path(str(file_path.parent)):
                                        st.error("Could not open the folder.")
                                if file_path.suffix.lower() == ".pdf":
                                    if st.button("👁 View PDF", key=f"folder_view_pdf_menu_v8_{idx}", width="stretch"):
                                        st.session_state[f"cvfolder_preview_pdf_{idx}"] = not st.session_state.get(f"cvfolder_preview_pdf_{idx}", False)
                                        st.rerun()
                                if st.button("Delete", key=f"folder_delete_menu_v8_{idx}", width="stretch"):
                                    remove_document(doc)
                                    try:
                                        create_cv_library_backup("delete")
                                    except Exception:
                                        pass
                                    st.rerun()
                            else:
                                st.caption("File is no longer available.")
                    if st.session_state.get(f"cvfolder_preview_pdf_{idx}") and file_path.exists() and file_path.suffix.lower() == ".pdf":
                        _render_pdf_preview(str(file_path), f"{name_value} — PDF preview")
                    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)

elif page == "Profile":
    # v1.7.0 profile refinement: a completed identity is presented as one calm,
    # centered profile card. Editing happens in a focused modal dialog.
    profile_values = [
        profile.get("name"), profile.get("email"), profile.get("phone"),
        profile.get("city"), profile.get("field"), profile.get("industry"),
        profile.get("location"), profile.get("experience"), profile.get("language"),
        profile.get("target_titles") or "",
    ]
    completed = sum(1 for value in profile_values if str(value or "").strip() and str(value).strip().lower() != "any")
    completion_pct = int(round((completed / len(profile_values)) * 100))
    initials_source = str(profile.get("name") or profile.get("email") or "User").strip()
    initials = "".join(part[0] for part in initials_source.split()[:2]).upper() or "U"
    display_name = str(profile.get("name") or "Your name").strip()
    display_email = str(profile.get("email") or "Add your email").strip()
    display_city = str(profile.get("city") or "Location not set").strip()
    display_field = str(profile.get("field") or "Main field not set").strip()
    display_industry = str(profile.get("industry") or "Industry not set").strip()
    display_experience = str(profile.get("experience") or "Any").strip()
    display_language = str(profile.get("language") or "Any").strip()
    complete = completion_pct >= 100

    st.markdown("""<style>
      .p17-profile-only{min-height:calc(100vh - 92px);display:flex;align-items:flex-start;justify-content:center;padding:1rem 1rem 4rem;box-sizing:border-box}
      .p17-identity-card{width:min(640px,100%);border:1px solid rgba(89,211,255,.18);border-radius:28px;overflow:hidden;background:radial-gradient(circle at 50% 0%,rgba(89,211,255,.10),transparent 30%),linear-gradient(150deg,rgba(9,22,39,.98),rgba(10,11,24,.99));box-shadow:0 28px 80px rgba(0,0,0,.28),inset 0 1px 0 rgba(255,255,255,.055);animation:p17IdentityIn .55s cubic-bezier(.2,.75,.2,1) both}
      .p17-identity-top{position:relative;height:92px;background:radial-gradient(circle at 80% 10%,rgba(224,74,202,.16),transparent 28%),linear-gradient(120deg,rgba(24,62,91,.7),rgba(51,25,75,.72));display:flex;align-items:flex-start;justify-content:space-between;padding:18px 20px;box-sizing:border-box}
      .p17-identity-kicker{font-size:.52rem;font-weight:950;letter-spacing:.18em;color:#65ddff;text-transform:uppercase}.p17-identity-live{font-size:.48rem;font-weight:900;letter-spacing:.08em;color:#7cf0b6;border:1px solid rgba(74,231,164,.18);background:rgba(48,205,133,.055);padding:6px 9px;border-radius:999px}.p17-identity-live i{display:inline-block;width:6px;height:6px;border-radius:50%;background:#4be6a0;box-shadow:0 0 10px rgba(75,230,160,.8);margin-right:5px;animation:p17IdentityPulse 1.7s ease-in-out infinite}
      .p17-identity-body{padding:0 28px 28px;text-align:center}.p17-identity-avatar-wrap{position:relative;width:112px;height:112px;margin:-55px auto 13px}.p17-identity-avatar{width:112px;height:112px;border-radius:34px;display:grid;place-items:center;font-size:2rem;font-weight:950;color:white;background:linear-gradient(145deg,#32d8ff,#7658ff 55%,#ef58b4);border:6px solid #091522;box-shadow:0 0 0 1px rgba(101,220,255,.52),0 0 46px rgba(91,91,255,.26);animation:p17IdentityFloat 5s ease-in-out infinite}.p17-identity-dot{position:absolute;right:1px;bottom:2px;width:16px;height:16px;border-radius:50%;background:#42e7a0;border:4px solid #091522;box-shadow:0 0 15px rgba(66,231,160,.72)}
      .p17-identity-status{display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border-radius:999px;color:#99f3c7;background:rgba(62,226,158,.055);border:1px solid rgba(62,226,158,.16);font-size:.49rem;font-weight:950;letter-spacing:.08em}.p17-identity-status b{width:6px;height:6px;border-radius:50%;background:#4be6a0;box-shadow:0 0 9px rgba(75,230,160,.7)}
      .p17-identity-name{margin:10px 0 3px;font-size:1.8rem;line-height:1.05;font-weight:950;letter-spacing:-.045em;color:#f4f7fc}.p17-identity-email{font-size:.72rem;color:#8291a5}.p17-identity-role{margin-top:8px;font-size:.78rem;color:#b7c6d5;font-weight:750}.p17-identity-meta{display:flex;flex-wrap:wrap;justify-content:center;gap:7px;margin:18px auto 0;max-width:520px}.p17-identity-chip{padding:8px 11px;border-radius:12px;border:1px solid rgba(255,255,255,.065);background:rgba(255,255,255,.025);color:#aab9c9;font-size:.57rem}.p17-identity-chip strong{color:#edf4fa;font-weight:850}.p17-identity-actions{display:flex;justify-content:center;margin-top:22px}.p17-identity-actions .stButton{width:min(360px,100%)}.p17-identity-actions .stButton>button{height:46px!important;border-radius:14px!important;font-size:.68rem!important;font-weight:900!important;background:linear-gradient(100deg,rgba(47,194,231,.9),rgba(115,76,231,.95),rgba(205,61,177,.92))!important;color:#fff!important;border:1px solid rgba(103,224,255,.38)!important;box-shadow:0 10px 28px rgba(77,86,220,.16)!important}.p17-identity-hint{margin-top:11px;color:#5f7085;font-size:.52rem}.p17-identity-progress{margin:20px auto 0;max-width:420px}.p17-identity-progress-head{display:flex;justify-content:space-between;color:#718196;font-size:.48rem;font-weight:900;letter-spacing:.08em}.p17-identity-progress-head b{color:#eaf3f8}.p17-identity-track{height:6px;border-radius:99px;background:rgba(255,255,255,.06);overflow:hidden;margin-top:6px}.p17-identity-track i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,#38d9ff,#7c5cff,#eb5ab5);box-shadow:0 0 15px rgba(88,128,255,.3)}
      .st-key-p17_edit_top{max-width:640px;margin:0 auto 8px;display:flex;justify-content:flex-end;}
      .st-key-p17_edit_top .stButton>button{height:34px!important;min-height:34px!important;padding:0 14px!important;border-radius:10px!important;font-size:.64rem!important;font-weight:850!important;background:rgba(255,255,255,.07)!important;color:#eef2f7!important;border:1px solid rgba(255,255,255,.14)!important;box-shadow:none!important;white-space:nowrap!important;}
      .st-key-p17_edit_top .stButton>button:hover{background:rgba(255,255,255,.14)!important;}
      @keyframes p17IdentityIn{from{opacity:0;transform:translateY(10px) scale(.985)}to{opacity:1;transform:none}}@keyframes p17IdentityFloat{50%{transform:translateY(-2px)}}@keyframes p17IdentityPulse{50%{opacity:.35;transform:scale(.72)}}
      @media(max-width:700px){.p17-profile-only{padding:2rem .5rem 3rem}.p17-identity-body{padding:0 18px 22px}.p17-identity-name{font-size:1.5rem}.p17-identity-top{height:82px;padding:15px}.p17-identity-avatar,.p17-identity-avatar-wrap{width:96px;height:96px}.p17-identity-avatar-wrap{margin-top:-47px}.p17-identity-avatar{font-size:1.7rem}.p17-identity-chip{font-size:.54rem}}
      @media(prefers-reduced-motion:reduce){.p17-identity-card,.p17-identity-avatar,.p17-identity-live i{animation:none!important}}
    </style>""", unsafe_allow_html=True)

    st.markdown(f'''<section class="p17-profile-only"><div class="p17-identity-card">
      <div class="p17-identity-top"><span class="p17-identity-kicker">JOBSYNC · PROFILE</span><span class="p17-identity-live"><i></i>{"PROFILE COMPLETE" if complete else "PROFILE ACTIVE"}</span></div>''', unsafe_allow_html=True)
    with st.container(key="p17_edit_top"):
        edit_profile = st.button("✎ Edit profile", key="p17_edit_profile_top")
    st.markdown(f'''<div class="p17-identity-body">
        <div class="p17-identity-avatar-wrap"><div class="p17-identity-avatar">{html.escape(initials[:2])}</div><span class="p17-identity-dot"></span></div>
        <div class="p17-identity-status"><b></b>{"IDENTITY READY" if complete else "IDENTITY IN PROGRESS"}</div>
        <div class="p17-identity-name">{html.escape(display_name)}</div>
        <div class="p17-identity-email">{html.escape(display_email)}</div>
        <div class="p17-identity-role">{html.escape(display_field)}</div>
        <div class="p17-identity-meta"><span class="p17-identity-chip">⌖ <strong>{html.escape(display_city)}</strong></span><span class="p17-identity-chip">▦ <strong>{html.escape(display_industry)}</strong></span><span class="p17-identity-chip">◉ <strong>{html.escape(display_language)}</strong></span><span class="p17-identity-chip">◌ <strong>{html.escape(display_experience)}</strong></span></div>
        <div class="p17-identity-progress"><div class="p17-identity-progress-head"><span>PROFILE READINESS</span><b>{completion_pct}%</b></div><div class="p17-identity-track"><i style="width:{completion_pct}%"></i></div></div>
        <div class="p17-identity-hint">Edit any profile signal from one focused window. Your saved details power search matching and documents.</div>
      </div>
    </div></section>''', unsafe_allow_html=True)

    @st.dialog("Edit your profile", width="large")
    def _p17_full_profile_dialog():
        st.caption("Update your identity and search signals. Nothing else in your workspace is changed.")
        with st.form("p17_full_profile_form", clear_on_submit=False):
            st.markdown("**Personal details**")
            a,b=st.columns(2)
            with a:
                p_name=st.text_input("Full name", profile.get("name", ""), placeholder="Your full name")
                p_email=st.text_input("Email", profile.get("email", ""), placeholder="you@example.com")
                p_phone=st.text_input("Phone", profile.get("phone", ""), placeholder="+49 …")
                p_city=st.text_input("Home city", profile.get("city", ""), placeholder="Hannover")
            with b:
                p_industry=st.text_input("Industry", profile.get("industry", ""), placeholder="Automotive, Manufacturing …")
                p_field=st.text_input("Main field", profile.get("field", ""), placeholder="Mechanical Engineer")
                p_location=st.text_input("Search location", profile.get("location", profile.get("city", "")), placeholder="Germany, Hannover, Remote …")
                raw_titles = profile.get("target_titles") or []
                saved_titles = [str(x).strip() for x in raw_titles if str(x).strip()] if isinstance(raw_titles, list) else [x.strip() for x in re.split(r"[,;|]", str(raw_titles)) if x.strip()]
                titles_text=st.text_input("Target job titles", ", ".join(saved_titles), placeholder="Mechanical Engineer, Design Engineer")
            st.markdown("**Matching signals**")
            c,d=st.columns(2)
            exp_options=["Any","Internship","Entry level","Associate","Mid-Senior level","Director"]
            lang_options=["English","German","French","Spanish","Italian","Dutch","Any"]
            cur_exp=profile.get("experience","Any") if profile.get("experience","Any") in exp_options else "Any"
            cur_lang=profile.get("language","Any") if profile.get("language","Any") in lang_options else "Any"
            with c: p_exp=st.selectbox("Experience level", exp_options, index=exp_options.index(cur_exp))
            with d: p_lang=st.selectbox("Required job-search language", lang_options, index=lang_options.index(cur_lang))
            save=st.form_submit_button("✓ Save profile", type="primary", use_container_width=True)
        if save:
            profile.update({"name":p_name.strip(),"email":p_email.strip(),"phone":p_phone.strip(),"city":p_city.strip(),"industry":p_industry.strip(),"field":p_field.strip(),"location":p_location.strip(),"target_titles":[x.strip() for x in titles_text.split(",") if x.strip()],"experience":p_exp,"language":p_lang})
            save_state(state)
            st.rerun()

    if edit_profile:
        _p17_full_profile_dialog()

elif page == "Settings":
    render_modern_page_header("Settings")
    st.markdown('''<style>
      .settings-shell{max-width:1000px;margin:0 auto;}
      .settings-tabpanel{padding-top:6px; animation: jobsync-home-fade .4s cubic-bezier(.22,1,.36,1) both;}
      .stTabs [data-baseweb="tab-list"]{ gap:4px !important; }
      .stTabs [data-baseweb="tab"]{
        border-radius:12px 12px 0 0 !important; font-weight:700 !important; font-size:.78rem !important;
        padding:9px 14px !important; transition: background .2s ease, color .2s ease !important;
      }
      .stTabs [data-baseweb="tab"]:hover{ background:rgba(110,90,255,.12) !important; }
      .stTabs [aria-selected="true"]{ background:rgba(110,90,255,.16) !important; }
      .settings-card-head{display:flex; align-items:center; gap:11px; margin-bottom:10px;}
      .settings-icon{
        width:34px; height:34px; flex:0 0 34px; border-radius:11px; display:grid; place-items:center;
        font-size:1rem; background:linear-gradient(135deg,rgba(120,180,255,.35),rgba(190,130,255,.28));
        box-shadow:inset 0 1px 0 rgba(255,255,255,.25);
      }
      .settings-icon.danger{ background:linear-gradient(135deg,rgba(255,110,110,.4),rgba(255,60,60,.22)); }
      @media(prefers-reduced-motion:reduce){ .settings-tabpanel{ animation:none !important; } }
    </style>''', unsafe_allow_html=True)
    st.markdown('<div class="settings-shell">', unsafe_allow_html=True)

    tab_ai, tab_account, tab_jobs, tab_linkedin, tab_monitor, tab_oauth, tab_updates, tab_danger = st.tabs([
        "🤖 AI generation", "🔐 Account", "🔎 Job sources", "in LinkedIn",
        "🔔 Monitoring", "✉ Gmail OAuth", "⬆ Updates", "⚠ Danger zone",
    ])

    with tab_ai:
        st.markdown('<div class="settings-tabpanel">', unsafe_allow_html=True)
        st.markdown('<div class="settings-card-head"><div class="settings-icon">🤖</div><div class="section-title">AI generation</div></div>', unsafe_allow_html=True)
        st.info("CV and cover-letter generation uses JobSync's built-in local AI (Qwen3 14B) automatically — nothing to connect, no API key, no account. It downloads once (about 9.3 GB) the first time you generate a document.")

        with st.expander("Advanced: connect an online AI instead (optional)"):
            st.caption("Only needed if you want to use an online model instead of the local one. Not required for normal use.")
            key_col1, key_col2 = st.columns(2)
            with key_col1:
                with st.container(border=True):
                    st.markdown("**Get your free Gemini key (about 30 seconds)**")
                    st.markdown(
                        "1. Click **Get free Gemini key** below — it opens Google AI Studio in a new tab.\n"
                        "2. Sign in with any Google account (no credit card, no billing).\n"
                        "3. Click **Create API key**, then the copy icon next to the new key.\n"
                        "4. Come back to this tab, paste it into the field below, and click **Save settings**."
                    )
                    st.link_button("Get free Gemini key ↗", "https://aistudio.google.com/apikey", width="stretch")
                gemini_key = st.text_input("Gemini API key (free)", value=os.getenv("GEMINI_API_KEY", ""), type="password", help="Free at aistudio.google.com/apikey — no billing required.")
            with key_col2:
                with st.container(border=True):
                    st.markdown("**Get your free Groq key (about 30 seconds)**")
                    st.markdown(
                        "1. Click **Get free Groq key** below — it opens the Groq console in a new tab.\n"
                        "2. Sign in with Google, GitHub, or email (no credit card).\n"
                        "3. Click **Create API Key**, then copy it.\n"
                        "4. Come back to this tab, paste it into the field below, and click **Save settings**."
                    )
                    st.link_button("Get free Groq key ↗", "https://console.groq.com/keys", width="stretch")
                groq_key = st.text_input("Groq API key (free)", value=os.getenv("GROQ_API_KEY", ""), type="password", help="Free at console.groq.com/keys — very fast, high free-tier limits, good fallback when Gemini is rate-limited.")

            ai_col1, ai_col2 = st.columns(2)
            with ai_col1:
                openai_key = st.text_input("OpenAI API key (paid)", value=os.getenv("OPENAI_API_KEY", ""), type="password", help="From platform.openai.com/api-keys — needs billing enabled.")
            with ai_col2:
                anthropic_key = st.text_input("Anthropic API key (paid)", value=os.getenv("ANTHROPIC_API_KEY", ""), type="password", help="From console.anthropic.com/settings/keys — needs billing enabled.")
            st.caption("Keys are saved locally to JobSync's own .env file on this computer only — never uploaded anywhere else. CV Studio currently always uses the local model regardless of any key saved here.")
        st.markdown('</div>', unsafe_allow_html=True)

    with tab_account:
        st.markdown('<div class="settings-tabpanel">', unsafe_allow_html=True)
        st.markdown('<div class="settings-card-head"><div class="settings-icon">🔐</div><div class="section-title">Account security</div></div>', unsafe_allow_html=True)
        account_email = str(st.session_state.get("local_user_email") or profile.get("email") or "").strip().lower()
        st.caption(f"Local account: {account_email or 'Not signed in'}")
        with st.form("change_password_form"):
            current_password = st.text_input("Current password", type="password", autocomplete="current-password")
            new_password_settings = st.text_input("New password", type="password", autocomplete="new-password")
            confirm_password_settings = st.text_input("Confirm new password", type="password", autocomplete="new-password")
            change_pw = st.form_submit_button("Change password", type="primary", width="stretch")
        if change_pw:
            if new_password_settings != confirm_password_settings:
                notify_error("The new passwords do not match.")
            else:
                try:
                    _local_change_password(account_email, current_password, new_password_settings)
                    st.session_state["_remembered_login"] = False
                    notify_success("Password changed. Sign in again if your session ends.")
                except Exception as exc:
                    notify_error(f"Could not change password: {exc}")
        if st.button("Generate a new recovery code", key="generate_recovery_code", type="secondary", width="stretch"):
            try:
                code = _ensure_recovery_code(account_email)
                if code:
                    st.session_state["_account_recovery_code"] = code
                    notify_success("New recovery code created. Save it somewhere safe.")
                else:
                    notify_success("A recovery code already exists for this account. Use the code saved when the account was created.")
            except Exception as exc:
                notify_error(f"Could not create recovery code: {exc}")
        if st.session_state.get("_account_recovery_code"):
            st.code(st.session_state["_account_recovery_code"], language=None)
            st.caption("This code can reset the local password if you forget it.")
        st.markdown('</div>', unsafe_allow_html=True)

    with tab_jobs:
        st.markdown('<div class="settings-tabpanel">', unsafe_allow_html=True)
        st.markdown('<div class="settings-card-head"><div class="settings-icon">🔎</div><div class="section-title">Job sources</div></div>', unsafe_allow_html=True)
        saved_mode = state.get("settings", {}).get("job_search_mode", "free")
        if saved_mode not in JOB_SEARCH_MODE_LABELS:
            saved_mode = "free"
        settings_mode_label = st.selectbox(
            "Default job search method",
            options=list(JOB_SEARCH_MODES.keys()),
            index=list(JOB_SEARCH_MODES.values()).index(saved_mode),
        )
        settings_search_mode = JOB_SEARCH_MODES[settings_mode_label]
        free_sources_setting = st.multiselect(
            "Free/public sources",
            options=FREE_SOURCE_NAMES,
            default=state.get("settings", {}).get("free_sources") or FREE_SOURCE_NAMES,
        )
        ats_urls_setting_text = st.text_area(
            "Company ATS career URLs (one per line)",
            value="\n".join(state.get("settings", {}).get("ats_urls") or []),
            placeholder="https://company.wd5.myworkdayjobs.com/Careers\nhttps://boards.greenhouse.io/company",
        )
        ats_urls_setting = [x.strip() for x in ats_urls_setting_text.splitlines() if x.strip()]
        if settings_search_mode == "free":
            st.info("Free APIs & public sources selected. Searches from this installation will not use Apify.")
        configured_ids = state.get("settings", {}).get("actor_ids") or [ACTOR_CATALOG[name]["id"] for name in DEFAULT_ACTOR_NAMES]
        configured_names = [ACTOR_ID_TO_NAME.get(x, x) for x in configured_ids]
        if settings_search_mode in {"apify", "both"}:
            selected_names = st.multiselect("Default Apify Actors", options=list(ACTOR_CATALOG.keys()), default=[x for x in configured_names if x in ACTOR_CATALOG])
            for name, meta in ACTOR_CATALOG.items():
                if name in selected_names:
                    st.caption(f"{name} — {meta['pricing']} — {meta['note']}")
        else:
            selected_names = configured_names
            st.caption("Apify Actor selection is ignored while Free mode is selected.")
        apify_token = st.text_input("Apify API token", value=os.getenv("APIFY_TOKEN", ""), type="password")
        st.markdown('</div>', unsafe_allow_html=True)

    with tab_linkedin:
        st.markdown('<div class="settings-tabpanel">', unsafe_allow_html=True)
        st.markdown('<div class="settings-card-head"><div class="settings-icon">in</div><div class="section-title">LinkedIn profile & notifications</div></div>', unsafe_allow_html=True)
        linkedin_profile_url = st.text_input(
            "LinkedIn profile URL",
            value=state.get("settings", {}).get("linkedin_profile_url", ""),
            placeholder="https://www.linkedin.com/in/your-profile/",
            help="Saved locally. The notification sync uses the existing persistent LinkedIn browser session and does not use Apify.",
        )
        st.caption("Connect LinkedIn once in the browser. Your local session is then reused for notification syncs; no LinkedIn password is stored by JobSync.")
        l1, l2 = st.columns(2)
        with l1:
            if st.button("in  Connect LinkedIn", width="stretch", key="linkedin_connect"):
                try:
                    with st.spinner("Opening LinkedIn — complete the sign-in in the browser window…"):
                        connect_linkedin(login_wait_seconds=300)
                    notify_success("LinkedIn connected successfully. You can now sync notifications.")
                except Exception as exc:
                    notify_error(f"LinkedIn connection failed: {exc}")
        with l2:
            if st.button("Clear LinkedIn session", width="stretch", key="linkedin_clear"):
                import shutil
                from services.linkedin_browser import PROFILE_DIR
                try:
                    shutil.rmtree(PROFILE_DIR, ignore_errors=True)
                    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
                    state.get("settings", {}).pop("linkedin_last_sync", None)
                    state["linkedin_updates"] = []
                    save_state(state)
                    notify_success("LinkedIn browser session cleared.")
                except Exception as exc:
                    notify_error(f"Could not clear LinkedIn session: {exc}")
        st.markdown('</div>', unsafe_allow_html=True)

    with tab_monitor:
        st.markdown('<div class="settings-tabpanel">', unsafe_allow_html=True)
        st.markdown('<div class="settings-card-head"><div class="settings-icon">🔔</div><div class="section-title">Daily new-job monitoring</div></div>', unsafe_allow_html=True)
        monitor_enabled = st.checkbox(
            "Start the background job monitor automatically",
            value=bool(state.get("settings", {}).get("live_monitor_enabled", True)),
            help="When enabled, JobSync checks your selected job sources once every 24 hours, detects jobs it has not seen before, updates the New Jobs list, and shows a Windows desktop notification."
        )
        st.info("The monitor runs once every 24 hours. The first successful check creates a baseline and does not send a flood of notifications. Later checks notify only for newly detected jobs.")
        manual_col1, manual_col2 = st.columns([1, 3])
        with manual_col1:
            if st.button("▶ Run monitor now", key="run_monitor_now", width="stretch"):
                try:
                    from services.job_monitor import _monitor_once
                    ok = _monitor_once()
                    refresh_state()
                    if ok:
                        notify_success("Live monitor check completed. See the monitor status below.")
                    else:
                        notify_error("Monitor did not run. Check your profile/search settings or data/monitor.log.")
                except Exception as exc:
                    notify_error(f"Monitor check failed: {exc}")
                st.rerun()
        with manual_col2:
            st.caption("The background monitor checks immediately when Windows starts, then every 24 hours. It follows your most recent New Search settings, including selected free sources and ATS URLs.")
        last_check = state.get("settings", {}).get("monitor_last_check", "")
        last_new = state.get("settings", {}).get("monitor_last_new_count", 0)
        last_results = state.get("settings", {}).get("monitor_last_result_count", 0)
        last_error = state.get("settings", {}).get("monitor_last_error", "")
        if last_check:
            st.caption(f"Last monitor check: {last_check} · Results checked: {last_results} · New jobs detected: {last_new}")
        if last_error:
            st.error(f"Last monitor error: {last_error}")
        st.markdown('</div>', unsafe_allow_html=True)

    with tab_oauth:
        st.markdown('<div class="settings-tabpanel">', unsafe_allow_html=True)
        st.markdown('<div class="settings-card-head"><div class="settings-icon">✉</div><div class="section-title">Google OAuth setup (for Gmail sync)</div></div>', unsafe_allow_html=True)
        with st.expander("Configure Google OAuth application credentials", expanded=False):
            st.caption(
                "Configure this once for this JobSync installation. You only need to complete this "
                "after enabling Gmail sync — you will be taken through Google's own login next."
            )
            existing_client_id = ""
            existing_client_secret = ""
            if OAUTH_CONFIG_FILE.exists():
                try:
                    oauth_data = json.loads(OAUTH_CONFIG_FILE.read_text(encoding="utf-8"))
                    installed = oauth_data.get("installed", oauth_data.get("web", {}))
                    existing_client_id = str(installed.get("client_id", "")).strip()
                    existing_client_secret = str(installed.get("client_secret", "")).strip()
                except Exception:
                    pass
            oauth_client_id = st.text_input(
                "Google OAuth Client ID",
                value=existing_client_id or os.getenv("GOOGLE_OAUTH_CLIENT_ID", ""),
                help="Application-level Google OAuth client ID. Desktop app client is recommended for local JobSync."
            )
            oauth_client_secret = st.text_input(
                "Google OAuth Client Secret",
                value=existing_client_secret or os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", ""),
                type="password",
                help="Application-level Google OAuth client secret."
            )
            if st.button("Save Google OAuth", key="save_google_oauth", type="secondary"):
                if not oauth_client_id.strip() or not oauth_client_secret.strip():
                    notify_error("Enter both the Google OAuth Client ID and Client Secret.")
                else:
                    OAUTH_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
                    payload = {
                        "installed": {
                            "client_id": oauth_client_id.strip(),
                            "client_secret": oauth_client_secret.strip(),
                            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                            "token_uri": "https://oauth2.googleapis.com/token",
                            "redirect_uris": ["http://localhost"],
                        }
                    }
                    OAUTH_CONFIG_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                    notify_success("Google OAuth application configuration saved locally.")
                    st.rerun()
            st.caption(f"Configuration file: {OAUTH_CONFIG_FILE}")
        st.markdown('</div>', unsafe_allow_html=True)

    with tab_updates:
        st.markdown('<div class="settings-tabpanel">', unsafe_allow_html=True)
        st.markdown('<div class="settings-card-head"><div class="settings-icon">⬆</div><div class="section-title">Software updates</div></div>', unsafe_allow_html=True)
        st.caption("Updates are checked when you press the button. If a newer release is available, JobSync downloads the installer, closes the current app, and opens the visible installer. Your local data stays inside the JobSync folder.")
        update_col1, update_col2 = st.columns([1, 2])
        with update_col1:
            if st.button("Check GitHub for updates", key="manual_github_update", type="secondary", width="stretch"):
                import subprocess
                updater_root = _find_github_updater_root()
                if updater_root is None:
                    expected = PACKAGE_DIR / "github"
                    notify_error(f"GitHub updater files not found. Expected: {expected}")
                else:
                    updater_path = updater_root / "updater.ps1"
                    cfg_path = updater_root / "update-config.json"
                    try:
                        completed = subprocess.run(
                            ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(updater_path), "-InstallDir", str(PACKAGE_DIR), "-ConfigPath", str(cfg_path)],
                            cwd=str(PACKAGE_DIR),
                            capture_output=True,
                            text=True,
                            timeout=120,
                        )
                        output_text = (completed.stdout or completed.stderr or "").strip()
                        result_path = _github_update_state_path()
                        result = {}
                        try:
                            if result_path.exists():
                                result = json.loads(result_path.read_text(encoding="utf-8-sig"))
                        except Exception:
                            result = {}

                        if completed.returncode != 0 or result.get("error"):
                            error_text = str(result.get("error") or output_text or "unknown error").strip()
                            notify_error(f"Update check failed: {error_text[-1000:]}")
                        elif result.get("downloaded"):
                            notify_success(f"Update v{result.get('latest_version')} is ready. The installer will open automatically.")
                        elif result.get("up_to_date"):
                            notify_success(f"You are up to date (v{result.get('current_version', APP_VERSION)}).")
                        else:
                            notify_success(output_text[-1000:] or "Update check completed.")
                    except Exception as exc:
                        notify_error(f"Could not run the updater: {exc}")
        with update_col2:
            updater_root = _find_github_updater_root()
            result_path = _github_update_state_path() if updater_root else None
            if result_path and result_path.exists():
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    checked = result.get("checked_at", "")
                    latest = result.get("latest_version", "")
                    dl = result.get("download_path") or ""
                    if result.get("downloaded"):
                        st.caption(f"Latest: v{latest} · installer started: {dl} · checked: {checked}")
                    elif result.get("error"):
                        st.caption(f"Last check failed: {result.get('error')} · checked: {checked}")
                    else:
                        st.caption(f"Latest checked: v{latest} · checked: {checked}")
                except Exception:
                    st.caption("No successful update check recorded yet.")
            else:
                st.caption("No manual GitHub update check has been run yet.")
        st.markdown('</div>', unsafe_allow_html=True)

    st.write("")
    st.markdown('<div class="card">', unsafe_allow_html=True)
    if st.button("Save settings", type="primary", width="stretch"):
        settings = state.setdefault("settings", {})
        settings["linkedin_profile_url"] = linkedin_profile_url.strip()
        settings["actor_ids"] = [ACTOR_CATALOG[name]["id"] for name in selected_names if name in ACTOR_CATALOG]
        settings["job_search_mode"] = settings_search_mode
        settings["free_sources"] = free_sources_setting
        settings["ats_urls"] = ats_urls_setting
        settings["live_monitor_enabled"] = bool(monitor_enabled)
        settings["monitor_interval_hours"] = 24
        content = "\n".join([
            f"APIFY_TOKEN={apify_token}",
            f"GEMINI_API_KEY={gemini_key.strip()}",
            f"GROQ_API_KEY={groq_key.strip()}",
            f"OPENAI_API_KEY={openai_key.strip()}",
            f"ANTHROPIC_API_KEY={anthropic_key.strip()}",
            "",
        ])
        ENV_FILE.write_text(content, encoding="utf-8")
        load_dotenv(ENV_FILE, override=True)
        # Session-only keys pasted directly in CV Studio (the old per-generation
        # flow) are superseded once a key is saved here — drop them so
        # _ai_api_key() always prefers the persisted, one-time value.
        for _provider_name in ("Gemini", "Groq", "ChatGPT", "Claude"):
            st.session_state.pop(f"cv_ai_key_{_provider_name}", None)
        save_state(state)
        notify_success("Settings saved locally.")
        st.rerun()

    st.caption("CV and cover-letter generation uses JobSync's local AI by default. The AI generation tab above is only needed for the optional online fallback.")
    st.caption(f"Generated CV folder: {OUTPUT_CV}")
    st.caption(f"Local AI model storage: {OLLAMA_MODELS_DIR}")
    st.caption(f"Generated cover-letter folder: {OUTPUT_CL}")
    st.caption(f"Excel tracker: {TRACKER}")
    st.markdown('</div>', unsafe_allow_html=True)

    with tab_danger:
        st.markdown('<div class="settings-tabpanel">', unsafe_allow_html=True)
        st.markdown('<div class="settings-card-head"><div class="settings-icon danger">⚠</div><div class="section-title">Master reset</div></div>', unsafe_allow_html=True)
        st.caption(
            "Completely clear JobSync's user data and return the workspace to a clean state."
        )
        if st.button(
            "Delete everything and reset JobSync",
            type="primary",
            width="stretch",
            key="master_reset_button",
        ):
            confirm_master_reset()
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)  # close .settings-shell


# Custom sections pages.
if page in {x["name"] for x in custom_sections()}:
    item = next(x for x in custom_sections() if x["name"] == page)
    st.markdown(
        f'<div class="mh-page-hero"><div class="mh-page-kicker">JOBSYNC • CUSTOM SECTION</div>'
        f'<div class="mh-page-title">{html.escape(item["name"])}</div>'
        f'<div class="mh-page-copy">{html.escape(item.get("description") or "Custom workspace section created by an administrator.")}</div></div>',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="card"><div class="section-title">Ready for your workflow</div><div class="muted">This section has been added by an administrator and is ready to be connected to a future JobSync feature.</div></div>', unsafe_allow_html=True)
