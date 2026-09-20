from __future__ import annotations

import re

from services.free_job_sources import ats_from_urls

# Predictable public career-board URL patterns for the ATS platforms
# free_job_sources.py already knows how to parse. Workday is deliberately
# excluded here -- its subdomain (wd1, wd3, wd5...) isn't guessable from a
# company name, so a Workday board can only be added by pasting the URL.
_CANDIDATE_TEMPLATES = [
    ("Greenhouse", "https://boards.greenhouse.io/{slug}"),
    ("Lever", "https://jobs.lever.co/{slug}"),
    ("Ashby", "https://jobs.ashbyhq.com/{slug}"),
    ("SmartRecruiters", "https://careers.smartrecruiters.com/{slug}"),
    ("Workable", "https://apply.workable.com/{slug}"),
]


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
    """Try known ATS URL patterns for a company name; return the first one
    that actually returns job postings, or None if nothing matched.

    Returns {"name", "url", "source", "job_count"} on success.
    """
    company_name = company_name.strip()
    if not company_name:
        return None
    slugs = _slugs_for(company_name)
    if not slugs:
        return None

    for source, template in _CANDIDATE_TEMPLATES:
        for slug in slugs:
            url = template.format(slug=slug)
            try:
                rows, errors = ats_from_urls([url])
            except Exception:
                continue
            if rows:
                return {
                    "name": company_name,
                    "url": url,
                    "source": source,
                    "job_count": len(rows),
                }
    return None


def fetch_company_jobs(url: str, search_text: str = "") -> tuple[list[dict], list[str]]:
    """Re-fetch current postings for one already-added watched company URL."""
    return ats_from_urls([url], search_text=search_text)


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
    returns real postings, package it the same way resolve_company() does.
    """
    url = url.strip()
    company_name = company_name.strip() or _detect_source(url)
    if not url:
        return None
    try:
        rows, errors = ats_from_urls([url])
    except Exception:
        return None
    if not rows:
        return None
    return {
        "name": company_name,
        "url": url,
        "source": _detect_source(url),
        "job_count": len(rows),
    }
