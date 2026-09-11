from __future__ import annotations

import html
import inspect
import json
import os
import re
import shutil
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus

import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

from services.cv_engine import (
    build_reference_context,
    extract_text,
    build_external_ai_prompt,
    extract_latex_code,
)
from services.excel_export import export_applied_jobs_xlsx
from services.jobs import ACTOR_CATALOG, ACTOR_ID_TO_NAME, DEFAULT_ACTOR_NAMES, search_jobs
from services.storage import DEFAULT_STATE, load_state, save_state
from services.gmail import get_gmail_service, disconnect_gmail, sync_gmail
from services.auth import account_email, account_exists, create_account, reset_password, verify_login, update_account_email
from services.avatar import avatar_html
from services.linkedin_browser import sync_linkedin_notifications, connect_linkedin
from services.free_job_sources import FREE_SOURCE_NAMES
from services.presence import heartbeat_presence, list_online_users, remove_presence
from services.roles import ROLE_ACCESS, ROLE_OPTIONS, ensure_user, get_user_role, list_users, normalize_role, set_user_role, touch_user, update_user, remove_user, is_user_blocked

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
            match = re.search(r"(?<!\\d)(\\d+\\.\\d+(?:\\.\\d+)?)(?!\\d)", str(value))
            if match:
                return match.group(1)
        except Exception:
            pass
    return "0.0.0"

APP_VERSION = _read_app_version()

