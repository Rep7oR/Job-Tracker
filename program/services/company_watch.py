from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeoutError
from pathlib import Path

from services.app_paths import BASE_DIR as _PACKAGED_BASE_DIR
from services.free_job_sources import ats_from_urls

_DEV_BASE = Path(__file__).resolve().parents[2]
BASE_DIR = _PACKAGED_BASE_DIR if _PACKAGED_BASE_DIR else _DEV_BASE
# A local, shared, ever-growing cache of "company name -> known career board
# URL". Every company any local account successfully resolves (by guessed
# slug or a manually pasted URL) is written here, so the NEXT time anyone on
# this machine types that name it resolves instantly with no network call
# and no re-validation -- "just the name" as requested. This is separate
# from each account's own watched_companies list (state.settings), which is
# "which of these am I actively tracking".
CACHE_FILE = BASE_DIR / "data" / "company_career_boards.json"

# Predictable public career-board URL patterns for the ATS platforms
# free_job_sources.py already knows how to parse. Workday is deliberately
# excluded here -- its subdomain (wd1, wd3, wd5...) isn't guessable from a
# company name, so a Workday board can only be added by pasting the URL
# (or by already being in SEED_BOARDS / the local cache below).
_CANDIDATE_TEMPLATES = [
    ("Greenhouse", "https://boards.greenhouse.io/{slug}"),
    ("Lever", "https://jobs.lever.co/{slug}"),
    ("Ashby", "https://jobs.ashbyhq.com/{slug}"),
    ("SmartRecruiters", "https://careers.smartrecruiters.com/{slug}"),
    ("Workable", "https://apply.workable.com/{slug}"),
]

# A best-effort starter set of well-known employers' public career boards,
# shipped with the app so common names resolve instantly on a fresh
# install with zero network calls -- before any user has ever looked them
# up. These are not guaranteed current (a company can move ATS providers),
# which is exactly why every add still runs through the same live
# validation as a freshly-guessed URL before it's saved to the watchlist:
# a stale seed just fails validation and falls through to the normal
# guess-then-ask-for-a-URL flow instead of silently misleading anyone.
SEED_BOARDS: dict[str, dict] = {
    "airbnb": {"url": "https://careers.airbnb.com", "source": "Custom"},
    "airbus": {"url": "https://ag.wd3.myworkdayjobs.com/Airbus", "source": "Workday"},
    "asana": {"url": "https://boards.greenhouse.io/asana", "source": "Greenhouse"},
    "coinbase": {"url": "https://www.coinbase.com/careers/positions", "source": "Custom"},
    "datadog": {"url": "https://careers.datadoghq.com", "source": "Custom"},
    "discord": {"url": "https://job-boards.greenhouse.io/discord", "source": "Greenhouse"},
    "dropbox": {"url": "https://jobs.lever.co/dropbox", "source": "Lever"},
    "duolingo": {"url": "https://boards.greenhouse.io/duolingo", "source": "Greenhouse"},
    "figma": {"url": "https://job-boards.greenhouse.io/figma", "source": "Greenhouse"},
    "github": {"url": "https://github.com/about/careers", "source": "Custom"},
    "gitlab": {"url": "https://job-boards.greenhouse.io/gitlab", "source": "Greenhouse"},
    "netflix": {"url": "https://jobs.lever.co/netflix", "source": "Lever"},
    "notion": {"url": "https://job-boards.greenhouse.io/notion", "source": "Greenhouse"},
    "openai": {"url": "https://jobs.ashbyhq.com/openai", "source": "Ashby"},
    "pinterest": {"url": "https://www.pinterestcareers.com", "source": "Custom"},
    "reddit": {"url": "https://boards.greenhouse.io/reddit", "source": "Greenhouse"},
    "revolut": {"url": "https://www.revolut.com/careers", "source": "Custom"},
    "robinhood": {"url": "https://careers.robinhood.com", "source": "Custom"},
    "shopify": {"url": "https://www.shopify.com/careers", "source": "Custom"},
    "siemens": {"url": "https://siemens.wd3.myworkdayjobs.com/SiemensCareers", "source": "Workday"},
    "snowflake": {"url": "https://careers.snowflake.com", "source": "Custom"},
    "spotify": {"url": "https://www.lifeatspotify.com/jobs", "source": "Custom"},
    "stripe": {"url": "https://stripe.com/jobs/search", "source": "Custom"},
    "twilio": {"url": "https://www.twilio.com/en-us/company/jobs", "source": "Custom"},
}

# A company's real board (especially Workday, and big Greenhouse/Lever
# boards) can have thousands of postings and paginate slowly. We only need
# enough to confirm the URL is real and show an approximate count, not the
# full list -- fetch_company_jobs()/the background monitor pull the complete
# set later. Every network call here is wrapped in a hard wall-clock
# timeout so a slow/unresponsive board can never hang the UI: Streamlit
# runs this synchronously on the main thread, and Windows has no SIGALRM,
# so a thread-pool future with .result(timeout=...) is the only clean
# cross-platform way to bound it.
_VALIDATE_TIMEOUT_SECONDS = 18
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="company_watch")


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.strip().lower())


