from __future__ import annotations

from datetime import datetime
from pathlib import Path

from services.app_paths import BASE_DIR as _PACKAGED_BASE_DIR
from services.gmail import gmail_configured, sync_gmail
from services.notifications import desktop_notify
from services.storage import load_state, save_state

_DEV_BASE = Path(__file__).resolve().parents[2]
BASE_DIR = _PACKAGED_BASE_DIR if _PACKAGED_BASE_DIR else _DEV_BASE
LOG_FILE = BASE_DIR / "data" / "status_agent.log"

# The agent never applies a status change on its own -- it only stages a
# suggestion for the user to confirm or dismiss with one click. This is the
# minimum match confidence worth surfacing as a ready-to-confirm suggestion
# at all (anything below still appears on the Gmail Updates page as before).
SUGGEST_CONFIDENCE = 0.55
STALE_FOLLOWUP_DAYS = 14

STATUS_MAP = {
    "Application received": "Applied",
    "Interview": "Interview",
    "Offer": "Offer",
    "Rejected": "Rejected",
    "Assessment": "Shortlisted",
}
# Never let a generic auto-matched email knock an application further along
# the pipeline back down a stage.
STATUS_RANK = {"Applied": 0, "Shortlisted": 1, "Interview": 2, "Offer": 3, "Rejected": 3}


def _log(message: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(f"[{stamp}] {message}\n")


def _log_activity(state: dict, kind: str, text: str) -> None:
    feed = state.setdefault("assistant_activity", [])
    feed.insert(0, {
        "agent": "Application status",
        "kind": kind,
        "text": text,
        "at": datetime.now().isoformat(timespec="seconds"),
    })
    state["assistant_activity"] = feed[:250]


def _check_email_updates(state: dict) -> int:
    """Auto-sync Gmail and stage confident matches as ready-to-confirm suggestions.

    Never writes application['status'] itself -- the agent's job ends at
    preparing the suggestion. Applying it is always a deliberate click by the
    user, on the Applied Jobs card or the Gmail Updates page.
    """
    settings = state.setdefault("settings", {})
    if not gmail_configured() or not settings.get("gmail_email"):
        return 0
    applications = state.get("applied", [])
    if not applications:
        return 0
    try:
        updates = sync_gmail(applications, days=7, max_messages=30)
    except Exception as exc:
        _log(f"Gmail sync failed: {exc}")
        return 0

    settings["gmail_last_sync"] = datetime.now().isoformat(timespec="seconds")
    state["gmail_updates"] = updates

    pending = state.setdefault("assistant_pending_status_changes", [])
    already_pending = {(p.get("application_index"), p.get("suggested_status")) for p in pending}
    new_count = 0
    for update in updates:
        matched = update.get("matched_application_index")
        confidence = float(update.get("match_confidence") or 0)
        if matched is None or matched >= len(applications) or confidence < SUGGEST_CONFIDENCE:
            continue
        suggested = STATUS_MAP.get(update.get("status", ""), "")
        if not suggested:
            continue
        app_row = applications[matched]
        current = app_row.get("status", "Applied")
        if suggested == current:
            continue
        if STATUS_RANK.get(suggested, 0) < STATUS_RANK.get(current, 0):
            # Don't even suggest downgrading an application that's already
            # further along the pipeline -- a generic email is too weak
            # evidence for that regardless of confirmation.
            continue
        if (matched, suggested) in already_pending:
            continue
        pending.append({
            "application_index": matched,
            "suggested_status": suggested,
            "confidence": confidence,
            "subject": update.get("subject", ""),
            "prepared_at": datetime.now().isoformat(timespec="seconds"),
        })
        already_pending.add((matched, suggested))
        new_count += 1
        title = app_row.get("title", "a role")
        company = app_row.get("company", "")
        text = f"{title} at {company}: found a likely status update to {suggested} — ready to confirm"
        _log_activity(state, "status_suggestion", text)
        try:
            desktop_notify("JobSync: update ready to review", text)
        except Exception:
            pass
    state["assistant_pending_status_changes"] = pending[-200:]
    return new_count


def _check_stale_applications(state: dict) -> int:
    """Flag applications with no movement in a while so the user can follow up."""
    applications = state.get("applied", [])
    now = datetime.now()
    flagged = 0
    for app_row in applications:
        if app_row.get("status") not in ("Applied", None, ""):
            continue
        if app_row.get("_followup_flagged"):
            continue
        applied_raw = str(app_row.get("applied_date") or "")
        try:
            applied_dt = datetime.strptime(applied_raw, "%Y-%m-%d")
        except Exception:
            continue
        if (now - applied_dt).days < STALE_FOLLOWUP_DAYS:
            continue
        app_row["_followup_flagged"] = True
        flagged += 1
        title = app_row.get("title", "a role")
        company = app_row.get("company", "")
        text = f"{title} at {company}: no update in {STALE_FOLLOWUP_DAYS}+ days — consider following up"
        _log_activity(state, "follow_up", text)
    if flagged:
        try:
            desktop_notify("JobSync: follow-up suggested", f"{flagged} application(s) may need a follow-up.")
        except Exception:
            pass
    return flagged


def run_once() -> dict:
    """Run both checks once and persist the results. Safe to call from the UI or a background loop."""
    state = load_state()
    settings = state.setdefault("settings", {})
    if not settings.get("application_status_agent_enabled", True):
        _log("Application status agent disabled; skipping.")
        return {"enabled": False}

    new_suggestions = _check_email_updates(state)
    stale = _check_stale_applications(state)

    settings["status_agent_last_check"] = datetime.now().isoformat(timespec="seconds")
    settings["status_agent_last_email_updates"] = new_suggestions
    settings["status_agent_last_stale_flags"] = stale
    settings.pop("status_agent_last_error", None)
    save_state(state)
    _log(f"Checked: {new_suggestions} new status suggestions staged, {stale} new follow-up flags.")
    return {"enabled": True, "email_updates": new_suggestions, "stale_flags": stale}