# Resolve the project from the actual app.py location. JOBSYNC_ROOT is accepted
# only when it points back to this exact application, preventing stale startup
# variables from redirecting the app to an older installation.
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
    page_icon="💼",
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
    .block-container { padding-top:0.35rem !important; padding-bottom:3rem; max-width:1500px; }

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
    button[aria-label="Open sidebar"] {
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

    /* Global text */
    .page-title, .section-title, .hero h1, .metric-value,
    .job-title, .profile-value { color:var(--jf-text) !important; }
    .page-subtitle, .quick-note, .muted, .metric-title, .metric-note,
    .brand-sub, .job-company, .profile-label { color:var(--jf-muted) !important; }

    .home-center-brand { text-align:center; padding:3.2rem 1rem 2.4rem; margin:2rem auto 2.2rem; max-width:900px; }
    .home-center-kicker { color:#ff7d84; font-size:.68rem; font-weight:900; letter-spacing:.2em; text-transform:uppercase; }
    .home-center-title { font-size:clamp(3rem,8vw,6rem); font-weight:950; letter-spacing:-.075em; line-height:.95; margin:.35rem 0 .8rem; background:linear-gradient(90deg,#f8fafc 0%,#ff6971 55%,#77e6a2 100%); -webkit-background-clip:text; background-clip:text; color:transparent; }
    .home-center-copy { color:#a6b0bd; font-size:1rem; line-height:1.7; max-width:780px; margin:0 auto; }

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



    /* ================= CV STUDIO INTERACTION LAYER — VISUAL ONLY ================= */
    .cv-hero {
        position:relative; overflow:hidden; padding:1.35rem 1.45rem; margin:.1rem 0 1rem;
        border:1px solid rgba(255,255,255,.09); border-radius:22px;
        background:radial-gradient(circle at 90% 20%, rgba(255,77,91,.14), transparent 28%), linear-gradient(135deg, rgba(18,22,29,.95), rgba(10,12,16,.9));
        box-shadow:0 22px 60px rgba(0,0,0,.28);
    }
    .cv-hero:after { content:""; position:absolute; width:260px; height:260px; right:-90px; top:-120px; border-radius:50%; border:1px solid rgba(255,77,91,.18); box-shadow:0 0 0 35px rgba(255,77,91,.025),0 0 0 70px rgba(255,77,91,.018); pointer-events:none; }
    .cv-kicker { color:#ff7882; font-size:.68rem; font-weight:850; letter-spacing:.18em; margin-bottom:.35rem; }
    .cv-title-row { display:flex; align-items:center; gap:.85rem; }
    .cv-orb,.cv-command-avatar,.cv-empty-orb,.cv-upload-orb { display:flex; align-items:center; justify-content:center; flex:0 0 auto; border-radius:50%; font-weight:900; color:#fff; background:linear-gradient(135deg,#ff4d5b,#ff825d); box-shadow:0 0 0 5px rgba(255,77,91,.08),0 10px 26px rgba(255,77,91,.18); }
    .cv-orb { width:48px; height:48px; font-size:.85rem; }
    .cv-page-title { color:#f8fafc; font-size:2rem; line-height:1.05; font-weight:900; letter-spacing:-.035em; }
    .cv-page-subtitle { color:#9da8b7; margin-top:.25rem; font-size:.92rem; }
    .cv-hero-copy { margin:.85rem 0 0 3.35rem; color:#bbc3ce; font-size:.86rem; line-height:1.5; max-width:850px; }
    .cv-hero-badge { position:absolute; right:1rem; bottom:1rem; z-index:2; color:#9da8b7; border:1px solid rgba(255,255,255,.09); background:rgba(0,0,0,.2); border-radius:999px; padding:.34rem .62rem; font-size:.64rem; font-weight:800; letter-spacing:.08em; }
    .cv-live-dot { width:7px; height:7px; display:inline-block; border-radius:50%; background:#39e58c; box-shadow:0 0 0 5px rgba(57,229,140,.07); margin-right:6px; vertical-align:1px; }
    .cv-stepper { display:grid; grid-template-columns:repeat(4,1fr); gap:.6rem; margin:.15rem 0 1.15rem; }
    .cv-step { min-height:65px; display:flex; align-items:center; gap:.65rem; padding:.7rem .8rem; border:1px solid rgba(255,255,255,.065); border-radius:15px; background:rgba(15,18,23,.7); transition:all .18s ease; }
    .cv-step:hover { transform:translateY(-2px); border-color:rgba(255,255,255,.14); background:rgba(20,24,31,.9); }
    .cv-step.active { border-color:rgba(255,77,91,.32); box-shadow:inset 0 1px 0 rgba(255,77,91,.4),0 10px 25px rgba(0,0,0,.14); }
    .cv-step > span { width:31px; height:31px; border-radius:10px; display:flex; align-items:center; justify-content:center; color:#f5f7fa; background:#1c2129; font-size:.68rem; font-weight:850; }
    .cv-step.active > span { background:linear-gradient(135deg,#ff4d5b,#ff7a59); }
    .cv-step b { display:block; color:#f2f5f8; font-size:.82rem; }
    .cv-step small { display:block; color:#7e8998; font-size:.68rem; margin-top:.15rem; }
    .cv-assistant-strip { margin:-.25rem 0 .85rem; }
    .cv-assistant-card { display:flex; align-items:center; gap:.7rem; padding:.72rem .85rem; border:1px solid rgba(255,255,255,.07); border-radius:15px; background:linear-gradient(100deg,rgba(18,22,28,.88),rgba(14,17,22,.65)); transition:.18s ease; }
    .cv-assistant-card:hover { transform:translateY(-2px); border-color:rgba(255,77,91,.28); box-shadow:0 12px 30px rgba(0,0,0,.2); }
    .cv-assistant-icon { width:30px; height:30px; display:flex; align-items:center; justify-content:center; border-radius:9px; color:#fff; background:rgba(255,77,91,.16); }
    .cv-assistant-card b { display:block; color:#f7f9fc; font-size:.82rem; }
    .cv-assistant-card span { display:block; color:#8994a3; font-size:.7rem; margin-top:.12rem; }
    .cv-card-arrow { margin-left:auto; color:#748091; }
    .cv-mini-hint { color:#6f7a89; font-size:.67rem; margin:.3rem 0 0 .15rem; }
    .cv-doc-toggle-note { display:flex; align-items:center; gap:.45rem; color:#778291; font-size:.72rem; margin:-.15rem 0 .75rem; }
    .cv-pill,.cv-match-chip { display:inline-flex; align-items:center; padding:.23rem .52rem; border-radius:999px; font-size:.62rem; font-weight:850; letter-spacing:.06em; }
    .cv-pill { color:#fff; background:rgba(102,166,255,.12); border:1px solid rgba(102,166,255,.18); }
    .cv-target-card { margin:.65rem 0 .75rem; padding:1rem 1.05rem; border-radius:17px; border:1px solid rgba(255,255,255,.075); background:linear-gradient(135deg,rgba(19,23,30,.9),rgba(12,15,20,.75)); transition:.18s ease; }
    .cv-target-card:hover { border-color:rgba(255,255,255,.13); box-shadow:0 15px 35px rgba(0,0,0,.18); }
    .cv-target-top { display:flex; align-items:center; justify-content:space-between; gap:1rem; }
    .cv-label { color:#737f8f; font-size:.62rem; letter-spacing:.13em; font-weight:850; }
    .cv-match-chip { color:#8ef0b6; background:rgba(57,229,140,.08); border:1px solid rgba(57,229,140,.14); }
    .cv-target-title { color:#f4f7fa; font-size:1.15rem; font-weight:850; margin-top:.48rem; }
    .cv-target-meta { color:#929dac; font-size:.78rem; margin-top:.2rem; }
    .cv-control-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:.6rem; margin:.8rem 0 1rem; }
    .cv-info-tile { padding:.78rem .82rem; border:1px solid rgba(255,255,255,.06); background:#0f1318; border-radius:14px; transition:.18s ease; }
    .cv-info-tile:hover { transform:translateY(-2px); border-color:rgba(255,255,255,.12); }
    .cv-info-tile span { display:block; color:#697586; font-size:.58rem; font-weight:850; letter-spacing:.12em; }
    .cv-info-tile b { display:block; margin-top:.25rem; color:#edf1f6; font-size:.76rem; }
    .cv-info-tile small { display:block; margin-top:.12rem; color:#7e8997; font-size:.64rem; line-height:1.35; }
    .cv-command-panel { display:flex; align-items:center; gap:.72rem; padding:.78rem .85rem; margin:.7rem 0 .8rem; border-radius:16px; border:1px solid rgba(255,255,255,.07); background:linear-gradient(90deg,rgba(255,77,91,.07),rgba(18,22,28,.6)); }
    .cv-command-avatar { width:36px; height:36px; font-size:.62rem; }
    .cv-command-copy { min-width:0; }
    .cv-command-copy b { color:#f4f7fa; display:block; font-size:.78rem; }
    .cv-command-copy span { color:#7f8a98; display:block; font-size:.67rem; line-height:1.35; margin-top:.1rem; }
    .cv-command-state { margin-left:auto; white-space:nowrap; color:#7f8c99; font-size:.62rem; font-weight:850; letter-spacing:.08em; }
    .cv-section-head { display:flex; align-items:center; gap:.65rem; margin:1.1rem 0 .55rem; padding-top:.35rem; }
    .cv-section-number { width:30px; height:30px; display:flex; align-items:center; justify-content:center; border-radius:9px; background:#171c23; border:1px solid rgba(255,255,255,.07); color:#ff7b84; font-size:.68rem; font-weight:900; }
    .cv-section-head b { display:block; color:#f5f7fa; font-size:.9rem; }
    .cv-section-head span:not(.cv-section-number) { display:block; color:#737f8e; font-size:.67rem; margin-top:.1rem; }
    .cv-prompt-shell { border:1px solid rgba(255,255,255,.075); border-radius:15px; background:#0c1015; overflow:hidden; box-shadow:0 12px 30px rgba(0,0,0,.2); }
    .cv-prompt-toolbar { display:flex; align-items:center; gap:.55rem; padding:.62rem .7rem; border-bottom:1px solid rgba(255,255,255,.06); background:#11151b; }
    .cv-small-label { display:block; color:#758192; font-size:.58rem; letter-spacing:.11em; font-weight:850; }
    .cv-small-meta { display:block; color:#525e6e; font-size:.62rem; margin-top:.08rem; }
    .cv-copy-btn { margin-left:auto; border:1px solid rgba(255,255,255,.09); background:linear-gradient(135deg,#ff4d5b,#ff7a59); color:#fff; border-radius:8px; padding:6px 11px; font-weight:800; cursor:pointer; font-size:12px; }
    .cv-copy-status { color:#5fe6a0; font-size:11px; min-width:58px; }
    .cv-code-box { box-sizing:border-box; width:100%; height:105px; resize:vertical; background:#0b0f14; color:#e8edf3; border:0; outline:none; padding:10px 11px; font:11px/1.42 Consolas,monospace; }
    .cv-code-box.small { height:92px; }
    .cv-mini-action { min-height:50px; display:flex; align-items:center; justify-content:center; flex-direction:column; text-align:center; border:1px solid rgba(255,255,255,.06); border-radius:12px; background:#0f1318; color:#85909e; font-size:.62rem; font-weight:850; letter-spacing:.1em; }
    .cv-mini-action span { color:#596474; font-size:.6rem; font-weight:500; letter-spacing:0; margin-top:.08rem; }
    .cv-upload-card,.cv-empty-card { display:flex; align-items:center; gap:.75rem; padding:.85rem; border:1px dashed rgba(255,255,255,.1); border-radius:15px; background:rgba(15,18,23,.7); margin:.75rem 0; }
    .cv-upload-orb,.cv-empty-orb { width:36px; height:36px; font-size:.58rem; }
    .cv-upload-card b,.cv-empty-card b { display:block; color:#eef2f6; font-size:.78rem; }
    .cv-upload-card span,.cv-empty-card span { display:block; color:#788494; font-size:.66rem; margin-top:.13rem; line-height:1.4; }
    @media (max-width:850px) {
        .cv-stepper,.cv-control-grid { grid-template-columns:repeat(2,1fr); }
        .cv-hero-badge { display:none; }
        .cv-hero-copy { margin-left:0; }
    }

    /* Responsive right-side collaboration panel */
    .block-container { padding-right:330px !important; }
    .jobsync-presence-panel {
        position:fixed; top:.75rem; right:.75rem; width:285px;
        max-height:calc(100vh - 1.5rem); overflow:hidden; z-index:900;
        background:linear-gradient(180deg,#0d1014,#090b0e); border:1px solid #252a31; border-radius:18px;
        box-shadow:0 18px 50px rgba(0,0,0,.38); color:#f5f7fa;
    }
    .jobsync-presence-head { padding:15px 16px 12px; border-bottom:1px solid #20242a; }
    .jobsync-presence-title { font-size:.86rem; font-weight:850; letter-spacing:.04em; text-transform:uppercase; }
    .jobsync-presence-count { color:#22c55e; font-size:.72rem; margin-top:3px; }
    .jobsync-presence-list { max-height:calc(100vh - 9rem); overflow-y:auto; padding:7px 8px 10px; }
    .jobsync-presence-section { padding:8px 8px 4px; color:#717c8b; font-size:.6rem; font-weight:850; letter-spacing:.12em; text-transform:uppercase; }
    .jobsync-user-row { display:flex; align-items:center; gap:10px; padding:8px; border-radius:12px; margin-bottom:2px; border:1px solid transparent; transition:all .16s ease; }
    .jobsync-user-row:hover { background:#111418; border-color:#252c35; }
    .jobsync-user-avatar { width:38px; height:38px; border-radius:50%; flex:0 0 38px; }
    .jobsync-user-copy { min-width:0; }
    .jobsync-user-name { font-size:.8rem; font-weight:750; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .jobsync-user-meta { display:flex; align-items:center; gap:7px; flex-wrap:wrap; margin-top:2px; }
    .jobsync-user-status { font-size:.66rem; color:#8f99a7; display:flex; align-items:center; gap:5px; }
    .jobsync-online-dot { width:7px; height:7px; border-radius:50%; background:#22c55e; display:inline-block; box-shadow:0 0 0 3px rgba(34,197,94,.10); }
    .jobsync-offline-dot { width:7px; height:7px; border-radius:50%; background:#68707c; display:inline-block; }
    .jobsync-role-badge { display:inline-flex; align-items:center; padding:2px 7px; border-radius:999px; font-size:.58rem; line-height:1.25; font-weight:850; letter-spacing:.04em; text-transform:uppercase; background:rgba(255,77,91,.08); border:1px solid rgba(255,77,91,.18); color:#ff9da2 !important; }
    .jobsync-role-badge.admin { background:rgba(239,68,68,.10); border-color:rgba(239,68,68,.25); color:#ff8e96 !important; }
    .jobsync-role-badge.moderator { background:rgba(245,158,11,.10); border-color:rgba(245,158,11,.25); color:#ffc265 !important; }
    .jobsync-role-badge.member { background:rgba(34,197,94,.08); border-color:rgba(34,197,94,.20); color:#7df0a5 !important; }
    .jobsync-presence-empty { padding:16px 10px; color:#7f8996; font-size:.75rem; text-align:center; }
    .role-admin-note { border-left:3px solid #ef4444; padding:.7rem .8rem; border-radius:10px; background:rgba(239,68,68,.055); color:#b9c1cd; font-size:.78rem; margin:.6rem 0; }
    @media (max-width:1200px) {
        .block-container { padding-right:290px !important; }
        .jobsync-presence-panel { width:250px; }
    }
    @media (max-width:900px) {
        .block-container { padding-right:1rem !important; padding-left:1rem !important; }
        .jobsync-presence-panel { position:relative; top:auto; right:auto; width:auto; max-height:none; margin:0 0 1rem; }
        .jobsync-presence-list { max-height:300px; }
    }
    @media (max-width:640px) {
        .block-container { padding-left:.65rem !important; padding-right:.65rem !important; }
        .home-center-brand { padding:2.4rem .4rem 1.6rem; }
        .home-center-title { font-size:clamp(2.8rem,16vw,4.2rem); }
        .contact-actions { flex-direction:column; width:100%; }
        .contact-btn { width:100%; text-align:center; box-sizing:border-box; }
    }

    </style>
    """,
    unsafe_allow_html=True,
)

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "auth_email" not in st.session_state:
    st.session_state.auth_email = ""
if "show_password_reset" not in st.session_state:
    st.session_state.show_password_reset = False
if "sidebar_collapsed" not in st.session_state:
    st.session_state.sidebar_collapsed = False
if "cv_studio_cycle" not in st.session_state:
    st.session_state.cv_studio_cycle = 0

# Deterministic JobSync sidebar state. This intentionally overrides Streamlit's
# remembered browser sidebar state so the application always starts with a usable menu.
if st.session_state.sidebar_collapsed:
    st.markdown("""
    <style>
      section[data-testid="stSidebar"] {
        width:0 !important; min-width:0 !important; max-width:0 !important;
        flex:0 0 0 !important;
        overflow:hidden !important;
        transform:none !important;
        visibility:visible !important;
        opacity:1 !important;
      }
      section[data-testid="stSidebar"] > div:first-child {
        width:280px !important;
        min-width:280px !important;
        max-width:280px !important;
        opacity:0 !important;
        pointer-events:none !important;
      }
      div[data-testid="stElementContainer"]:has(.jobsync-sidebar-reopen-marker) + div[data-testid="stElementContainer"] {
        position:fixed !important;
        left:0 !important;
        top:50% !important;
        transform:translateY(-50%) !important;
        z-index:2147483647 !important;
        width:52px !important;
      }
      div[data-testid="stElementContainer"]:has(.jobsync-sidebar-reopen-marker) + div[data-testid="stElementContainer"] button {
        min-width:52px !important;
        width:52px !important;
        height:76px !important;
        border-radius:0 14px 14px 0 !important;
        border:1px solid rgba(255,255,255,.15) !important;
        border-left:0 !important;
        background:linear-gradient(180deg,rgba(255,77,91,.34),rgba(34,197,94,.20)) !important;
        color:#fff !important;
        font-size:1.3rem !important;
        box-shadow:8px 0 30px rgba(0,0,0,.38) !important;
      }
      div[data-testid="stElementContainer"]:has(.jobsync-sidebar-reopen-marker) + div[data-testid="stElementContainer"] button:hover {
        border-color:#22c55e !important;
        box-shadow:8px 0 34px rgba(34,197,94,.16) !important;
      }
      .main .block-container {
        margin-left:0 !important;
      }
    </style>
    """, unsafe_allow_html=True)
else:
    st.markdown("""
    <style>
      section[data-testid="stSidebar"] {
        display:block !important;
        width:280px !important; min-width:280px !important; max-width:280px !important;
        flex:0 0 280px !important;
        transform:none !important;
        visibility:visible !important;
        opacity:1 !important;
        overflow:visible !important;
      }
      section[data-testid="stSidebar"] > div:first-child {
        opacity:1 !important; pointer-events:auto !important;
      }
    </style>
    """, unsafe_allow_html=True)

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
        .jobsync-presence-panel {
            position: relative !important;
            top: auto !important;
            right: auto !important;
            width: 100% !important;
            max-width: none !important;
            max-height: none !important;
            margin: 0 0 1rem !important;
        }
        .jobsync-presence-list { max-height: 280px !important; }
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
        .jobsync-presence-panel {
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
    </style>
    """,
    unsafe_allow_html=True,
)

# Local single-user authentication gate. Existing profile/application data remains
# in the same local workspace and is only accessible after login.
state = load_state()
profile = state["profile"]

def refresh_state() -> None:
    global state, profile
    state = load_state()
    profile = state["profile"]


def _presence_identity() -> tuple[str, str, str, str]:
    display_name = str(profile.get("name") or "User").strip() or "User"
    email = str(profile.get("email") or st.session_state.get("auth_email") or account_email() or "").strip().lower()
    seed = email or display_name or "job-tracker-user"
    role = get_user_role(email, default="member")
    return display_name, seed, email, role


def _role_badge(role: str) -> str:
    safe_role = normalize_role(role)
    return f'<span class="jobsync-role-badge {safe_role}">{html.escape(safe_role)}</span>'


def ensure_local_admin() -> str:
    """Ensure at least one local account is an admin so the role panel is reachable."""
    if not st.session_state.get("authenticated"):
        return "guest"
    email = str(st.session_state.get("auth_email") or account_email()).strip().lower()
    name = str(profile.get("name") or "User").strip() or "User"
    if not email:
        return "member"
    users = list_users()
    admins = [u for u in users if normalize_role(u.get("role")) == "admin"]
    # A single-account local installation should always leave its owner with
    # access to the administration panel. Older builds could have created that
    # first account as member/moderator, so migrate the sole account to admin.
    if not admins or (len(users) == 1 and users[0].get("email") == email):
        record = ensure_user(email, name, default_role="admin")
        if normalize_role(record.get("role")) != "admin":
            set_user_role(email, "admin", name)
        return "admin"
    return get_user_role(email, default="member")


def render_online_users() -> None:
    if not st.session_state.get("authenticated"):
        return

    display_name, avatar_seed, email, role = _presence_identity()
    touch_user(email, display_name)
    online = []
    presence_error = ""

    shared_presence_ok = False
    try:
        shared_presence_ok = heartbeat_presence(display_name, avatar_seed, email=email, role=role)
        if shared_presence_ok:
            try:
                online = list_online_users()
            except Exception as exc:
                # Heartbeat succeeded, so the current user is definitely online.
                # A failed read should not incorrectly mark the whole workspace Offline.
                presence_error = f"Shared presence read unavailable: {exc}"
                online = [{
                    "presence_id": "local-current",
                    "display_name": display_name,
                    "avatar_seed": avatar_seed,
                    "email": email,
                    "role": role,
                }]
        else:
            presence_error = "Supabase presence is not configured."
    except Exception as exc:
        presence_error = str(exc)

    # Always treat the current authenticated session as locally online.
    # This keeps the UI truthful even when shared Supabase presence is unavailable.
    if email and not any(str(u.get("email") or "").strip().lower() == email for u in online):
        online.insert(0, {
            "presence_id": "local-current",
            "display_name": display_name,
            "avatar_seed": avatar_seed,
            "email": email,
            "role": role,
        })

    online_emails = set()
    rows = []
    me_row = (f'<div class="jobsync-user-row jobsync-user-me">{avatar_html(avatar_seed,38)}'
              f'<div class="jobsync-user-copy"><div class="jobsync-user-name">{html.escape(display_name)} <span style="color:#697484;font-size:.6rem">YOU</span></div>'
              f'<div class="jobsync-user-meta"><span class="jobsync-user-status"><span class="jobsync-online-dot"></span>Online</span>{_role_badge(role)}</div></div></div>')
    for user in online:
        user_email = str(user.get("email") or "").strip().lower()
        if user_email:
            online_emails.add(user_email)
        name = html.escape(str(user.get("display_name") or "User"))
        seed = str(user.get("avatar_seed") or name)
        user_role = normalize_role(user.get("role") or get_user_role(user_email))
        avatar = avatar_html(seed, 38)
        rows.append(
            f'<div class="jobsync-user-row">{avatar}'
            f'<div class="jobsync-user-copy"><div class="jobsync-user-name">{name}</div>'
            f'<div class="jobsync-user-meta"><span class="jobsync-user-status"><span class="jobsync-online-dot"></span>Online</span>{_role_badge(user_role)}</div>'
            f'</div></div>'
        )

    offline_rows = []
    for user in list_users():
        user_email = str(user.get("email") or "").strip().lower()
        if not user_email or user_email in online_emails or user_email == email:
            continue
        name = html.escape(str(user.get("display_name") or user_email or "User"))
        user_role = normalize_role(user.get("role"))
        avatar = avatar_html(user_email or name, 38)
        offline_rows.append(
            f'<div class="jobsync-user-row">{avatar}'
            f'<div class="jobsync-user-copy"><div class="jobsync-user-name">{name}</div>'
            f'<div class="jobsync-user-meta"><span class="jobsync-user-status"><span class="jobsync-offline-dot"></span>Offline</span>{_role_badge(user_role)}</div>'
            f'</div></div>'
        )

    if presence_error:
        online_body = (
            "".join(rows)
            + '<div class="jobsync-presence-empty" style="padding-top:8px;">'
            '<span style="color:#7f8996;font-size:.68rem;">Shared presence is unavailable; showing this session as online.</span>'
            '</div>'
        )
        count_text = f"{max(1, len(rows))} online"
    else:
        online_body = "".join(rows) if rows else '<div class="jobsync-presence-empty">No users are online.</div>'
        count_text = f"{len(rows)} online"

    offline_body = "".join(offline_rows) if offline_rows else '<div class="jobsync-presence-empty">No offline users recorded on this installation.</div>'
    body = (f'<div class="jobsync-presence-section">You</div>{me_row}'
            f'<div class="jobsync-presence-section">Other online users</div>{online_body}'
            f'<div class="jobsync-presence-section">Offline users</div>{offline_body}')

    st.markdown(
        f'<aside class="jobsync-presence-panel">'
        f'<div class="jobsync-presence-head"><div class="jobsync-presence-title">Team Presence</div>'
        f'<div class="jobsync-presence-count">● {count_text}</div></div>'
        f'<div class="jobsync-presence-list">{body}</div></aside>',
        unsafe_allow_html=True,
    )


# Streamlit 1.37+ fragments can refresh only this small panel instead of
# rerunning the whole application. The current packaged environment uses a
# modern Streamlit version, so the presence heartbeat stays lightweight.
@st.fragment(run_every="30s")
def _online_users_fragment():
    render_online_users()


def notify_success(message: str, *args, **kwargs):
    """Keep the existing success message and also show a compact bottom-right toast."""
    result = st.success(message, *args, **kwargs)
    try:
        st.toast(str(message), icon="✅")
    except Exception:
        pass
    return result


def notify_error(message: str, *args, **kwargs):
    """Keep the existing error message and also show a compact bottom-right toast."""
    result = st.error(message, *args, **kwargs)
    try:
        st.toast(str(message), icon="⚠️")
    except Exception:
        pass
    return result

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



# ---------------- Navigation helpers ----------------
BASE_PAGES = ["Home", "Dashboard", "New Search", "Applied Jobs", "Gmail Updates", "LinkedIn Updates", "CV & Cover Letter", "Folders", "Profile", "Settings", "Admin Panel"]

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
PROTECTED_PAGES = set(PAGES) - {"Home", "Login", "Sign Up"}

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
    if page in PROTECTED_PAGES and not st.session_state.authenticated:
        st.session_state.nav = "Login"
    else:
        st.session_state.nav = page
    st.rerun()


def logout():
    try:
        remove_presence()
    except Exception:
        pass
    st.session_state.authenticated = False
    st.session_state.auth_email = ""
    st.session_state.nav = "Home"
    st.rerun()


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



def cv_document_records() -> list[dict]:
    """Return every locally managed CV record, generated or uploaded."""
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
with st.sidebar:
    # Custom sidebar toggle. Keep a clearly visible labelled control rather than
    # squeezing the icon into a tiny one-column container.
    if st.button("«  Hide navigation", key="jobsync_sidebar_hide", help="Hide navigation", width="stretch"):
        st.session_state.sidebar_collapsed = True
        st.rerun()
    current_email = st.session_state.get("auth_email") or account_email()
    current_role = ensure_local_admin() if st.session_state.authenticated else "guest"
    st.markdown(
        '<div class="brand"><div class="brand-name">💼 JobSync</div>'
        '<div class="brand-sub">Your local job-search workspace</div></div>',
        unsafe_allow_html=True,
    )

    if st.session_state.authenticated:
        st.caption("WORKSPACE")
        main_items = [("Home", "⌂"), ("Dashboard", "▦"), ("New Search", "⌕"), ("Applied Jobs", "✓"), ("Gmail Updates", "✉"), ("LinkedIn Updates", "in")]
        for p, icon in main_items:
            active = st.session_state.nav == p
            if st.button(f"{icon}  {p}", key=f"nav_{p}", width="stretch", type="primary" if active else "secondary"):
                go(p)

        st.caption("DOCUMENTS & ACCOUNT")
        tool_items = [("CV & Cover Letter", "▣"), ("Folders", "▤"), ("Profile", "◉"), ("Settings", "⚙")]
        for p, icon in tool_items:
            active = st.session_state.nav == p
            if st.button(f"{icon}  {p}", key=f"nav_{p}", width="stretch", type="primary" if active else "secondary"):
                go(p)

        custom = custom_sections()
        if custom:
            st.caption("CUSTOM SECTIONS")
            for item in custom:
                p = item["name"]
                active = st.session_state.nav == p
                if st.button(f"{item['icon']}  {p}", key=f"nav_custom_{safe_name(p,'section')}", width="stretch", type="primary" if active else "secondary"):
                    go(p)

        if ROLE_ACCESS.get(current_role, 0) >= ROLE_ACCESS["moderator"]:
            st.caption("ADMINISTRATION")
            active = st.session_state.nav == "Admin Panel"
            if st.button("🛡  Admin Panel", key="nav_admin_panel", width="stretch", type="primary" if active else "secondary"):
                go("Admin Panel")

        st.divider()
        avatar_seed = profile.get("email") or account_email() or profile.get("name") or "job-tracker-user"
        st.markdown(
            f'<div class="sidebar-userbar">{avatar_html(avatar_seed,42)}<div class="sidebar-usercopy"><div class="sidebar-username">{html.escape(profile.get("name") or "User")}</div><div class="sidebar-useremail">{html.escape(current_email)}</div><div class="sidebar-role">{_role_badge(current_role)}</div></div></div>',
            unsafe_allow_html=True,
        )
        if st.button("↪ Sign out", key="sidebar_logout", width="stretch"):
            logout()
        st.markdown('<div class="sidebar-footnote">MANUAL APPLICATIONS ONLY · YOU CONTROL THE FINAL SUBMISSION</div>', unsafe_allow_html=True)
    else:
        st.caption("WELCOME")
        if st.button("⌂  Home", key="nav_loggedout_home", width="stretch", type="primary" if st.session_state.nav == "Home" else "secondary"):
            go("Home")
        st.write("")
        if st.button("↪  Login", key="sidebar_login", type="primary", width="stretch"):
            go("Login")
        if st.button("✚  Sign up", key="sidebar_signup", width="stretch"):
            go("Sign Up")

# Permanent collaboration panel. It is rendered before every page so navigation does not remove it.
if st.session_state.sidebar_collapsed:
    st.markdown('<span class="jobsync-sidebar-reopen-marker" aria-hidden="true"></span>', unsafe_allow_html=True)
    if st.button("☰", key="jobsync_sidebar_reopen", help="Open navigation", width="stretch"):
        st.session_state.sidebar_collapsed = False
        st.rerun()

_online_users_fragment()

page = st.session_state.nav
if page in PROTECTED_PAGES and not st.session_state.authenticated:
    page = "Login"



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
    for file_path in (state_file, account_file, gmail_token_file):
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

    # Return to logged-out Home. The user must sign up again.
    st.session_state.authenticated = False
    st.session_state.auth_email = ""
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

# ---------------- AUTH ----------------
if page == "Login":
    st.markdown(
        '<div class="hero"><div class="hero-eyebrow">JOBSYNC · SIGN IN</div>'
        '<h1>Welcome back</h1>'
        '<p>Sign in to access your dashboard, job search, documents and application tracker.</p></div>',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="section-kicker">LOGIN</div>', unsafe_allow_html=True)

    if not account_exists():
        st.info("No local account exists on this laptop yet. Use Sign up to create your account.")
        if st.button("Create account", type="primary", width="stretch"):
            go("Sign Up")
    else:
        with st.form("login_form"):
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Login", type="primary", width="stretch")
        if submitted:
            login_email = email.strip().lower()
            if is_user_blocked(login_email):
                notify_error("This account is blocked from the JobSync workspace by an administrator.")
            elif verify_login(email, password):
                st.session_state.authenticated = True
                st.session_state.auth_email = email.strip().lower()
                default_role = "admin" if not list_users() else "member"
                ensure_user(st.session_state.auth_email, profile.get("name") or "User", default_role=default_role)
                st.session_state.show_password_reset = False
                st.session_state.nav = "Dashboard"
                st.rerun()
            else:
                notify_error("Incorrect email or password.")

        st.markdown('<div style="height:.2rem"></div>', unsafe_allow_html=True)
        if st.button("Forgot password?", key="show_forgot_password", width="stretch"):
            st.session_state.show_password_reset = not st.session_state.get("show_password_reset", False)

        if st.session_state.get("show_password_reset"):
            st.markdown('<div class="card" style="margin-top:.7rem"><div class="section-title">🔐 Local password recovery</div><div class="muted" style="font-size:.8rem;line-height:1.5">Saved passwords are never stored in readable form, so JobSync cannot display the existing password. Because this is a local account, recovery means replacing it with a new password on this computer.</div></div>', unsafe_allow_html=True)
            with st.form("reset_password_form"):
                reset_email = st.text_input("Account email", key="reset_email")
                reset_new = st.text_input("New password", type="password", key="reset_new", help="Use at least 8 characters.")
                reset_confirm = st.text_input("Confirm new password", type="password", key="reset_confirm")
                reset_submit = st.form_submit_button("Reset local password", type="secondary", width="stretch")
            if reset_submit:
                if reset_new != reset_confirm:
                    notify_error("The new passwords do not match.")
                else:
                    try:
                        reset_password(reset_email, reset_new)
                        st.session_state.show_password_reset = False
                        notify_success("Local password reset. You can now log in with the new password.")
                        st.rerun()
                    except Exception as exc:
                        notify_error(str(exc))

elif page == "Sign Up":
    st.markdown(
        '<div class="hero"><div class="hero-eyebrow">JOBSYNC · CREATE ACCOUNT</div>'
        '<h1>Create your local account</h1>'
        '<p>Your account is stored locally on this laptop. After signup, JobSync takes you to profile creation.</p></div>',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="section-kicker">SIGN UP</div>', unsafe_allow_html=True)

    if account_exists():
        st.info("A JobSync account already exists on this laptop. Please use Login.")
        if st.button("Go to Login", type="primary", width="stretch"):
            go("Login")
    else:
        with st.form("signup_form"):
            email = st.text_input("Email")
            password = st.text_input("Password", type="password", help="Use at least 8 characters.")
            confirm = st.text_input("Confirm password", type="password")
            accepted = st.checkbox("I understand that this is a local account and my JobSync data stays on this computer.")
            submitted = st.form_submit_button("Create account", type="primary", width="stretch")

        if submitted:
            if not accepted:
                notify_error("Please confirm the local-account notice.")
            elif password != confirm:
                notify_error("The passwords do not match.")
            else:
                try:
                    create_account(email, password)
                    st.session_state.authenticated = True
                    st.session_state.auth_email = email.strip().lower()
                    # The first local account owns this installation. Future accounts default to member.
                    default_role = "admin" if not list_users() else "member"
                    ensure_user(st.session_state.auth_email, profile.get("name") or "User", default_role=default_role)
                    st.session_state.nav = "Profile"
                    st.rerun()
                except Exception as exc:
                    notify_error(str(exc))

# ---------------- HOME ----------------
if page == "Home":
    name = profile.get("name", "").strip()
    display_name = html.escape(name) if name else ""
    jobs = state.get("jobs", [])
    applied = state.get("applied", [])
    cvs = generated_cvs()
    interviews = sum(1 for r in applied if r.get("status") == "Interview")
    offers = sum(1 for r in applied if r.get("status") == "Offer")

    if not st.session_state.authenticated:
        st.markdown(
            '<div class="home-center-brand">'
            '<div class="home-center-kicker">JOBSYNC · LOCAL JOB-SEARCH WORKSPACE</div>'
            '<div class="home-center-title">JobSync 👋</div>'
            '<div class="home-center-copy">Find recent jobs, build vacancy-specific CVs and cover letters, manage applications, and keep your recruitment workflow organized in one focused local workspace.</div>'
            f'<div class="home-center-copy" style="margin-top:1rem;font-size:.78rem;color:#697585;">Build {APP_VERSION} · Login and sign-up are available from the left navigation.</div>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.markdown('<div class="section-kicker">WHAT JOBSYNC IS FOR</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="info-card"><div class="card-heading">💼 Your job-search workspace, without the clutter</div><div class="card-body">'
            '<div class="update-row"><span class="update-dot" style="background:#ef4444"></span><span>Search recent jobs from your configured sources and keep the results together.</span></div>'
            '<div class="update-row"><span class="update-dot" style="background:#22c55e"></span><span>Generate vacancy-specific AI prompts using your profile plus an optional latest CV / cover-letter upload.</span></div>'
            '<div class="update-row"><span class="update-dot" style="background:#22c55e"></span><span>Track applications, documents and recruitment updates locally.</span></div>'
            '</div></div>',
            unsafe_allow_html=True,
        )
        st.markdown('<div class="section-kicker">DISCORD & WHATSAPP</div>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="contact-card"><div class="contact-copy"><div class="contact-title">Stay connected with JobSync</div>'
            f'<div class="contact-note">Support, updates and community discussion.</div></div>'
            f'<div class="contact-actions"><a class="contact-btn discord" href="{html.escape(DISCORD_URL, quote=True)}" target="_blank" rel="noopener noreferrer">🎮 Discord ↗</a>'
            f'<a class="contact-btn whatsapp" href="{html.escape(WHATSAPP_URL, quote=True)}" target="_blank" rel="noopener noreferrer">💬 WhatsApp ↗</a></div></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown('<div class="section-kicker">GET STARTED</div>', unsafe_allow_html=True)
        h1, h2, h3, h4 = st.columns(4, gap="medium")
        home_actions = [
            (h1, "👤", "Profile", "Set your job-search details.", "Open Profile", "Profile", "home_profile", "primary"),
            (h2, "🔎", "New Search", "Find all jobs returned by the selected sources.", "Search Jobs", "New Search", "home_search", "primary"),
            (h3, "📄", "CV Studio", "Prepare your tailored CV and cover letter.", "Open CV Studio", "CV & Cover Letter", "home_cv", "secondary"),
            (h4, "✓", "Applications", "See your applications and progress.", "Open Applications", "Applied Jobs", "home_apps", "secondary"),
        ]
        for col, icon, title, desc, label, destination, key, kind in home_actions:
            with col:
                st.markdown(
                    f'<div class="action-card"><div class="action-icon">{icon}</div>'
                    f'<div class="action-title">{title}</div>'
                    f'<div class="action-desc">{desc}</div></div>',
                    unsafe_allow_html=True,
                )
                st.button(
                    label, key=key,
                    type="primary" if kind == "primary" else "secondary",
                    width="stretch",
                    on_click=go, args=(destination,),
                )

        st.markdown('<div class="section-kicker">QUICK GLANCE</div>', unsafe_allow_html=True)
        q1, q2, q3, q4 = st.columns(4, gap="medium")
        glance = [
            (q1, "Fresh jobs", len(jobs), "Latest search", "red"),
            (q2, "Applied", len(applied), "Recorded applications", "green"),
            (q3, "Interviews", interviews, "Current pipeline", "green"),
            (q4, "Offers", offers, "Positive outcomes", "green"),
        ]
        for col, title, value, note, tone in glance:
            with col:
                st.markdown(
                    f'<div class="metric-card {tone}"><div class="metric-top"><span>{title}</span>'
                    f'<span class="metric-dot"></span></div><div class="metric-value">{value}</div>'
                    f'<div class="metric-note">{note}</div></div>',
                    unsafe_allow_html=True,
                )

        st.markdown('<div class="section-kicker">YOUR PROGRESS</div>', unsafe_allow_html=True)
        progress_pct = min(100, int((interviews / len(applied)) * 100)) if applied else 0
        st.markdown(
            f'<div class="info-card">'
            f'<div class="card-heading">📊 Application progress</div>'
            f'<div class="card-body">'
            f'<div style="display:flex;justify-content:space-between;margin:.7rem 0 .35rem;">'
            f'<span>Applications reaching interview</span><strong>{progress_pct}%</strong></div>'
            f'<div style="height:10px;background:#252a31;border-radius:99px;overflow:hidden;">'
            f'<div style="height:100%;width:{progress_pct}%;background:#22c55e;border-radius:99px;"></div></div>'
            f'<div style="margin-top:.8rem;color:#98a2b3;">'
            f'{len(jobs)} jobs found · {len(cvs)} CVs ready · {offers} offers</div>'
            f'</div></div>',
            unsafe_allow_html=True,
        )

        st.markdown('<div style="margin-top:1rem;"></div>', unsafe_allow_html=True)
        st.button(
            "▦ Open full dashboard",
            key="home_dashboard",
            type="secondary",
            width="stretch",
            on_click=go,
            args=("Dashboard",),
        )


        st.markdown('<div class="section-kicker">NEED HELP?</div>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="contact-card">'
            f'<div class="contact-copy"><div class="contact-title">Stay connected with JobSync</div>'
            f'<div class="contact-note">Reach out through our community channels.</div></div>'
            f'<div class="contact-actions">'
            f'<a class="contact-btn whatsapp" href="{html.escape(WHATSAPP_URL, quote=True)}" target="_blank" rel="noopener noreferrer">💬 WhatsApp</a>'
            f'<a class="contact-btn discord" href="{html.escape(DISCORD_URL, quote=True)}" target="_blank" rel="noopener noreferrer">🎮 Discord</a>'
            f'</div></div>',
            unsafe_allow_html=True,
        )


# ---------------- DASHBOARD ----------------
elif page == "Dashboard":
    name = profile.get("name", "").strip()
    fallback_username = (account_email().split("@", 1)[0].strip() if account_email() else "User")
    display_name = html.escape(name or fallback_username)
    avatar_seed = profile.get("email") or account_email() or name or "job-tracker-user"
    st.markdown(f'<div style="display:flex;align-items:center;gap:14px;margin-bottom:10px;">{avatar_html(avatar_seed,56)}<div><div style="font-size:.75rem;color:#8f99a7;font-weight:800;letter-spacing:.14em">YOUR PROFILE</div><div style="font-size:1.1rem;font-weight:800;color:#f5f7fa">{display_name}</div></div></div>', unsafe_allow_html=True)

    jobs = state.get("jobs", [])
    applied = state.get("applied", [])
    cvs = generated_cvs()
    letters = generated_letters()
    interviews = sum(1 for r in applied if r.get("status") == "Interview")
    offers = sum(1 for r in applied if r.get("status") == "Offer")
    rejected = sum(1 for r in applied if r.get("status") == "Rejected")
    response_rate = (interviews / len(applied) * 100) if applied else 0

    # A true Home/Welcome page: always visible, whether or not the profile
    # has already been completed. It combines a project overview with the
    # live dashboard so the user gets the full picture at a glance.
    st.markdown(
        f'<div class="hero">'
        f'<div class="hero-eyebrow">JOBSYNC · YOUR JOB SEARCH WORKSPACE</div>'
        f'<h1>Welcome to JobSync{", " + display_name if name else ""} 👋</h1>'
        f'<p>Find recent jobs, prepare a tailored CV and cover letter, apply yourself, '
        f'and keep every application organized in one local workspace.</p>'
        f'</div>',
        unsafe_allow_html=True,
    )

    st.markdown('<div class="section-kicker">YOUR OVERVIEW</div>', unsafe_allow_html=True)
    c1, c2, c3, c4, c5 = st.columns(5, gap="medium")
    metrics = [
        ("Fresh jobs", len(jobs), "Latest search", "red"),
        ("Applied", len(applied), "Recorded", "green"),
        ("Interviews", interviews, "Pipeline", "green"),
        ("Offers", offers, "Outcomes", "green"),
        ("CVs ready", len(cvs), "Generated locally", "red"),
    ]
    for col, (title, value, note, tone) in zip((c1, c2, c3, c4, c5), metrics):
        with col:
            st.markdown(
                f'<div class="metric-card {tone}"><div class="metric-top"><span>{title}</span>'
                f'<span class="metric-dot"></span></div><div class="metric-value">{value}</div>'
                f'<div class="metric-note">{note}</div></div>',
                unsafe_allow_html=True,
            )

    st.markdown('<div class="section-kicker">APPLICATION PROGRESS</div>', unsafe_allow_html=True)
    import pandas as pd
    import plotly.graph_objects as go_plotly

    g1, g2, g3 = st.columns(3, gap="medium")
    with g1:
        st.markdown('<div class="chart-header"><div class="chart-title">Application pipeline</div><div class="chart-subtitle">From discovery to outcome</div></div>', unsafe_allow_html=True)
        labels = ["Found", "Applied", "Interview", "Offer"]
        values = [len(jobs), len(applied), interviews, offers]
        fig = go_plotly.Figure(go_plotly.Bar(x=values, y=labels, orientation="h"))
        fig.update_layout(height=280, margin=dict(l=5,r=10,t=10,b=10), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font=dict(color="#eef2f7"), xaxis=dict(gridcolor="#252a31", zerolinecolor="#252a31"), yaxis=dict(gridcolor="rgba(0,0,0,0)"), showlegend=False)
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    
    with g2:
        st.markdown('<div class="chart-header"><div class="chart-title">Applications by week</div><div class="chart-subtitle">Your activity over time</div></div>', unsafe_allow_html=True)
        buckets = defaultdict(int)
        for row in applied:
            d = row.get("applied_date", "")
            try:
                dt = datetime.strptime(d, "%Y-%m-%d").date()
                monday = dt - timedelta(days=dt.weekday())
                buckets[monday] += 1
            except Exception:
                continue
        if buckets:
            ordered = dict(sorted(buckets.items()))
            df = pd.DataFrame({"week":[week.strftime("%d %b") for week in ordered], "applications":list(ordered.values())})
            fig2 = go_plotly.Figure(go_plotly.Scatter(x=df["week"], y=df["applications"], mode="lines+markers", fill="tozeroy"))
            fig2.update_layout(height=280, margin=dict(l=5,r=10,t=10,b=10), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font=dict(color="#eef2f7"), xaxis=dict(gridcolor="#252a31"), yaxis=dict(gridcolor="#252a31", dtick=1), showlegend=False)
            st.plotly_chart(fig2, width="stretch", config={"displayModeBar": False})
        else:
            st.markdown('<div class="chart-empty">Your weekly application activity will appear here after you record applications.</div>', unsafe_allow_html=True)
    
    with g3:
        st.markdown('<div class="chart-header"><div class="chart-title">Current outcomes</div><div class="chart-subtitle">Where your applications stand</div></div>', unsafe_allow_html=True)
        counts = Counter((r.get("status") or "Applied") for r in applied)
        pie_labels = list(counts.keys()) or ["No applications"]
        pie_values = list(counts.values()) or [1]
        fig3 = go_plotly.Figure(go_plotly.Pie(labels=pie_labels, values=pie_values, hole=0.62, textinfo="label+percent", sort=False))
        fig3.update_layout(height=280, margin=dict(l=5,r=5,t=5,b=5), paper_bgcolor="rgba(0,0,0,0)", font=dict(color="#eef2f7"), showlegend=False)
        st.plotly_chart(fig3, width="stretch", config={"displayModeBar": False})
        st.markdown(f'<div class="chart-foot">Interview conversion: <strong>{response_rate:.0f}%</strong> · Rejected: <strong>{rejected}</strong> · Offers: <strong>{offers}</strong></div>', unsafe_allow_html=True)

    st.markdown('<div class="section-kicker">RECENT ACTIVITY</div>', unsafe_allow_html=True)
    lower_left, lower_right = st.columns([1.15, 1], gap="medium")
    with lower_left:
        updates = latest_updates()
        update_rows = []
        if not updates:
            update_rows.append('<div class="empty-state">No activity yet. Start a job search to create your first updates.</div>')
        else:
            for _, kind, text in updates:
                dot = {"search": "#ef4444", "apply": "#22c55e", "cv": "#ef4444", "letter": "#22c55e"}.get(kind, "#8a94a3")
                update_rows.append(f'<div class="update-row"><span class="update-dot" style="background:{dot}"></span><span>{html.escape(text)}</span></div>')
        updates_html = ''.join(update_rows)
        st.markdown(f'<div class="info-card"><div class="card-heading">🔔 New updates</div><div class="card-body">{updates_html}</div></div>', unsafe_allow_html=True)
    with lower_right:
        profile_rows = [
            ("Name", profile.get("name") or "Not set"),
            ("Location", profile.get("city") or profile.get("location") or "Not set"),
            ("Target field", profile.get("field") or "Not set"),
            ("Industry", profile.get("industry") or "Any"),
            ("Experience", profile.get("experience") or "Any"),
            ("Required language", profile.get("language") or "Any"),
        ]
        profile_html = ''.join(f'<div class="profile-line"><span class="profile-label">{html.escape(label)}</span><span class="profile-value">{html.escape(str(value))}</span></div>' for label, value in profile_rows)
        st.markdown(f'<div class="info-card"><div class="card-heading">👤 Your profile</div><div class="card-body">{profile_html}</div></div>', unsafe_allow_html=True)

    st.markdown('<div class="section-kicker">LATEST JOBS</div>', unsafe_allow_html=True)
    st.markdown('<div class="jobs-panel">', unsafe_allow_html=True)
    if jobs:
        header = st.columns([2.6, 1.5, 1.4, 1.0])
        for col, text in zip(header, ["JOB", "COMPANY / LOCATION", "POSTED", "SOURCE"]):
            col.markdown(f'<div class="table-head">{text}</div>', unsafe_allow_html=True)
        for job in jobs[:6]:
            r = st.columns([2.6, 1.5, 1.4, 1.0])
            with r[0]:
                st.markdown(f'<div class="job-row-title">{html.escape(job.get("title", "Untitled"))}</div>', unsafe_allow_html=True)
            with r[1]:
                st.markdown(f'<div class="job-row-sub">{html.escape(job.get("company", "Unknown company"))}<br>{html.escape(job.get("location", ""))}</div>', unsafe_allow_html=True)
            with r[2]:
                st.markdown(f'<div class="job-row-sub">{html.escape(job.get("posted_date", "Unknown"))}</div>', unsafe_allow_html=True)
            with r[3]:
                st.markdown(f'<span class="source-pill">{html.escape(job.get("source", ""))}</span>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="empty-state">No jobs yet. Use New Search to populate your dashboard.</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

# ---------------- NEW SEARCH ----------------
elif page == "New Search":
    st.markdown('<div class="mh-page-hero"><div class="mh-page-kicker">JOBSYNC • SEARCH LAB</div><div class="mh-page-title">Find the right opening, faster.</div><div class="mh-page-copy">Tune your search profile, choose the collection network, then let JobSync assemble one clean results stream without changing the underlying search engine.</div><div class="mh-page-meta"><span class="mh-meta-chip"><span class="mh-meta-dot"></span> LIVE SEARCH WORKSPACE</span><span class="mh-meta-chip"><span class="mh-meta-dot green"></span> FREE SOURCES READY</span><span class="mh-meta-chip"><span class="mh-meta-dot blue"></span> LOCAL RESULTS</span></div></div><div class="mh-flowbar"><div class="mh-flowitem"><div class="mh-flownum">01</div><div><div class="mh-flowname">Sources</div><div class="mh-flowdesc">Choose collectors</div></div></div><div class="mh-flowitem"><div class="mh-flownum">02</div><div><div class="mh-flowname">Profile</div><div class="mh-flowdesc">Define the role</div></div></div><div class="mh-flowitem"><div class="mh-flownum">03</div><div><div class="mh-flowname">Search</div><div class="mh-flowdesc">Scan the network</div></div></div><div class="mh-flowitem"><div class="mh-flownum">04</div><div><div class="mh-flowname">Results</div><div class="mh-flowdesc">Review &amp; act</div></div></div></div>', unsafe_allow_html=True)

    configured_ids = state.get("settings", {}).get("actor_ids") or [ACTOR_CATALOG[name]["id"] for name in DEFAULT_ACTOR_NAMES]
    configured_names = [ACTOR_ID_TO_NAME.get(x, x) for x in configured_ids]
    saved_mode = state.get("settings", {}).get("job_search_mode", "free")
    if saved_mode not in JOB_SEARCH_MODE_LABELS:
        saved_mode = "free"

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">1. Choose your job source</div>', unsafe_allow_html=True)
    saved_preset = state.get("settings", {}).get("job_source_preset", "open")
    if saved_preset not in {"open", "linkedin", "apify"}:
        saved_preset = "apify" if saved_mode == "apify" else "open"

    preset_labels = ["Open source", "LinkedIn", "Apify"]
    preset_values = ["open", "linkedin", "apify"]
    preset_label = st.radio(
        "Job scraping option",
        options=preset_labels,
        index=preset_values.index(saved_preset),
        horizontal=True,
        help="Open source uses public/keyless sources. LinkedIn uses the direct LinkedIn jobs collector. Apify uses your configured Actors.",
    )
    source_preset = preset_values[preset_labels.index(preset_label)]

    configured_free = state.get("settings", {}).get("free_sources") or FREE_SOURCE_NAMES
    if source_preset == "open":
        search_mode = "free"
        free_source_names = st.multiselect(
            "Open-source collectors",
            options=OPEN_SOURCE_DEFAULTS,
            default=[x for x in configured_free if x in OPEN_SOURCE_DEFAULTS] or OPEN_SOURCE_DEFAULTS,
            help="Select the public/open sources to search. Apify is not used in this mode.",
        )
        st.caption("Public options include Bundesagentur für Arbeit, Indeed, StepStone, Monster, Glassdoor, Arbeitnow, Remote OK, Remotive and public ATS boards.")
        source_names = []
    elif source_preset == "linkedin":
        search_mode = "free"
        free_source_names = ["LinkedIn"]
        source_names = []
        st.success("LinkedIn selected — JobSync will search LinkedIn directly without Apify.")
    else:
        search_mode = "apify"
        free_source_names = []
        source_names = st.multiselect(
            "Apify Actors",
            options=list(ACTOR_CATALOG.keys()),
            default=[x for x in configured_names if x in ACTOR_CATALOG],
            help="Select the Apify Actors to use for this search.",
        )
        st.caption("Apify selected — only the selected Actors will be called.")

    ats_urls_text = st.text_area(
        "Company ATS career URLs (optional)",
        value="\n".join(state.get("settings", {}).get("ats_urls") or []),
        placeholder="https://company.wd5.myworkdayjobs.com/Careers\nhttps://boards.greenhouse.io/company\nhttps://jobs.lever.co/company",
        help="Public Workday, Greenhouse, Lever, Ashby, SmartRecruiters, Workable or Personio boards are used with Open source.",
    )
    ats_urls = [x.strip() for x in ats_urls_text.splitlines() if x.strip()]
    st.markdown('</div>', unsafe_allow_html=True)
    st.write("")

    st.markdown('<div class="section-title">2. Search parameters</div>', unsafe_allow_html=True)
    with st.form("search_form"):
        c1, c2 = st.columns(2)
        with c1:
            field = st.text_input("Field / job title", value=profile.get("field", ""), placeholder="Mechanical Engineer")
            industry = st.text_input("Industry", value=profile.get("industry", ""), placeholder="Manufacturing, optics, automotive")
        with c2:
            location = st.text_input("Location", value=profile.get("location", profile.get("city", "")), placeholder="Hannover, Germany")
            experience_options = ["Any", "Internship", "Entry level", "Associate", "Mid-Senior level", "Director"]
            current_exp = profile.get("experience", "Any") if profile.get("experience", "Any") in experience_options else "Any"
            experience = st.selectbox("Experience", experience_options, index=experience_options.index(current_exp))
            language_options = ["English", "German", "French", "Spanish", "Italian", "Dutch", "Any"]
            current_language = profile.get("language", "Any") if profile.get("language", "Any") in language_options else "Any"
            language = st.selectbox(
                "Required language",
                language_options,
                index=language_options.index(current_language),
                help="Any is the default. Choose a specific language to apply the explicit language-requirement filter; other languages mentioned as advantages are shown on each job."
            )
        date_options = {
            "Past 24 hours": 1,
            "Past 3 days": 3,
            "Past 7 days": 7,
        }
        current_window = profile.get("date_window", "Past 7 days")
        if current_window not in date_options:
            current_window = "Past 7 days"
        date_window = st.selectbox(
            "Date posted",
            list(date_options.keys()),
            index=list(date_options.keys()).index(current_window),
            help="The selected window is sent to compatible Apify job sources and is also enforced locally before results are shown."
        )
        st.caption("The selected date window is enforced locally. In free mode, no Apify request is made. In Apify mode, compatible Actors also receive the freshness window.")
        submitted = st.form_submit_button("🔎 Find all matching jobs", type="primary", width="stretch")
    st.markdown('</div>', unsafe_allow_html=True)

    if submitted:
        profile.update({"field": field.strip(), "industry": industry.strip(), "location": location.strip(), "experience": experience, "language": language, "date_window": date_window})
        save_state(state)
        try:
            actor_ids = [ACTOR_CATALOG[name]["id"] for name in source_names]
            if search_mode in {"apify", "both"} and not actor_ids:
                raise RuntimeError("Select at least one Apify Actor for the selected search method.")
            with st.spinner("Searching job sources…"):
                os.environ["JOB_TRACKER_DATE_WINDOW_DAYS"] = str(date_options[date_window])
                results = search_jobs(
                    field.strip(),
                    location.strip(),
                    industry.strip(),
                    experience,
                    language=language,
                    limit=10000,
                    actor_ids=actor_ids,
                    search_mode=search_mode,
                    free_sources=free_source_names,
                    ats_urls=ats_urls,
                )
            state.setdefault("settings", {})["actor_ids"] = actor_ids
            state.setdefault("settings", {})["job_search_mode"] = search_mode
            state.setdefault("settings", {})["job_source_preset"] = source_preset
            state.setdefault("settings", {})["free_sources"] = free_source_names
            state.setdefault("settings", {})["ats_urls"] = ats_urls
            state["jobs"] = results
            state["search_history"].append({
                "field": field.strip(), "industry": industry.strip(), "location": location.strip(),
                "experience": experience, "language": language, "count": len(results), "searched_at": datetime.now().isoformat(timespec="seconds")
            })
            state["search_history"] = state["search_history"][-25:]
            save_state(state)
            notify_success(f"Found {len(results)} jobs from the selected sources.")
        except Exception as exc:
            notify_error(str(exc))

    if state["jobs"]:
        st.write("")
        # Show a compact source summary so it is immediately clear which selected
        # collectors actually returned jobs and whether a public source failed.
        source_counts = {}
        source_warnings = set()
        for row in state["jobs"]:
            source_name = row.get("source") or row.get("actor") or "Unknown"
            source_counts[source_name] = source_counts.get(source_name, 0) + 1
            for warning in row.get("warnings", []) or []:
                source_warnings.add(str(warning))
        if source_counts:
            summary = " · ".join(f"{html.escape(str(k))}: {v}" for k, v in sorted(source_counts.items()))
            st.markdown(f'<div class="muted" style="margin-bottom:8px">Sources returned: {summary}</div>', unsafe_allow_html=True)
        if source_warnings:
            with st.expander("Source diagnostics"):
                for warning in sorted(source_warnings):
                    st.caption("⚠ " + warning)
        st.markdown(f'<div class="section-title">Results <span class="muted">({len(state["jobs"])} shown)</span></div>', unsafe_allow_html=True)
        for idx, job in enumerate(state["jobs"]):
            already_applied = any(r.get("url") == job.get("url") for r in state["applied"] if r.get("url"))
            with st.container(border=True):
                left, right = st.columns([5, 1.2])
                with left:
                    st.markdown(f"<div class='job-title'>{html.escape(job.get('title','Untitled'))}</div>", unsafe_allow_html=True)
                    st.markdown(f"<div class='job-company'>{html.escape(job.get('company','Unknown company'))} · {html.escape(job.get('location',''))}</div>", unsafe_allow_html=True)
                    tags = [x for x in [job.get("work_type"), job.get("contract_type"), job.get("experience")] if x]
                    if job.get("language_required"):
                        tags.append(f"Language required: {job['language_required']}")
                    if tags:
                        st.markdown(" ".join(f"<span class='pill'>{html.escape(str(x))}</span>" for x in tags), unsafe_allow_html=True)
                    if job.get("language_advantages"):
                        st.caption("Language advantage: " + ", ".join(job["language_advantages"]))
                    st.caption(f"Posted: {job.get('posted_date','Unknown')} · Source: {job.get('source') or job.get('actor','Apify')} · Salary: {job.get('salary') or 'Not listed'}")
                    # Job sources may return raw HTML or entity-escaped HTML.
                    # Always convert descriptions to readable plain text before
                    # displaying them so tags such as <h3> never appear in the UI.
                    raw_desc = str(job.get("description") or "").strip()
                    desc = html.unescape(raw_desc)
                    for _ in range(2):
                        decoded = html.unescape(desc)
                        if decoded == desc:
                            break
                        desc = decoded
                    if "<" in desc and ">" in desc:
                        from bs4 import BeautifulSoup
                        desc = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)
                    desc = re.sub(r"\s+", " ", desc).strip()
                    if desc:
                        with st.expander("Preview job description"):
                            st.write(desc[:2800] + ("…" if len(desc) > 2800 else ""))
                with right:
                    if job.get("url"):
                        st.link_button("Open job ↗", job["url"], width="stretch")
                    if already_applied:
                        notify_success("Tracked")
                    elif st.button("Mark applied", key=f"mark_{idx}", width="stretch"):
                        state["applied"].append({
                            **job,
                            "applied_date": datetime.now().strftime("%Y-%m-%d"),
                            "status": "Applied",
                            "cv_path": "",
                            "cover_letter_path": "",
                        })
                        save_state(state)
                        notify_success("Application recorded.")
                    if st.button("Prepare CV", key=f"cv_{idx}", width="stretch"):
                        st.session_state.selected_job_index = idx
                        go("CV & Cover Letter")

# ---------------- APPLIED ----------------
elif page == "Applied Jobs":
    st.markdown('<div class="mh-page-hero"><div class="mh-page-kicker">JOBSYNC • APPLICATION COMMAND</div><div class="mh-page-title">Keep every application moving.</div><div class="mh-page-copy">A focused view of your application pipeline, linked documents, and next-stage actions — without changing how your tracker stores or updates applications.</div><div class="mh-page-meta"><span class="mh-meta-chip"><span class="mh-meta-dot"></span> APPLICATION PIPELINE</span><span class="mh-meta-chip"><span class="mh-meta-dot green"></span> DOCUMENT LINKS</span><span class="mh-meta-chip"><span class="mh-meta-dot blue"></span> STATUS CONTROL</span></div></div><div class="mh-pulse-card"><div class="mh-pulse-left"><div class="mh-pulse-orb">✓</div><div><div class="mh-pulse-title">Application control center</div><div class="mh-pulse-sub">Update status, connect your CV/cover letter, and keep the exact job record together.</div></div></div><div class="mh-pulse-status">TRACKING ACTIVE</div></div>', unsafe_allow_html=True)
    applied = state["applied"]
    counts = Counter((r.get("status") or "Applied") for r in applied)
    a, b, c, d = st.columns(4)
    a.metric("Applied", len(applied))
    b.metric("Interview", counts.get("Interview", 0))
    c.metric("Offer", counts.get("Offer", 0))
    d.metric("Rejected", counts.get("Rejected", 0))
    st.write("")

    x1, x2 = st.columns([1, 3])
    with x1:
        if st.button("📊 Update Excel tracker", type="primary", width="stretch"):
            try:
                out = export_applied_jobs_xlsx(applied, TRACKER)
                notify_success(f"Updated {out.name}")
            except Exception as exc:
                notify_error(f"Could not create Excel tracker: {exc}")
    with x2:
        if TRACKER.exists():
            st.download_button("Download Excel tracker", TRACKER.read_bytes(), file_name=TRACKER.name, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    if not applied:
        st.info("No applications recorded yet. Use New Search → Mark applied.")
    else:
        st.write("")
        status_values = ["Applied", "Shortlisted", "Interview", "Offer", "Rejected", "Withdrawn"]
        cv_options = [""] + [d["path"] for d in generated_cvs()]
        cl_options = [""] + [d["path"] for d in generated_letters()]
        for idx, row in enumerate(applied):
            with st.container(border=True):
                h1, h2 = st.columns([4, 1.1])
                with h1:
                    st.markdown(f"<div class='job-title'>{html.escape(row.get('title','Unknown'))}</div>", unsafe_allow_html=True)
                    st.markdown(f"<div class='job-company'>{html.escape(row.get('company','Unknown'))} · {html.escape(row.get('location',''))}</div>", unsafe_allow_html=True)
                    st.caption(f"Applied: {row.get('applied_date','')} · {row.get('source','')} · {row.get('url','')}")
                with h2:
                    old_status = row.get("status", "Applied")
                    status = st.selectbox("Status", status_values, index=status_values.index(old_status) if old_status in status_values else 0, key=f"status_{idx}")
                    st.markdown(f"<span class='status-pill {status_class(status)}'>{html.escape(status)}</span>", unsafe_allow_html=True)
                    if status != old_status:
                        row["status"] = status
                        save_state(state)
                if row.get("url"):
                    st.link_button("Open original job ↗", row["url"])
                c1, c2 = st.columns(2)
                with c1:
                    cv_path = st.selectbox("CV used", cv_options, index=cv_options.index(row.get("cv_path", "")) if row.get("cv_path", "") in cv_options else 0, key=f"cvused_{idx}")
                with c2:
                    cl_path = st.selectbox("Cover letter", cl_options, index=cl_options.index(row.get("cover_letter_path", "")) if row.get("cover_letter_path", "") in cl_options else 0, key=f"clused_{idx}")
                if cv_path != row.get("cv_path") or cl_path != row.get("cover_letter_path"):
                    row["cv_path"] = cv_path
                    row["cover_letter_path"] = cl_path
                    save_state(state)

# ---------------- GMAIL UPDATES ----------------
elif page == "Gmail Updates":
    st.markdown('<div class="mh-page-hero"><div class="mh-page-kicker">JOBSYNC • MAIL RADAR</div><div class="mh-page-title">Turn recruitment mail into signals.</div><div class="mh-page-copy">Connect Gmail in read-only mode, sync recent recruitment messages, and surface application updates inside the same workspace.</div><div class="mh-page-meta"><span class="mh-meta-chip"><span class="mh-meta-dot"></span> READ-ONLY</span><span class="mh-meta-chip"><span class="mh-meta-dot green"></span> RECRUITMENT FOCUS</span><span class="mh-meta-chip"><span class="mh-meta-dot blue"></span> LOCAL MATCHING</span></div></div><div class="mh-flowbar"><div class="mh-flowitem"><div class="mh-flownum">01</div><div><div class="mh-flowname">Connect</div><div class="mh-flowdesc">Google sign-in</div></div></div><div class="mh-flowitem"><div class="mh-flownum">02</div><div><div class="mh-flowname">Sync</div><div class="mh-flowdesc">Recent messages</div></div></div><div class="mh-flowitem"><div class="mh-flownum">03</div><div><div class="mh-flowname">Match</div><div class="mh-flowdesc">Tracked applications</div></div></div><div class="mh-flowitem"><div class="mh-flownum">04</div><div><div class="mh-flowname">Update</div><div class="mh-flowdesc">Move your pipeline</div></div></div></div>', unsafe_allow_html=True)

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
    st.markdown('<div class="mh-page-hero"><div class="mh-page-kicker">JOBSYNC • NETWORK PULSE</div><div class="mh-page-title">Stay close to the people behind the application.</div><div class="mh-page-copy">Sync LinkedIn notifications into a dedicated local panel while keeping your existing browser-based connection and notification workflow intact.</div><div class="mh-page-meta"><span class="mh-meta-chip"><span class="mh-meta-dot"></span> BROWSER CONNECTION</span><span class="mh-meta-chip"><span class="mh-meta-dot green"></span> NOTIFICATION SYNC</span><span class="mh-meta-chip"><span class="mh-meta-dot blue"></span> LOCAL HISTORY</span></div></div><div class="mh-pulse-card"><div class="mh-pulse-left"><div class="mh-pulse-orb">in</div><div><div class="mh-pulse-title">LinkedIn signal channel</div><div class="mh-pulse-sub">Connect once in the browser, then use this panel as your notification cockpit.</div></div></div><div class="mh-pulse-status">NETWORK READY</div></div>', unsafe_allow_html=True)

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
    """Prepare a focused external-AI handoff and save only the finished PDF locally."""
    cv_cycle = int(st.session_state.get("cv_studio_cycle", 0))
    st.markdown(
        '<div class="cv-hero"><div class="cv-kicker">JOBSYNC • DOCUMENT STUDIO</div>'
        '<div class="cv-title-row"><div class="cv-orb">JH</div><div><div class="cv-page-title">CV &amp; Cover Letter</div>'
        '<div class="cv-page-subtitle">Build one focused prompt, open your AI, download the finished PDF, and save it to Folders.</div></div></div>'
        '<div class="cv-hero-copy">Three steps: choose → prepare prompt → upload final PDF.</div>'
        '<div class="cv-hero-badge"><span class="cv-live-dot"></span> SIMPLE WORKFLOW</div></div>',
        unsafe_allow_html=True,
    )

    st.markdown('<div class="cv-section-head"><span class="cv-section-number">01</span><div><b>Choose the AI and document</b><span>Nothing is uploaded to the AI automatically.</span></div></div>', unsafe_allow_html=True)
    c1, c2 = st.columns([2, 1])
    with c1:
        ai_choice = st.radio("AI model", ["ChatGPT", "Claude", "Gemini"], horizontal=True, key=f"external_ai_choice_{cv_cycle}")
    with c2:
        doc_type = st.radio("Document", ["CV", "Cover Letter"], horizontal=True, key=f"external_document_type_{cv_cycle}")

    st.markdown('<div class="cv-section-head"><span class="cv-section-number">02</span><div><b>Choose the job</b><span>Use a saved search result or enter the vacancy yourself.</span></div></div>', unsafe_allow_html=True)
    jobs = state.get("jobs", [])
    source_options = (["Use a saved search job"] if jobs else []) + ["Enter job manually"]
    job_source = st.radio("Job details", source_options, horizontal=True, key=f"cv_job_source_{cv_cycle}")
    if job_source == "Use a saved search job":
        default_index = st.session_state.get("selected_job_index", 0)
        if not isinstance(default_index, int) or default_index >= len(jobs):
            default_index = 0
        selected = st.selectbox(
            "Saved job", range(len(jobs)), index=default_index,
            format_func=lambda i: f"{jobs[i].get('title') or 'Untitled role'} — {jobs[i].get('company') or 'Unknown company'}",
            key=f"external_target_job_{cv_cycle}",
        )
        base_job = dict(jobs[selected])
    else:
        base_job = {"title": "", "company": "", "location": "", "url": "", "description": ""}

    j1, j2 = st.columns(2)
    with j1:
        manual_title = st.text_input("Position / job title", value=str(base_job.get("title") or ""), key=f"cv_job_title_{cv_cycle}")
        manual_company = st.text_input("Company", value=str(base_job.get("company") or ""), key=f"cv_job_company_{cv_cycle}")
    with j2:
        manual_location = st.text_input("Location", value=str(base_job.get("location") or profile.get("location") or profile.get("city") or ""), key=f"cv_job_location_{cv_cycle}")
        manual_url = st.text_input("Job posting URL", value=str(base_job.get("url") or ""), key=f"cv_job_url_{cv_cycle}")
    manual_description = st.text_area(
        "Job description", value=str(base_job.get("description") or ""), height=135,
        key=f"cv_job_description_{cv_cycle}", placeholder="Paste the vacancy here, or leave the saved search text as provided.",
    )
    working_job = {
        **base_job,
        "title": manual_title.strip(),
        "company": manual_company.strip(),
        "location": manual_location.strip(),
        "url": manual_url.strip(),
        "description": manual_description.strip(),
    }
    st.markdown(
        f'<div class="cv-target-card"><div class="cv-target-top"><span class="cv-label">TARGET</span><span class="cv-match-chip">{html.escape(doc_type.upper())}</span></div>'
        f'<div class="cv-target-title">{html.escape(working_job.get("title") or "Untitled role")}</div>'
        f'<div class="cv-target-meta">{html.escape(working_job.get("company") or "Company not entered")} &nbsp;•&nbsp; {html.escape(working_job.get("location") or "Location not entered")}</div></div>',
        unsafe_allow_html=True,
    )

    with st.expander("Optional: latest CV / cover letter", expanded=False):
        latest_reference_uploads = st.file_uploader(
            "Upload one or both reference files", type=["pdf", "tex", "docx"], accept_multiple_files=True,
            key=f"latest_candidate_documents_{cv_cycle}",
        )
        if latest_reference_uploads and len(latest_reference_uploads) > 2:
            st.warning("Upload at most two reference files.")
        st.caption("Used only for the current prompt. These uploads are not added to Folders and are cleared after the final PDF is saved.")

    with st.expander("Optional: LaTeX template", expanded=False):
        template_upload = st.file_uploader("Upload a .tex template", type=["tex"], key=f"cv_template_one_{cv_cycle}")
        template = None
        template_path = CV_BASE_TEMPLATE_PATH if doc_type == "CV" else COVER_LETTER_BASE_TEMPLATE_PATH
        if template_upload is not None:
            try:
                content = template_upload.getvalue()
                if content:
                    template = content.decode("utf-8", errors="ignore")
            except Exception as exc:
                notify_error(f"Could not read the optional LaTeX template: {exc}")
        elif template_path.exists():
            template = template_path.read_text(encoding="utf-8", errors="ignore").strip() or None
        st.caption("Optional. Leave this closed to use the standard JobSync template guidance.")

    st.markdown('<div class="cv-section-head"><span class="cv-section-number">03</span><div><b>Prepare the prompt</b><span>One action. JobSync builds the complete handoff for your selected AI.</span></div></div>', unsafe_allow_html=True)
    if st.button("Prepare prompt", type="primary", width="stretch"):
        if not working_job.get("title") and not working_job.get("description"):
            notify_error("Enter a job title or paste a job description first.")
        else:
            latest_pdf_text = latest_final_pdf_evidence(state, working_job, doc_type)
            evidence_refs = []
            for uploaded in (latest_reference_uploads or [])[:2]:
                try:
                    raw = uploaded.getvalue()
                    suffix = Path(uploaded.name).suffix.lower()
                    if suffix in {".txt", ".tex"}:
                        text = raw.decode("utf-8", errors="ignore")
                    elif suffix in {".pdf", ".docx"}:
                        temp = UPLOAD_REFERENCES / f"__prompt_{safe_name(Path(uploaded.name).stem)}_{cv_cycle}{suffix}"
                        temp.parent.mkdir(parents=True, exist_ok=True)
                        temp.write_bytes(raw)
                        text = extract_text(temp)
                        temp.unlink(missing_ok=True)
                    else:
                        text = ""
                    if text.strip():
                        evidence_refs.append({"name": uploaded.name, "text": text.strip()[:14000], "reference_type": "document"})
                except Exception as exc:
                    notify_error(f"Could not read {uploaded.name}: {exc}")
            evidence = build_reference_context(evidence_refs) if evidence_refs else ""
            if latest_pdf_text:
                evidence += "\n\nLATEST USER-APPROVED FINAL PDF CONTENT\n" + latest_pdf_text[:18000]
            prompt = build_external_ai_prompt(
                job=working_job, references=evidence, profile=profile, template=template,
                document_type=doc_type, provider=ai_choice,
            )
            st.session_state.update({
                "external_ai_prompt": prompt,
                "external_ai_provider": ai_choice,
                "external_document_type_snapshot": doc_type,
                "external_job_snapshot": working_job,
                "external_template_snapshot": template,
            })

    if st.session_state.get("external_ai_prompt"):
        st.markdown('<div class="cv-section-head"><span class="cv-section-number">04</span><div><b>Your prompt is ready</b><span>Copy it, open the selected AI, and create the finished PDF there.</span></div></div>', unsafe_allow_html=True)
        prompt_value = st.session_state["external_ai_prompt"]
        prompt_html = html.escape(prompt_value, quote=True)
        components.html(
            f"""<div style="font-family:Arial,sans-serif;width:100%;">
            <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px;">
              <button id="copyPrompt" style="border:0;background:#ef4444;color:#fff;border-radius:8px;padding:7px 12px;font-weight:700;cursor:pointer;">Copy prompt</button>
              <span id="copyStatus" style="font-size:11px;color:#63e6a3;"></span>
            </div>
            <textarea id="promptBox" readonly style="box-sizing:border-box;width:100%;height:118px;resize:vertical;background:#0d0f12;color:#f5f7fa;border:1px solid #252a31;border-radius:9px;padding:9px;font:11px/1.38 Consolas,monospace;">{prompt_html}</textarea>
            <script>
            const btn=document.getElementById('copyPrompt'),box=document.getElementById('promptBox'),status=document.getElementById('copyStatus');
            btn.addEventListener('click',async()=>{{try{{await navigator.clipboard.writeText(box.value);status.textContent='Copied ✓';}}catch(e){{box.focus();box.select();document.execCommand('copy');status.textContent='Copied ✓';}}setTimeout(()=>status.textContent='',1400);}});
            </script></div>""",
            height=164,
        )
        provider = st.session_state.get("external_ai_provider", ai_choice)
        ai_urls = {"ChatGPT":"https://chatgpt.com/", "Claude":"https://claude.ai/", "Gemini":"https://gemini.google.com/"}
        st.link_button(f"Open {provider} ↗", ai_urls.get(provider, ai_urls["ChatGPT"]), width="stretch")
        st.caption("Create the document in the selected AI, then download its final PDF. JobSync cannot fetch a file directly from another AI website.")

        st.markdown('<div class="cv-section-head"><span class="cv-section-number">05</span><div><b>Upload the finished PDF</b><span>Only the final PDF is kept in your JobSync folder.</span></div></div>', unsafe_allow_html=True)
        pdf_upload = st.file_uploader("Finished PDF", type=["pdf"], key=f"final_pdf_upload_{cv_cycle}")
        if pdf_upload is not None:
            selected_pdf_bytes = pdf_upload.getvalue()
            st.download_button("Download PDF", selected_pdf_bytes, file_name=Path(pdf_upload.name).name, mime="application/pdf", width="stretch")
            st.caption("Optional: download another local copy before saving it to Folders.")

        if pdf_upload is not None and st.button("Save PDF & open Folders", type="primary", width="stretch"):
            try:
                current_doc_type = st.session_state.get("external_document_type_snapshot", doc_type)
                saved_job = st.session_state.get("external_job_snapshot", working_job)
                out_folder = OUTPUT_CV if current_doc_type == "CV" else OUTPUT_CL
                final_pdf_dir = out_folder / "final_pdf"
                final_pdf_dir.mkdir(parents=True, exist_ok=True)
                pdf_target = unique_doc_path(final_pdf_dir, f"{saved_job.get('title') or 'Document'}_{saved_job.get('company') or ''}_{current_doc_type}_final", ".pdf")
                pdf_target.write_bytes(pdf_upload.getbuffer())
                kind = "generated_cv" if current_doc_type == "CV" else "generated_coverletter"
                created_at = datetime.now().isoformat(timespec="seconds")
                record = {
                    "name": pdf_target.name,
                    "display_name": Path(pdf_target.name).stem,
                    "kind": kind,
                    "path": str(pdf_target),
                    "pdf_path": str(pdf_target),
                    "pdf_text_path": "",
                    "job_url": saved_job.get("url", ""),
                    "job_title": saved_job.get("title", ""),
                    "company": saved_job.get("company", ""),
                    "location": saved_job.get("location", ""),
                    "position": saved_job.get("title", ""),
                    "provider": st.session_state.get("external_ai_provider", ai_choice),
                    "created_at": created_at,
                    "pdf_uploaded_at": created_at,
                    "prompt_ready": bool(st.session_state.get("external_ai_prompt")),
                }
                state.setdefault("documents", []).append(record)
                save_state(state)
                maybe_library_copy(record)
                save_state(state)
                clear_temporary_cv_references()
                for transient_key in ("external_ai_prompt", "external_ai_provider", "external_document_type_snapshot", "external_job_snapshot", "external_template_snapshot"):
                    st.session_state.pop(transient_key, None)
                st.session_state["cv_studio_cycle"] = cv_cycle + 1
                st.session_state["sidebar_collapsed"] = False
                if current_doc_type == "CV":
                    st.session_state["nav"] = "Folders"
                    create_cv_library_backup("generated-cv")
                else:
                    st.session_state["nav"] = "CV & Cover Letter"
                st.rerun()
            except Exception as exc:
                notify_error(f"Could not store the final PDF: {exc}")

elif page == "Folders":
    st.markdown('<div class="mh-page-hero"><div class="mh-page-kicker">JOBSYNC • CV FOLDER</div><div class="mh-page-title">Your CV library, organised in one place.</div><div class="mh-page-copy">Upload CV files, rename them, assign the position they belong to, open them directly, remove old versions, and keep an automatic local backup of the whole library.</div><div class="mh-page-meta"><span class="mh-meta-chip"><span class="mh-meta-dot"></span> ONE LOCAL FOLDER</span><span class="mh-meta-chip"><span class="mh-meta-dot green"></span> EDITABLE METADATA</span><span class="mh-meta-chip"><span class="mh-meta-dot blue"></span> AUTOMATIC BACKUPS</span></div></div>', unsafe_allow_html=True)

    library_path = cv_library_location()
    sync_cv_library()

    # Upload area: multiple files keeps the workflow compatible with existing Streamlit versions.
    st.markdown('<div class="card" style="padding:18px;margin-bottom:14px"><div style="font-weight:850;font-size:1.05rem">Add CVs to the folder</div><div style="color:#9da8b7;font-size:.82rem;margin-top:3px">Select one or more PDF, DOCX, LaTeX or TXT files. JobSync copies every file into the single local CV library and records its metadata.</div></div>', unsafe_allow_html=True)
    upload_col1, upload_col2 = st.columns([4, 1])
    with upload_col1:
        folder_upload = st.file_uploader("Upload CV files", type=["pdf", "docx", "tex", "txt"], accept_multiple_files=True, key="folder_cv_uploads_v2")
    with upload_col2:
        st.write("")
        st.write("")
        if st.button("Add to folder", type="primary", width="stretch", key="folders_upload_cv_v2"):
            count = 0
            for uploaded in folder_upload or []:
                # Save first in the normal managed upload area, then place an organised library copy.
                target = unique_doc_path(UPLOAD_CV, Path(uploaded.name).stem, Path(uploaded.name).suffix or ".bin")
                target.write_bytes(uploaded.getbuffer())
                try:
                    text = extract_text(target).strip()
                except Exception:
                    text = ""
                display_name = Path(uploaded.name).stem
                library_target = library_path / f"{safe_name(display_name)}{Path(uploaded.name).suffix or '.bin'}"
                counter = 2
                while library_target.exists():
                    library_target = library_path / f"{safe_name(display_name)}_{counter}{Path(uploaded.name).suffix or '.bin'}"
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
                backup = create_cv_library_backup("upload")
                notify_success(f"Added {count} CV(s). Backup created: {backup.name}")
                st.rerun()
            else:
                notify_error("Choose at least one CV file first.")

    # Library summary + backup controls
    current_docs = sorted(cv_document_records(), key=lambda d: str(d.get("created_at") or ""), reverse=True)
    latest_backup = Path(str(state.get("settings", {}).get("cv_last_backup_path") or ""))
    summary_a, summary_b, summary_c = st.columns(3)
    with summary_a:
        st.metric("Saved CVs", len(current_docs))
    with summary_b:
        st.metric("Folder files", len([p for p in library_path.iterdir() if p.is_file()]))
    with summary_c:
        st.metric("Backups", int(state.get("settings", {}).get("cv_backup_count", 0)))

    st.markdown(f'<div class="card" style="padding:14px 16px;margin:8px 0 16px"><div style="font-size:.68rem;color:#7f8a99;text-transform:uppercase;letter-spacing:.12em;font-weight:900">CV folder location</div><div style="margin-top:5px;color:#dce2e9;font-family:Consolas,monospace;font-size:.8rem;word-break:break-all">{html.escape(str(library_path.resolve()))}</div><div style="display:flex;gap:8px;align-items:center;margin-top:8px;color:#8f9aaa;font-size:.74rem">{html.escape("Latest backup: " + (str(latest_backup.resolve()) if latest_backup.exists() else "Not created yet"))}</div></div>', unsafe_allow_html=True)
    backup_c1, backup_c2 = st.columns([1, 3])
    with backup_c1:
        if st.button("Create backup now", width="stretch", key="folders_backup_now"):
            try:
                backup = create_cv_library_backup("manual")
                notify_success(f"Backup created: {backup.name}")
                st.rerun()
            except Exception as exc:
                notify_error(f"Backup failed: {exc}")
    with backup_c2:
        if latest_backup.exists():
            st.download_button("Download latest backup ZIP", latest_backup.read_bytes(), file_name=latest_backup.name, mime="application/zip", width="stretch", key="download_latest_cv_backup")

    st.markdown(f'<div style="display:flex;justify-content:space-between;align-items:end;margin:10px 0 8px"><div><div class="section-title" style="margin-bottom:0">CV library</div><div class="muted">{len(current_docs)} document(s) · edit the position and CV name directly below</div></div></div>', unsafe_allow_html=True)

    if not current_docs:
        st.info("Your CV folder is empty. Upload a CV above or generate one from CV & Cover Letter.")
    else:
        header = st.columns([2.6, 1.35, 2.7, 1.0, 1.15, 1.0])
        for col, title in zip(header, ("Position", "Date applied", "CV name", "Type", "Open", "Delete")):
            with col:
                st.markdown(f'<div style="color:#7f8a99;font-size:.68rem;font-weight:900;letter-spacing:.12em;text-transform:uppercase;padding:0 .25rem .45rem">{title}</div>', unsafe_allow_html=True)

        for idx, doc in enumerate(current_docs):
            inferred_position, inferred_date = cv_position_and_date(doc)
            position_value = str(doc.get("job_title") or inferred_position or "General CV")
            name_value = str(doc.get("display_name") or Path(str(doc.get("path") or "CV")).stem)
            applied_value = str(doc.get("date_applied") or inferred_date or "")[:10]
            type_label = cv_kind_label(doc)
            path_value = str(doc.get("library_path") or doc.get("path") or "")
            open_target = str(doc.get("pdf_path") or path_value)
            open_link = open_file_anchor(open_target, "Open")

            d1, d2, d3, d4, d5, d6 = st.columns([2.6, 1.35, 2.7, 1.0, 1.15, 1.0], vertical_alignment="center")
            with d1:
                edited_position = st.text_input("Position", position_value, key=f"cvfolder_position_{idx}", label_visibility="collapsed")
            with d2:
                edited_date = st.text_input("Date applied", applied_value, key=f"cvfolder_date_{idx}", label_visibility="collapsed", placeholder="YYYY-MM-DD")
            with d3:
                edited_name = st.text_input("CV name", name_value, key=f"cvfolder_name_{idx}", label_visibility="collapsed")
            with d4:
                badge_bg = "rgba(34,197,94,.13)" if type_label == "Generated" else "rgba(239,68,68,.12)" if type_label == "Uploaded" else "rgba(245,158,11,.12)"
                badge_border = "rgba(34,197,94,.42)" if type_label == "Generated" else "rgba(239,68,68,.42)" if type_label == "Uploaded" else "rgba(245,158,11,.42)"
                st.markdown(f'<span style="display:inline-block;padding:5px 8px;border:1px solid {badge_border};background:{badge_bg};border-radius:999px;font-size:.66rem;font-weight:850">{html.escape(type_label)}</span>', unsafe_allow_html=True)
            with d5:
                if open_link:
                    st.markdown(open_link, unsafe_allow_html=True)
                else:
                    actual_path = Path(str(doc.get("path") or ""))
                    st.download_button("Open / DL", actual_path.read_bytes() if actual_path.exists() else b"", file_name=Path(str(actual_path)).name or "CV", key=f"folder_download_v2_{idx}_{safe_name(name_value)}", width="stretch", disabled=not actual_path.exists())
            with d6:
                if st.button("Delete", key=f"folder_delete_v2_{idx}_{safe_name(name_value)}", width="stretch"):
                    remove_document(doc)
                    try:
                        create_cv_library_backup("delete")
                    except Exception:
                        pass
                    st.rerun()

            save_col, spacer = st.columns([1, 5])
            with save_col:
                if st.button("Save changes", key=f"folder_save_v2_{idx}_{safe_name(name_value)}", width="stretch"):
                    old_display = str(doc.get("display_name") or Path(str(doc.get("path") or "CV")).stem)
                    doc["job_title"] = edited_position.strip()
                    doc["display_name"] = edited_name.strip() or old_display
                    doc["date_applied"] = edited_date.strip()
                    # Rename the organised library copy when the user changes the CV name.
                    source_library = Path(str(doc.get("library_path") or ""))
                    if source_library.exists():
                        suffix = source_library.suffix or Path(str(doc.get("path") or "CV")).suffix or ".tex"
                        new_library = library_path / f"{safe_name(doc['display_name'])}{suffix}"
                        counter = 2
                        while new_library.exists() and new_library.resolve() != source_library.resolve():
                            new_library = library_path / f"{safe_name(doc['display_name'])}_{counter}{suffix}"
                            counter += 1
                        if new_library.resolve() != source_library.resolve():
                            source_library.rename(new_library)
                        doc["library_path"] = str(new_library)
                    else:
                        target = maybe_library_copy(doc)
                        if target is not None:
                            doc["library_path"] = str(target)
                    save_state(state)
                    try:
                        backup = create_cv_library_backup("metadata update")
                        notify_success(f"Saved changes and backed up the folder: {backup.name}")
                    except Exception as exc:
                        notify_success("Saved changes.")
                        notify_error(f"Backup failed: {exc}")
                    st.rerun()
            st.markdown('<div style="height:1px;background:#20252c;margin:.45rem 0 .8rem"></div>', unsafe_allow_html=True)

    # Keep the backup information at the bottom as requested.
    st.markdown('<div class="card" style="padding:16px;margin-top:14px"><div class="section-title" style="margin-bottom:6px">Folder backup</div><div class="muted">The complete CV library and its metadata are backed up locally in a timestamped ZIP after uploads and metadata changes.</div></div>', unsafe_allow_html=True)
    final_backup = Path(str(state.get("settings", {}).get("cv_last_backup_path") or ""))
    if final_backup.exists():
        st.code(str(final_backup.resolve()), language="text")
        st.caption(f"Last backup created: {state.get('settings', {}).get('cv_last_backup_at','')}")
    else:
        st.caption(f"Backup location: {CV_BACKUP_DIR.resolve()} · No backup has been created yet.")


elif page == "Profile":
    avatar_seed = profile.get("email") or account_email() or profile.get("name") or "job-tracker-user"
    st.markdown(f'<div style="display:flex;align-items:center;gap:14px;margin-bottom:1rem;">{avatar_html(avatar_seed,80)}<div><div style="font-size:1.2rem;font-weight:800;color:#f5f7fa">Your human avatar</div><div style="color:#98a2b3">Automatically generated locally for your account.</div></div></div>', unsafe_allow_html=True)
    st.markdown('<div class="page-title">Profile</div><div class="page-subtitle">Keep the details JobSync uses for searches and document generation.</div>', unsafe_allow_html=True)
    with st.form("profile_form"):
        c1, c2 = st.columns(2)
        with c1:
            profile["name"] = st.text_input("Full name", profile.get("name", ""))
            profile["email"] = st.text_input("Email", profile.get("email", ""))
            profile["phone"] = st.text_input("Phone", profile.get("phone", ""))
        with c2:
            profile["city"] = st.text_input("Home city", profile.get("city", ""))
            titles_text = st.text_input("Target job titles", ", ".join(profile.get("target_titles", [])))
            profile["field"] = st.text_input("Main field", profile.get("field", ""))
        c3, c4 = st.columns(2)
        with c3:
            profile["industry"] = st.text_input("Industry", profile.get("industry", ""))
        with c4:
            profile["location"] = st.text_input("Search location", profile.get("location", profile.get("city", "")))
        experience_options_profile = ["Any", "Internship", "Entry level", "Associate", "Mid-Senior level", "Director"]
        current_exp_profile = profile.get("experience", "Any") if profile.get("experience", "Any") in experience_options_profile else "Any"
        profile["experience"] = st.selectbox("Experience", experience_options_profile, index=experience_options_profile.index(current_exp_profile))
        language_options_profile = ["English", "German", "French", "Spanish", "Italian", "Dutch", "Any"]
        current_language_profile = profile.get("language", "Any") if profile.get("language", "Any") in language_options_profile else "Any"
        profile["language"] = st.selectbox("Required language for job search", language_options_profile, index=language_options_profile.index(current_language_profile))
        st.caption("Any is the default. Choose a language only when you want to filter by an explicit requirement. Other languages marked as an advantage are shown on job cards.")
        submitted = st.form_submit_button("Save profile", type="primary", width="stretch")
    if submitted:
        profile["target_titles"] = [x.strip() for x in titles_text.split(",") if x.strip()]
        save_state(state)
        st.session_state.nav = "Dashboard"
        st.rerun()

    st.divider()
    st.subheader("Delete profile data")
    st.caption("This removes your saved name, contact details, location, target roles and search preferences. Your applications, jobs and documents are kept.")
    st.warning("This cannot be undone from the dashboard.")
    if st.button("Delete saved profile", type="primary", key="delete_profile"):
        state["profile"] = json.loads(json.dumps(DEFAULT_STATE["profile"]))
        save_state(state)
        notify_success("Profile data deleted locally.")
        st.rerun()

# ---------------- ADMIN PANEL ----------------
elif page == "Admin Panel":
    current_email = st.session_state.get("auth_email") or account_email()
    current_role = ensure_local_admin()
    if ROLE_ACCESS.get(current_role, 0) < ROLE_ACCESS["moderator"]:
        st.error("You do not have access to the administration panel.")
        st.stop()

    st.markdown('<div class="mh-page-hero"><div class="mh-page-kicker">JOBSYNC • ADMINISTRATION</div><div class="mh-page-title">Workspace control center.</div><div class="mh-page-copy">Manage members, names, roles and workspace access. Admins can also create new program sections from Settings.</div></div>', unsafe_allow_html=True)

    users = list_users()
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">🛡 Member management</div>', unsafe_allow_html=True)
    if not users:
        st.info("No members are registered in the local roster.")
    for user in users:
        email_u = str(user.get("email") or "").strip().lower()
        role_u = normalize_role(user.get("role"))
        name_u = str(user.get("display_name") or "User")
        blocked_u = bool(user.get("blocked"))
        st.markdown(f'<div class="card" style="margin:.55rem 0;padding:.8rem 1rem"><b>{html.escape(name_u)}</b> · {html.escape(email_u)} · {_role_badge(role_u)} ' + ('<span class="jobsync-role-badge" style="color:#ff8e96">BLOCKED</span>' if blocked_u else '') + '</div>', unsafe_allow_html=True)
        if current_role == "admin":
            c1, c2, c3, c4 = st.columns([1.15, 1, 1, .9])
            with c1:
                new_name = st.text_input("Account name", value=name_u, key=f"admin_name_{safe_name(email_u,'user')}")
            with c2:
                new_role = st.selectbox("Role", list(ROLE_OPTIONS), index=list(ROLE_OPTIONS).index(role_u), key=f"admin_role_{safe_name(email_u,'user')}")
            with c3:
                new_email = st.text_input("Account email", value=email_u, disabled=True, key=f"admin_email_{safe_name(email_u,'user')}")
            with c4:
                st.write("")
                save_member = st.button("Save", key=f"admin_save_{safe_name(email_u,'user')}", width="stretch")
            a1, a2 = st.columns(2)
            with a1:
                toggle_label = "Unblock member" if blocked_u else "Kick / block"
                toggle_btn = st.button(toggle_label, key=f"admin_block_{safe_name(email_u,'user')}", width="stretch", type="secondary")
            with a2:
                remove_btn = st.button("Remove member", key=f"admin_remove_{safe_name(email_u,'user')}", width="stretch")
            if save_member:
                try:
                    if new_role != "admin" and email_u == current_email:
                        notify_error("You cannot remove your own admin role.")
                    else:
                        update_user(email_u, display_name=new_name, role=new_role)
                        if email_u == current_email:
                            state["profile"]["name"] = new_name
                            save_state(state)
                        notify_success("Member updated.")
                        st.rerun()
                except Exception as exc:
                    notify_error(str(exc))
            if toggle_btn:
                if email_u == current_email:
                    notify_error("You cannot block your own account.")
                else:
                    update_user(email_u, blocked=not blocked_u)
                    notify_success("Member access updated.")
                    st.rerun()
            if remove_btn:
                if email_u == current_email:
                    notify_error("You cannot remove your own account from the roster.")
                else:
                    remove_user(email_u)
                    notify_success("Member removed from the JobSync roster.")
                    st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)

    if current_role == "admin":
        st.markdown('<div class="card">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">➕ Add member to workspace roster</div>', unsafe_allow_html=True)
        st.caption("This registers a member in the workspace role roster. It does not create a Windows or local login account; the member still needs an account on their own installation.")
        with st.form("admin_add_member"):
            a1, a2, a3 = st.columns([2,2,1])
            with a1: member_email = st.text_input("Member email")
            with a2: member_name = st.text_input("Account name")
            with a3: member_role = st.selectbox("Role", list(ROLE_OPTIONS), index=list(ROLE_OPTIONS).index("member"))
            if st.form_submit_button("Add / update member", type="primary", width="stretch"):
                try:
                    record = set_user_role(member_email, member_role, member_name)
                    notify_success(f"{record['email']} is now {record['role']}.")
                    st.rerun()
                except Exception as exc:
                    notify_error(str(exc))
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">👤 Your account</div>', unsafe_allow_html=True)
    with st.form("admin_account_form"):
        account_name = st.text_input("Account name", value=str(profile.get("name") or "User"))
        account_login = st.text_input("Login email", value=current_email)
        if st.form_submit_button("Save account details", type="primary", width="stretch"):
            try:
                old_email = current_email
                new_email = account_login.strip().lower()
                if new_email != old_email:
                    update_account_email(new_email)
                    update_user(old_email, display_name=account_name)
                    remove_user(old_email)
                    ensure_user(new_email, account_name, default_role="admin")
                    st.session_state.auth_email = new_email
                else:
                    update_user(old_email, display_name=account_name)
                state["profile"]["name"] = account_name.strip()
                state["profile"]["email"] = new_email
                save_state(state)
                notify_success("Account details saved.")
                st.rerun()
            except Exception as exc:
                notify_error(str(exc))
    st.markdown('</div>', unsafe_allow_html=True)

elif page == "Settings":
    current_role = ensure_local_admin()
    is_admin = current_role == "admin"
    st.markdown('<div class="page-title">Settings</div><div class="page-subtitle">Personal and workspace settings. Administrator-only controls are marked clearly.</div>', unsafe_allow_html=True)

    if is_admin:
        st.markdown('<div class="card">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">🧩 Program sections</div>', unsafe_allow_html=True)
        st.caption("Admins can add lightweight custom sections that appear in the left navigation. They can be used for future tools, notes or workflows without editing the source code.")
        sections = custom_sections()
        if sections:
            for item in sections:
                c1,c2,c3=st.columns([2,3,1])
                with c1: st.write(f"**{item['icon']} {item['name']}**")
                with c2: st.caption(item.get('description') or 'No description')
                with c3:
                    if st.button("Delete", key=f"delete_custom_{safe_name(item['name'],'section')}"):
                        state.setdefault("settings", {})["custom_sections"] = [x for x in sections if x["name"] != item["name"]]
                        save_state(state); notify_success("Section removed."); st.rerun()
        with st.form("add_custom_section"):
            sc1, sc2, sc3 = st.columns([2,3,1])
            with sc1: custom_name = st.text_input("Section name", placeholder="Interview Notes")
            with sc2: custom_desc = st.text_input("Description", placeholder="Keep interview notes and preparation in one place")
            with sc3: custom_icon = st.text_input("Icon", value="▣", max_chars=4)
            if st.form_submit_button("Add section", type="primary", width="stretch"):
                cleaned = custom_name.strip()
                if not cleaned:
                    notify_error("Enter a section name.")
                elif cleaned in BASE_PAGES or cleaned in {x["name"] for x in sections}:
                    notify_error("That section name is already in use.")
                else:
                    state.setdefault("settings", {}).setdefault("custom_sections", []).append({"name": cleaned[:50], "description": custom_desc.strip()[:500], "icon": (custom_icon.strip() or "▣")[:4]})
                    save_state(state); notify_success("Section added to the navigation."); st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        st.write("")

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">🔎 Job sources</div>', unsafe_allow_html=True)
    saved_mode = state.get("settings", {}).get("job_search_mode", "free")
    if saved_mode not in JOB_SEARCH_MODE_LABELS:
        saved_mode = "free"
    settings_mode_label = st.selectbox(
        "Default job search method",
        options=list(JOB_SEARCH_MODES.keys()),
        index=list(JOB_SEARCH_MODES.values()).index(saved_mode),
        disabled=not is_admin,
        help="Admins can change the workspace-wide search method.",
    )
    settings_search_mode = JOB_SEARCH_MODES[settings_mode_label]
    free_sources_setting = st.multiselect(
        "Free/public sources",
        options=FREE_SOURCE_NAMES,
        default=state.get("settings", {}).get("free_sources") or FREE_SOURCE_NAMES,
        disabled=not is_admin,
        help="Workspace-wide search sources. Admin only."
    )
    ats_urls_setting_text = st.text_area(
        "Company ATS career URLs (one per line)",
        value="\n".join(state.get("settings", {}).get("ats_urls") or []),
        placeholder="https://company.wd5.myworkdayjobs.com/Careers\nhttps://boards.greenhouse.io/company",
        disabled=not is_admin,
        help="Workspace-wide ATS endpoints. Admin only."
    )
    ats_urls_setting = [x.strip() for x in ats_urls_setting_text.splitlines() if x.strip()]
    if settings_search_mode == "free":
        st.info("Free APIs & public sources selected. Searches from this installation will not use Apify.")
    configured_ids = state.get("settings", {}).get("actor_ids") or [ACTOR_CATALOG[name]["id"] for name in DEFAULT_ACTOR_NAMES]
    configured_names = [ACTOR_ID_TO_NAME.get(x, x) for x in configured_ids]
    if settings_search_mode in {"apify", "both"}:
        selected_names = st.multiselect("Default Apify Actors", options=list(ACTOR_CATALOG.keys()), default=[x for x in configured_names if x in ACTOR_CATALOG], disabled=not is_admin)
        for name, meta in ACTOR_CATALOG.items():
            if name in selected_names:
                st.caption(f"{name} — {meta['pricing']} — {meta['note']}")
    else:
        selected_names = configured_names
        st.caption("Apify Actor selection is ignored while Free mode is selected.")
    apify_token = st.text_input("Apify API token", value=os.getenv("APIFY_TOKEN", ""), type="password", disabled=not is_admin)
    st.markdown('</div>', unsafe_allow_html=True)

    st.write("")
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">in LinkedIn profile & notifications</div>', unsafe_allow_html=True)
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

    st.write("")
    st.write("")
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">🔔 Daily new-job monitoring</div>', unsafe_allow_html=True)
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


    if is_admin:
        # Shared presence configuration. The shipped default uses a Supabase
        # publishable key, which is safe for a desktop/client app when the table
        # is protected by RLS. Never put a Supabase secret/service-role key here.
        with st.expander("👥 Users Online / Supabase presence", expanded=False):
            st.caption(
                "JobSync is already connected to the shared Supabase project. "
                "The right-side Users Online panel shows active users across all "
                "installations using the same project. These fields are only needed "
                "if you want to override the built-in project configuration."
            )
            supabase_url = st.text_input(
                "Supabase project URL",
                value=os.getenv("SUPABASE_URL", ""),
                placeholder="https://your-project.supabase.co",
                key="supabase_url_setting",
            )
            supabase_anon_key = st.text_input(
                "Supabase publishable key",
                value=os.getenv("SUPABASE_PUBLISHABLE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", ""),
                type="password",
                placeholder="sb_publishable_...",
                help="Use the project's publishable key. Never use a secret/service-role key.",
                key="supabase_publishable_key_setting",
            )
            st.caption(
                "One-time database setup: run supabase_presence.sql in the Supabase SQL Editor."
            )

    if is_admin:
        # Google OAuth is application-level configuration. It is intentionally kept
        # out of the Gmail user workflow: normal users only see "Connect Gmail".
        with st.expander("🔐 Google OAuth application setup (owner only)", expanded=False):
            st.caption(
                "Configure this once for this JobSync installation. Normal users will not need to see or enter these values; "
                "they will only click Connect Gmail and complete Google's login page."
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

    st.write("")
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">⬆ Software updates</div>', unsafe_allow_html=True)
    st.caption("Updates are checked only when you press the button. The universal GitHub updater checks the published releases, downloads a matching ZIP into the github folder, and never runs during application startup.")
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
                        ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(updater_path), "-InstallDir", str(BASE_DIR), "-ConfigPath", str(cfg_path)],
                        cwd=str(BASE_DIR),
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    output_text = (completed.stdout or completed.stderr or "").strip()
                    if completed.returncode != 0:
                        notify_error(f"Update check failed: {output_text[-800:] or 'unknown error'}")
                    else:
                        try:
                            result_path = updater_root / "last-update-check.json"
                            result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
                            if result.get("downloaded"):
                                notify_success(f"New release v{result.get('latest_version')} downloaded to {result.get('download_path')}.")
                            else:
                                notify_success(f"You are up to date (v{result.get('current_version', APP_VERSION)}).")
                        except Exception:
                            notify_success(output_text[-800:] or "Update check completed.")
                except Exception as exc:
                    notify_error(f"Could not run the updater: {exc}")
                st.rerun()
    with update_col2:
        updater_root = _find_github_updater_root()
        result_path = (updater_root / "last-update-check.json") if updater_root else None
        if result_path and result_path.exists():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
                checked = result.get("checked_at", "")
                latest = result.get("latest_version", "")
                dl = result.get("download_path") or ""
                if result.get("downloaded"):
                    st.caption(f"Latest: v{latest} · downloaded: {dl} · checked: {checked}")
                else:
                    st.caption(f"Latest checked: v{latest} · checked: {checked}")
            except Exception:
                st.caption("No successful update check recorded yet.")
        else:
            st.caption("No manual GitHub update check has been run yet.")
    st.markdown('</div>', unsafe_allow_html=True)

    if st.button("Save settings", type="primary", width="stretch"):
        settings = state.setdefault("settings", {})
        # Personal settings are available to every user; workspace-wide
        # provider/source settings and secrets are admin-controlled.
        settings["linkedin_profile_url"] = linkedin_profile_url.strip()
        if is_admin:
            settings["actor_ids"] = [ACTOR_CATALOG[name]["id"] for name in selected_names if name in ACTOR_CATALOG]
            settings["job_search_mode"] = settings_search_mode
            settings["free_sources"] = free_sources_setting
            settings["ats_urls"] = ats_urls_setting
            settings["live_monitor_enabled"] = bool(monitor_enabled)
            settings["monitor_interval_hours"] = 24
            content = "\n".join([
                f"APIFY_TOKEN={apify_token}",
                f"SUPABASE_URL={supabase_url.strip()}",
                f"SUPABASE_PUBLISHABLE_KEY={supabase_anon_key.strip()}",
                f"SUPABASE_ANON_KEY={supabase_anon_key.strip()}",
                "",
            ])
            ENV_FILE.write_text(content, encoding="utf-8")
            load_dotenv(ENV_FILE, override=True)
        save_state(state)
        notify_success("Settings saved locally.")
        st.rerun()

    st.write("")
    st.caption("CV and cover-letter generation uses your own external AI account. JobSync stores no AI API credentials.")
    st.caption(f"Generated CV folder: {OUTPUT_CV}")
    st.caption(f"Generated cover-letter folder: {OUTPUT_CL}")
    st.caption(f"Excel tracker: {TRACKER}")
    st.write("")
    st.divider()
    st.subheader("⚠ Master reset")
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



# Custom administrator-created section pages.
if st.session_state.get("authenticated") and page in {x["name"] for x in custom_sections()}:
    item = next(x for x in custom_sections() if x["name"] == page)
    st.markdown(
        f'<div class="mh-page-hero"><div class="mh-page-kicker">JOBSYNC • CUSTOM SECTION</div>'
        f'<div class="mh-page-title">{html.escape(item["name"])}</div>'
        f'<div class="mh-page-copy">{html.escape(item.get("description") or "Custom workspace section created by an administrator.")}</div></div>',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="card"><div class="section-title">Ready for your workflow</div><div class="muted">This section has been added by an administrator and is ready to be connected to a future JobSync feature.</div></div>', unsafe_allow_html=True)