def _load_cache() -> dict:
    try:
        if CACHE_FILE.exists():
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_to_cache(company_name: str, url: str, source: str) -> None:
    key = _normalize(company_name)
    if not key:
        return
    try:
        cache = _load_cache()
        cache[key] = {"display_name": company_name.strip(), "url": url, "source": source}
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # the cache is a pure speed optimization; never let it break adding a company


def _ats_from_urls_bounded(urls: list[str], search_text: str = "", timeout: float = _VALIDATE_TIMEOUT_SECONDS):
    """ats_from_urls(), but abandoned (not killed -- Python can't kill a
    running thread) if it doesn't finish within `timeout` seconds, so the
    caller's UI never hangs waiting on a slow or huge career board.
    """
    future = _executor.submit(ats_from_urls, urls, search_text)
    try:
        return future.result(timeout=timeout)
    except _FutureTimeoutError:
        return [], [f"Timed out after {timeout:.0f}s waiting for a response."]
    except Exception as exc:
        return [], [str(exc)]


def _slugs_for(company_name: str) -> list[str]:
    name = company_name.strip().lower()
    compact = re.sub(r"[^a-z0-9]+", "", name)
    hyphenated = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    slugs = []
    for candidate in (hyphenated, compact):
        if candidate and candidate not in slugs:
            slugs.append(candidate)
    return slugs


def resolve_company(company_name: str) -> dict | None:
    """Resolve a company name to its career board, fastest path first:

    1. This machine's own learned cache (instant, no network -- anything
       anyone has successfully added before, on any local account).
    2. The bundled seed list of well-known employers (instant, no network).
    3. Guessed ATS URL patterns, each live-validated (network, bounded).

    A cache/seed hit is still re-validated live before being returned, so a
    company that has switched ATS providers since being cached fails
    gracefully instead of adding a dead board.

    Returns {"name", "url", "source", "job_count"} on success, else None.
    """
    company_name = company_name.strip()
    if not company_name:
        return None
    key = _normalize(company_name)

    known = _load_cache().get(key) or SEED_BOARDS.get(key)
    if known:
        rows, _errors = _ats_from_urls_bounded([known["url"]])
        if rows:
            _save_to_cache(company_name, known["url"], known["source"])
            return {
                "name": company_name,
                "url": known["url"],
                "source": known["source"],
                "job_count": len(rows),
            }
        # Known URL went stale (ATS migration, board taken down, etc.) --
        # fall through to guessing instead of dead-ending here.

    slugs = _slugs_for(company_name)
    for source, template in _CANDIDATE_TEMPLATES:
        for slug in slugs:
            url = template.format(slug=slug)
            rows, _errors = _ats_from_urls_bounded([url])
            if rows:
                _save_to_cache(company_name, url, source)
                return {
                    "name": company_name,
                    "url": url,
                    "source": source,
                    "job_count": len(rows),
                }
    return None


def fetch_company_jobs(url: str, search_text: str = "") -> tuple[list[dict], list[str]]:
    """Re-fetch current postings for one already-added watched company URL.

    Uses a longer timeout than validation since this is a deliberate,
    single-company on-demand refresh (the '🔎 View jobs' button), not a
    multi-candidate lookup loop.
    """
    return _ats_from_urls_bounded([url], search_text=search_text, timeout=45)


def _detect_source(url: str) -> str:
    low = url.lower()
    if "myworkdayjobs.com" in low:
        return "Workday"
    if "greenhouse.io" in low:
        return "Greenhouse"
    if "lever.co" in low:
        return "Lever"
    if "ashbyhq.com" in low:
        return "Ashby"
    if "smartrecruiters.com" in low:
        return "SmartRecruiters"
    if "workable.com" in low:
        return "Workable"
    if "personio" in low:
        return "Personio"
    return "Custom"


def resolve_manual_url(company_name: str, url: str) -> dict | None:
    """Validate a career page URL the user pasted in directly and, if it
    returns real postings, package it the same way resolve_company() does
    -- and cache it, so the next person who just types this company's name
    never has to paste a URL again.
    """
    url = url.strip()
    company_name = company_name.strip() or _detect_source(url)
    if not url:
        return None
    # Workday boards in particular can be large/slow -- give manual-URL
    # validation (a single deliberate check, not a multi-candidate loop) a
    # bit more room than the auto-guess loop before giving up.
    rows, _errors = _ats_from_urls_bounded([url], timeout=30)
    if not rows:
        return None
    source = _detect_source(url)
    _save_to_cache(company_name, url, source)
    return {
        "name": company_name,
        "url": url,
        "source": source,
        "job_count": len(rows),
    }
