from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from services.jobs import search_jobs
from services.notifications import desktop_notify
from services.storage import load_state, save_state

BASE_DIR = Path(__file__).resolve().parents[2]
PROGRAM_DIR = BASE_DIR / "program"
# Job Tracker root is the directory that contains program/, data/, uploads/, etc.
ENV_FILE = BASE_DIR / ".env"
LOG_FILE = BASE_DIR / "data" / "monitor.log"
INTERVAL_SECONDS = 24 * 60 * 60


def _log(message: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(f"[{stamp}] {message}\n")


def _job_key(job: dict) -> str:
    url = str(job.get("url") or "").strip().lower()
    if url:
        return "url:" + url
    job_id = str(job.get("id") or "").strip().lower()
    if job_id:
        return "id:" + job_id
    return "text:" + "|".join(str(job.get(k) or "").strip().lower() for k in ("title", "company", "location"))


def _search_profile(state: dict) -> tuple[str, str, str, str, str]:
    profile = state.setdefault("profile", {})
    history = state.get("search_history") or []
    last = history[-1] if history else {}
    # The monitor follows the LAST JOB SEARCH, not the user's personal profile.
    field = str(last.get("field") or profile.get("field") or "").strip()
    industry = str(last.get("industry") or profile.get("industry") or "").strip()
    location = str(last.get("location") or profile.get("location") or profile.get("city") or "").strip()
    experience = str(last.get("experience") or profile.get("experience") or "Any")
    language = str(last.get("language") or profile.get("language") or "Any")
    return field, location, industry, experience, language


def _monitor_once() -> bool:
    load_dotenv(ENV_FILE, override=True)
    state = load_state()
    settings = state.setdefault("settings", {})

    if not settings.get("live_monitor_enabled", True):
        _log("Live monitor disabled; next check will retry after 24 hours.")
        return False

    field, location, industry, experience, language = _search_profile(state)
    if not field and not industry and not location:
        _log("No saved job search/profile configured; nothing to monitor.")
        return False

    mode = str(settings.get("job_search_mode") or "free").strip().lower()
    actor_ids = list(settings.get("actor_ids") or [])
    free_sources = list(settings.get("free_sources") or [])
    ats_urls = list(settings.get("ats_urls") or [])

    # Search a small overlap window every day. This prevents jobs missed during a
    # temporary source failure from being lost, while seen-key deduplication keeps
    # notifications limited to genuinely new jobs.
    date_window_days = 3
    try:
        jobs = search_jobs(
            field=field, location=location, industry=industry,
            experience=experience, language=language,
            limit=10000, actor_ids=actor_ids, date_window_days=date_window_days,
            search_mode=mode, free_sources=free_sources, ats_urls=ats_urls,
        )
    except Exception as exc:
        settings["monitor_last_check"] = datetime.now().isoformat(timespec="seconds")
        settings["monitor_last_error"] = str(exc)
        save_state(state)
        _log(f"Search failed: {exc}")
        return False

    current_keys = {_job_key(job) for job in jobs}
    seen = set(settings.get("monitor_seen_keys") or [])

    # Baseline on the first successful run. Use a flag separate from the key list
    # so an empty result does not silently mark monitoring as healthy.
    if not settings.get("monitor_seeded"):
        if not current_keys:
            settings["monitor_last_check"] = datetime.now().isoformat(timespec="seconds")
            settings["monitor_last_new_count"] = 0
            settings.pop("monitor_last_error", None)
            save_state(state)
            _log("First successful search returned 0 jobs; baseline not seeded yet.")
            return True
        settings["monitor_seen_keys"] = sorted(current_keys)[-10000:]
        settings["monitor_seeded"] = True
        settings["monitor_last_check"] = datetime.now().isoformat(timespec="seconds")
        settings["monitor_last_new_count"] = 0
        settings["monitor_last_result_count"] = len(jobs)
        settings.pop("monitor_last_error", None)
        save_state(state)
        _log(f"Baseline seeded with {len(current_keys)} jobs using {mode} mode.")
        return True

    new_jobs = [job for job in jobs if _job_key(job) not in seen]
    settings["monitor_seen_keys"] = sorted(seen | current_keys)[-10000:]
    settings["monitor_last_check"] = datetime.now().isoformat(timespec="seconds")
    settings["monitor_last_new_count"] = len(new_jobs)
    settings["monitor_last_result_count"] = len(jobs)
    settings.pop("monitor_last_error", None)

    if new_jobs:
        existing = state.get("jobs") or []
        existing_keys = {_job_key(job) for job in existing}
        additions = [job for job in new_jobs if _job_key(job) not in existing_keys]
        state["jobs"] = additions + existing
        # Keep the full result set from growing without bound. The monitor itself
        # has no artificial 30-job limit.
        state["jobs"] = state["jobs"][:10000]

        history = state.setdefault("monitor_notifications", [])
        now = datetime.now().isoformat(timespec="seconds")
        for job in new_jobs[:50]:
            history.insert(0, {
                "title": job.get("title", "New job"), "company": job.get("company", ""),
                "location": job.get("location", ""), "source": job.get("source", ""),
                "url": job.get("url", ""), "detected_at": now,
            })
        state["monitor_notifications"] = history[:250]

        preview = "\n".join(f"• {job.get('title','New job')} — {job.get('company','') or 'Unknown company'}" for job in new_jobs[:5])
        extra = f"\n+ {len(new_jobs) - 5} more" if len(new_jobs) > 5 else ""
        desktop_notify(f"JobSync: {len(new_jobs)} new job(s)", preview + extra)
        _log(f"Found {len(new_jobs)} new jobs from {len(jobs)} monitored results.")
    else:
        _log(f"No new jobs found ({len(jobs)} results checked).")

    save_state(state)
    return True


def run_forever() -> None:
    _log("Live job monitor started.")
    # Run immediately when the monitor process starts. It no longer waits 24 hours
    # for its first check. After that, run every 24 hours.
    while True:
        try:
            _monitor_once()
        except Exception as exc:
            _log(f"Unexpected monitor error: {exc}")
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
