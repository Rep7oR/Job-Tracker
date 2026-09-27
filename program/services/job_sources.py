"""Fetches job postings from public, unauthenticated ATS job-board APIs.

Two sources are supported, both well-documented public JSON endpoints that
require no API key:

  * Greenhouse job boards: ``https://boards-api.greenhouse.io/v1/boards/{slug}/jobs``
    Returns ``{"jobs": [{"id", "title", "location": {"name"}, "absolute_url",
    "updated_at", "content", ...}, ...]}``.
  * Lever postings: ``https://api.lever.co/v0/postings/{slug}?mode=json``
    Returns a bare JSON list of ``{"text", "categories": {"location", ...},
    "hostedUrl", "createdAt", "descriptionPlain"/"description", ...}``.

NOTE ON VERIFICATION: outbound HTTPS to boards-api.greenhouse.io and
api.lever.co was tested live from this sandbox and rejected by the outbound
network proxy (CONNECT -> 403, policy denial), so no live response was
observed here. The request construction and JSON parsing below follow the
field shapes documented publicly for these two APIs (they are widely used
and stable), but that shape has NOT been independently re-verified in this
environment. If/when network access allows it, run ``fetch_postings("stripe",
"greenhouse")`` and inspect the result to confirm before relying on this in
production.

Every posting is normalized to:
    {"title", "company", "location", "url", "posted_date", "description", "source"}
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

# A starter list of real companies known to use Greenhouse or Lever as their
# public job board, so there is something to search against immediately
# after profile setup (no separate "add a company" step).
SEEDED_SOURCES: list[dict[str, str]] = [
    {"company": "Stripe", "slug": "stripe", "provider": "greenhouse"},
    {"company": "Figma", "slug": "figma", "provider": "greenhouse"},
    {"company": "Notion", "slug": "notion", "provider": "greenhouse"},
    {"company": "Airbnb", "slug": "airbnb", "provider": "greenhouse"},
    {"company": "Robinhood", "slug": "robinhood", "provider": "greenhouse"},
    {"company": "Coinbase", "slug": "coinbase", "provider": "greenhouse"},
    {"company": "Asana", "slug": "asana", "provider": "greenhouse"},
    {"company": "Reddit", "slug": "reddit", "provider": "greenhouse"},
    {"company": "Netflix", "slug": "netflix", "provider": "lever"},
    {"company": "Plaid", "slug": "plaid", "provider": "lever"},
    {"company": "Rippling", "slug": "rippling", "provider": "lever"},
    {"company": "Attentive", "slug": "attentive", "provider": "lever"},
]

_TIMEOUT_SECONDS = 15
_USER_AGENT = "JobSync/1.0 (+phase2 job-source fetcher)"


def _http_get_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        return json.load(response)


def _normalize_greenhouse(company: str, raw: dict) -> dict:
    location = ""
    loc = raw.get("location")
    if isinstance(loc, dict):
        location = loc.get("name", "") or ""
    return {
        "title": raw.get("title", "") or "",
        "company": company,
        "location": location,
        "url": raw.get("absolute_url", "") or "",
        "posted_date": raw.get("updated_at", "") or "",
        "description": raw.get("content", "") or "",
        "source": "greenhouse",
    }


def _normalize_lever(company: str, raw: dict) -> dict:
    categories = raw.get("categories") or {}
    location = categories.get("location", "") or ""
    description = raw.get("descriptionPlain") or raw.get("description") or ""
    return {
        "title": raw.get("text", "") or "",
        "company": company,
        "location": location,
        "url": raw.get("hostedUrl", "") or "",
        "posted_date": str(raw.get("createdAt", "") or ""),
        "description": description,
        "source": "lever",
    }


def fetch_postings(slug: str, provider: str, company: str | None = None) -> list[dict]:
    """Fetch and normalize open postings for one company/source.

    Returns an empty list (never raises) on any network or parsing failure,
    so a single bad/unreachable source doesn't take down a full agent run.
    """
    company = company or slug
    try:
        if provider == "greenhouse":
            url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
            data = _http_get_json(url)
            jobs = data.get("jobs", []) if isinstance(data, dict) else []
            return [_normalize_greenhouse(company, job) for job in jobs]
        if provider == "lever":
            url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
            data = _http_get_json(url)
            postings = data if isinstance(data, list) else []
            return [_normalize_lever(company, posting) for posting in postings]
        return []
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return []


def fetch_all_postings(
    sources: list[dict[str, str]] | None = None,
) -> list[dict]:
    """Fetch and normalize postings from every seeded source (or a given list).

    Failures on individual sources are swallowed (see ``fetch_postings``) so
    that partial network availability still yields whatever succeeded.
    """
    sources = sources if sources is not None else SEEDED_SOURCES
    all_postings: list[dict] = []
    for source in sources:
        all_postings.extend(
            fetch_postings(source["slug"], source["provider"], source.get("company"))
        )
    return all_postings
