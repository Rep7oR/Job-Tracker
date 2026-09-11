from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import quote_plus, urlencode

import requests
from bs4 import BeautifulSoup


ACTOR_CATALOG = {
    "Indeed Jobs Scraper": {
        "id": "valig/indeed-jobs-scraper",
        "pricing": "low-cost / try for free",
        "note": "Good first source for a small free-plan search.",
    },
    "LinkedIn Jobs Scraper — automation-lab": {
        "id": "automation-lab/linkedin-jobs-scraper",
        "pricing": "low-cost / try for free",
        "note": "Public guest API; no LinkedIn login required according to the Actor page.",
    },
    "LinkedIn Jobs Scraper — curious_coder": {
        "id": "curious_coder/linkedin-jobs-scraper",
        "pricing": "try for free",
        "note": "Community LinkedIn scraper; input schema is based on search URLs/filters.",
    },
    "Google Jobs Scraper": {
        "id": "johnvc/google-jobs-scraper",
        "pricing": "try for free",
        "note": "Google Jobs source with query and location inputs.",
    },
    "Glassdoor Jobs Scraper": {
        "id": "valig/glassdoor-jobs-scraper",
        "pricing": "low-cost / try for free",
        "note": "Glassdoor source; useful as a second source after the first search works.",
    },
    "Jobs Scraper — Indeed, LinkedIn & Glassdoor": {
        "id": "khadinakbar/jobs-scraper",
        "pricing": "try for free",
        "note": "Multi-board source; can search multiple platforms in one Actor run.",
    },
}

ACTOR_ID_TO_NAME = {v["id"]: k for k, v in ACTOR_CATALOG.items()}
DEFAULT_ACTOR_NAMES = ["Indeed Jobs Scraper"]


def _published_date(item: dict):
    for key in (
        "postedAtTimestamp", "publishedAt", "published_at", "datePosted", "date_posted",
        "postedAt", "posted_at", "postedTime", "date", "createdAt", "created_at", "publication_date", "epoch", "aktuelleVeroeffentlichungsdatum", "scrapedAt",
    ):
        value = item.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, (int, float)):
            try:
                ts = value / 1000 if value > 10_000_000_000 else value
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except Exception:
                continue
        if isinstance(value, str):
            s = value.strip()
            try:
                parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed
            except Exception:
                pass
            lower = s.lower()
            m = re.search(r"(\d+)\s*(?:day|days)\s*ago", lower)
            if m:
                return datetime.now(timezone.utc) - timedelta(days=int(m.group(1)))
            m = re.search(r"(\d+)\s*(?:hour|hours)\s*ago", lower)
            if m:
                return datetime.now(timezone.utc) - timedelta(hours=int(m.group(1)))
            if "today" in lower or "just" in lower:
                return datetime.now(timezone.utc)
    return None


def _first(item, keys, default=""):
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return default


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(_text(x) for x in value if _text(x))
    if isinstance(value, dict):
        for key in ("name", "label", "text", "value"):
            if value.get(key):
                return _text(value[key])
        return str(value)
    return str(value)


def _common_query(field: str, industry: str) -> str:
    return " ".join(x.strip() for x in (field, industry) if x and x.strip()) or "jobs"


def _language_profile(text: str, preferred_language: str) -> tuple[bool, list[str], str]:
    """Classify a job's language requirement using explicit posting language phrases.

    Returns (required_match, advantages, evidence).
    This intentionally uses conservative rules: absence of a language statement is not
    treated as proof that the language is required.
    """
    if not preferred_language or preferred_language.lower() == "any":
        return True, [], ""

    lang = preferred_language.strip()
    lower = text.lower()

    aliases = {
        "English": ["english"],
        "German": ["german", "deutsch"],
        "French": ["french", "französisch", "francais", "français"],
        "Spanish": ["spanish", "español"],
        "Italian": ["italian", "italiano"],
        "Dutch": ["dutch", "nederlands"],
    }
    needles = aliases.get(lang, [lang.lower()])

    required_patterns = [
        r"\bfluent in\s+(?:[a-zà-ÿ-]+\s+){0,2}{LANG}\b",
        r"\b(?:very good|excellent|strong|professional|business fluent|proficient) (?:in )?{LANG}\b",
        r"\b{LANG}\s+(?:is\s+)?(?:required|mandatory|essential|a must)\b",
        r"\b(?:required|mandatory|essential|must(?:\s+be)?\s+have)\b[^.\n]{0,50}\b{LANG}\b",
        r"\b{LANG}\b[^.\n]{0,60}\b(?:required|mandatory|essential|must|proficient|fluent)\b",
    ]
    advantage_patterns = [
        r"\b{LANG}\b[^.\n]{0,60}\b(?:plus|advantage|preferred|desirable|nice to have|asset)\b",
        r"\b(?:plus|advantage|preferred|desirable|nice to have|asset)\b[^.\n]{0,60}\b{LANG}\b",
    ]

    def contains_required(needle: str) -> bool:
        safe = re.escape(needle)
        return any(re.search(p.replace("{LANG}", safe), lower, flags=re.IGNORECASE) for p in required_patterns)

    def contains_advantage(needle: str) -> bool:
        safe = re.escape(needle)
        return any(re.search(p.replace("{LANG}", safe), lower, flags=re.IGNORECASE) for p in advantage_patterns)

    required = any(contains_required(n) for n in needles)
    advantages: list[str] = []
    # Check the common European languages as advantages so an English-required job
    # can surface German/French/etc. as "advantage" without making them mandatory.
    for label, vals in aliases.items():
        if label == lang:
            continue
        if any(contains_advantage(v) for v in vals):
            advantages.append(label)

    evidence = ""
    if required:
        evidence = f"{lang} appears explicitly required/proficiency-related in the posting."
    elif any(re.search(rf"\b{re.escape(n.lower())}\b", lower) for n in needles):
        evidence = f"{lang} is mentioned in the posting, but a required-language phrase was not detected."

    return required, advantages, evidence


def _apply_language_filter(rows: list[dict], preferred_language: str) -> list[dict]:
    if not preferred_language or preferred_language.lower() == "any":
        for row in rows:
            row["language_required"] = ""
            row["language_advantages"] = []
            row["language_match"] = "Not filtered"
        return rows

    filtered: list[dict] = []
    for row in rows:
        raw_language = _text(_first(row.get("raw", {}), [
            "languageRequirements", "languageRequirementsText", "languages", "language", "requiredLanguages"
        ], ""))
        text = " ".join(
            part for part in [
                row.get("title", ""),
                row.get("description", ""),
                raw_language,
            ] if part
        )
        required, advantages, evidence = _language_profile(text, preferred_language)
        row["language_required"] = preferred_language if required else ""
        row["language_advantages"] = advantages
        row["language_match"] = "Required" if required else "Not confirmed"
        row["language_evidence"] = evidence
        if required:
            filtered.append(row)
    return filtered




FREE_SOURCE_TIMEOUT = int(os.getenv("JOB_TRACKER_FREE_SOURCE_TIMEOUT", "20") or "20")
FREE_USER_AGENT = os.getenv(
    "JOB_TRACKER_FREE_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
)


def _http_get(url: str, *, params: dict | None = None, headers: dict | None = None) -> requests.Response:
    merged = {"User-Agent": FREE_USER_AGENT, "Accept": "application/json, text/html;q=0.9, */*;q=0.8"}
    if headers:
        merged.update(headers)
    response = requests.get(url, params=params, headers=merged, timeout=FREE_SOURCE_TIMEOUT)
    response.raise_for_status()
    return response


def _free_query_terms(field: str, industry: str) -> list[str]:
    terms = []
    for value in (field, industry):
        value = (value or "").strip()
        if value and value.lower() not in {x.lower() for x in terms}:
            terms.append(value)
    if not terms:
        terms = ["engineer"]
    return terms


def _matches_free_job(row: dict, field: str, industry: str, location: str) -> bool:
    haystack = " ".join(
        _text(row.get(key, ""))
        for key in ("title", "company", "location", "description", "source")
    ).lower()
    field_terms = [x.lower() for x in _free_query_terms(field, "") if x.strip()]
    industry_terms = [x.lower() for x in _free_query_terms(industry, "") if x.strip()]
    # Title is the strongest signal. Industry is allowed anywhere in the posting.
    title = _text(row.get("title", "")).lower()
    field_match = any(term in title or term in haystack for term in field_terms)
    industry_match = not industry_terms or any(term in haystack for term in industry_terms)
    if not field_match or not industry_match:
        return False
    wanted_location = (location or "").strip().lower()
    if not wanted_location or wanted_location in {"germany", "deutschland", "remote"}:
        return True
    # Do not hard-reject a Germany search when a posting says Germany/Europe/Remote.
    loc = _text(row.get("location", "")).lower()
    return any(part in loc or part in haystack for part in [wanted_location, "germany", "deutschland", "remote", "europe", "eu"])


def _free_arbeitsagentur(field: str, location: str, industry: str, experience: str, date_window_days: int, limit: int) -> list[dict]:
    """Fetch directly from the official Bundesagentur für Arbeit Jobsuche API.

    This is intentionally independent of Apify. The API exposes a fixed public
    client id (X-API-Key: jobboerse-jobsuche) and supports title/location/age filters.
    """
    query = _common_query(field, industry)
    params = {
        "was": query,
        "wo": location or "Germany",
        "page": 1,
        "size": max(50, min(limit * 2, 100)),
        "veroeffentlichtseit": max(1, min(int(date_window_days), 100)),
        "angebotsart": 1,
        "zeitarbeit": "true",
    }
    if location and location.lower() not in {"germany", "deutschland"}:
        params["umkreis"] = 50

    response = _http_get(
        "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs",
        params=params,
        headers={"X-API-Key": "jobboerse-jobsuche"},
    )
    payload = response.json()
    items = payload.get("ergebnisliste") or payload.get("stellenangebote") or payload.get("data") or []
    if isinstance(items, dict):
        items = items.get("ergebnisse") or items.get("stellenangebote") or []

    rows: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ref = _text(_first(item, ["referenznummer", "refnr", "jobId"], ""))
        # v6 uses nested locations. Keep the raw object available to the normalizer.
        locs = item.get("stellenlokationen") or item.get("arbeitsort") or []
        if isinstance(locs, list):
            location_text = ", ".join(
                _text(_first(x, ["ort", "city", "arbeitsort"], ""))
                for x in locs if isinstance(x, dict)
            )
        elif isinstance(locs, dict):
            location_text = _text(_first(locs, ["ort", "city", "arbeitsort"], ""))
        else:
            location_text = _text(locs)
        item = dict(item)
        if location_text:
            item["location"] = location_text
        if ref:
            item["jobId"] = ref
            item["id"] = ref
        item["source"] = "Bundesagentur für Arbeit"
        dt = _published_date(item)
        row = _normalize_item(item, "Bundesagentur für Arbeit", industry, experience, dt)
        if row:
            # v6 exposes the employer and external application URL under different names.
            row["company"] = row["company"] or _text(_first(item, ["arbeitgeber", "arbeitgeberName", "employer"], ""))
            row["url"] = row["url"] or _text(_first(item, ["externeUrl", "externalUrl", "url"], ""))
            rows.append(row)

    # The search response may not contain full descriptions. Fetch details only for
    # the small set that survives the search so language filtering remains useful.
    for row in rows[: max(limit * 2, 20)]:
        ref = row.get("id") or ""
        if not ref:
            continue
        try:
            import base64
            encoded = base64.b64encode(str(ref).encode("utf-8")).decode("ascii")
            detail = _http_get(
                f"https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/{encoded}",
                headers={"X-API-Key": "jobboerse-jobsuche"},
            ).json()
            row["description"] = _text(_first(detail, ["stellenangebotsBeschreibung", "beschreibung", "description"], row.get("description", "")))
            row["salary"] = _text(_first(detail, ["verguetung", "salary"], row.get("salary", ""))) or row.get("salary", "")
            row["raw"]["detail"] = detail
            detail_date = _published_date(detail)
            if detail_date:
                row["posted_at"] = detail_date.isoformat()
                row["posted_date"] = detail_date.astimezone().strftime("%Y-%m-%d")
        except Exception:
            # Search results remain usable even when an individual detail call fails.
            continue
    return rows


def _free_arbeitnow(field: str, location: str, industry: str, experience: str, date_window_days: int, limit: int) -> list[dict]:
    rows: list[dict] = []
    max_pages = max(1, min(5, (limit * 2 + 19) // 20))
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(date_window_days))
    for page in range(1, max_pages + 1):
        payload = _http_get(
            "https://www.arbeitnow.com/api/job-board-api",
            params={"page": page},
        ).json()
        items = payload.get("data", []) if isinstance(payload, dict) else payload
        for item in items or []:
            if not isinstance(item, dict):
                continue
            raw = dict(item)
            raw["source"] = "Arbeitnow"
            dt = _published_date({"created_at": item.get("created_at")})
            if dt and dt < cutoff:
                continue
            loc = _text(item.get("location", ""))
            raw["location"] = loc
            row = _normalize_item(raw, "Arbeitnow", industry, experience, dt)
            if row and _matches_free_job(row, field, industry, location):
                rows.append(row)
            if len(rows) >= limit * 2:
                return rows
    return rows


def _free_remoteok(field: str, location: str, industry: str, experience: str, date_window_days: int, limit: int) -> list[dict]:
    payload = _http_get("https://remoteok.com/api").json()
    rows: list[dict] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(date_window_days))
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict) or not item.get("position"):
            continue
        raw = dict(item)
        raw["title"] = item.get("position")
        raw["company"] = item.get("company")
        raw["location"] = item.get("location") or "Remote"
        raw["url"] = item.get("url") or item.get("apply_url")
        raw["description"] = item.get("description") or ""
        raw["date"] = item.get("date") or item.get("epoch")
        raw["source"] = "Remote OK"
        dt = _published_date(raw)
        if dt and dt < cutoff:
            continue
        row = _normalize_item(raw, "Remote OK", industry, experience, dt)
        if row and _matches_free_job(row, field, industry, location):
            rows.append(row)
            if len(rows) >= limit * 2:
                break
    return rows


def _free_remotive(field: str, location: str, industry: str, experience: str, date_window_days: int, limit: int) -> list[dict]:
    # Remotive intentionally stays a supplementary source: its public API is delayed
    # by about 24 hours, so it is useful for coverage but not the primary "new today" feed.
    payload = _http_get("https://remotive.com/api/remote-jobs", params={"limit": min(100, max(20, limit * 2))}).json()
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    rows: list[dict] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(date_window_days))
    for item in jobs:
        raw = dict(item)
        raw["source"] = "Remotive"
        dt = _published_date({"publication_date": item.get("publication_date"), "date": item.get("date")})
        if dt and dt < cutoff:
            continue
        row = _normalize_item(raw, "Remotive", industry, experience, dt)
        if row and _matches_free_job(row, field, industry, location):
            rows.append(row)
            if len(rows) >= limit * 2:
                break
    return rows


def _free_linkedin_guest(field: str, location: str, industry: str, experience: str, date_window_days: int, limit: int) -> list[dict]:
    """Use LinkedIn's public guest job-search response without a login or Apify.

    This source is best-effort. LinkedIn can change the public HTML/endpoint at any
    time, so a failure is isolated and never prevents the other free sources.
    """
    params = {
        "keywords": _common_query(field, industry),
        "location": location or "Germany",
        "f_TPR": f"r{int(date_window_days) * 86400}",
        "start": 0,
    }
    response = _http_get(
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
        params=params,
        headers={"Accept": "text/html,application/xhtml+xml"},
    )
    soup = BeautifulSoup(response.text, "html.parser")
    rows: list[dict] = []
    cards = soup.select("li")
    for card in cards:
        title_el = card.select_one("h3")
        company_el = card.select_one("h4")
        location_el = card.select_one(".job-search-card__location")
        link_el = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
        time_el = card.select_one("time")
        if not title_el or not link_el:
            continue
        raw = {
            "title": title_el.get_text(" ", strip=True),
            "company": company_el.get_text(" ", strip=True) if company_el else "",
            "location": location_el.get_text(" ", strip=True) if location_el else "",
            "url": link_el.get("href", "").split("?")[0],
            "postedTime": time_el.get("datetime", "") if time_el else "",
            "posted": time_el.get_text(" ", strip=True) if time_el else "",
            "source": "LinkedIn",
        }
        dt = _published_date(raw)
        row = _normalize_item(raw, "LinkedIn", industry, experience, dt)
        if row:
            rows.append(row)
        if len(rows) >= limit * 2:
            break
    return rows


def _search_free_sources(field: str, location: str, industry: str, experience: str,
                          date_window_days: int, limit: int,
                          selected_sources: list[str] | None = None,
                          ats_urls: list[str] | None = None) -> tuple[list[dict], list[str]]:
    """Collect selected public/free sources. Never calls Apify."""
    from services.free_job_sources import collect_board_sources, ats_from_urls

    selected = selected_sources or [
        "Bundesagentur für Arbeit", "LinkedIn", "Indeed", "StepStone", "Monster",
        "Glassdoor", "Arbeitnow", "Remote OK", "Remotive"
    ]
    rows, failures = [], []

    # Existing direct public APIs.
    if "Bundesagentur für Arbeit" in selected:
        try:
            rows.extend(_free_arbeitsagentur(field, location, industry, experience, date_window_days, limit))
        except Exception as exc:
            failures.append(f"Bundesagentur für Arbeit: {exc}")
    if "Arbeitnow" in selected:
        try:
            rows.extend(_free_arbeitnow(field, location, industry, experience, date_window_days, limit))
        except Exception as exc:
            failures.append(f"Arbeitnow: {exc}")
    if "Remote OK" in selected:
        try:
            rows.extend(_free_remoteok(field, location, industry, experience, date_window_days, limit))
        except Exception as exc:
            failures.append(f"Remote OK: {exc}")
    if "Remotive" in selected:
        try:
            rows.extend(_free_remotive(field, location, industry, experience, date_window_days, limit))
        except Exception as exc:
            failures.append(f"Remotive: {exc}")

    # Direct public board pages: Indeed, StepStone, Monster, Glassdoor, LinkedIn.
    try:
        board_rows, board_errors = collect_board_sources(
            _common_query(field, industry), location, date_window_days,
            selected=selected, limit=max(limit, 30)
        )
        rows.extend(board_rows)
        failures.extend(board_errors)
    except Exception as exc:
        failures.append(f"Public board collectors: {exc}")

    # Company-specific ATS URLs (Workday and other public ATS APIs).
    ats_selected = {"Greenhouse", "Lever", "SmartRecruiters", "Workable", "Personio", "Ashby", "Workday"}
    if ats_urls and any(x in selected for x in ats_selected):
        filtered_urls = []
        for url in ats_urls:
            low_url = url.lower()
            # Match by actual ATS host patterns. In particular, normal Workday
            # URLs do not contain the word "Workday":
            # https://ag.wd3.myworkdayjobs.com/Airbus
            source_allowed = False
            if "Workday" in selected and "myworkdayjobs.com" in low_url:
                source_allowed = True
            if "Greenhouse" in selected and "greenhouse.io" in low_url:
                source_allowed = True
            if "Lever" in selected and "lever.co" in low_url:
                source_allowed = True
            if "Ashby" in selected and "ashbyhq.com" in low_url:
                source_allowed = True
            if "SmartRecruiters" in selected and "smartrecruiters.com" in low_url:
                source_allowed = True
            if "Workable" in selected and "workable.com" in low_url:
                source_allowed = True
            if "Personio" in selected and "personio" in low_url:
                source_allowed = True
            if not source_allowed:
                # Preserve support for custom/legacy URLs containing the ATS
                # name literally.
                source_allowed = any(name.lower() in low_url for name in ats_selected if name in selected)
            if source_allowed:
                filtered_urls.append(url)
        if filtered_urls:
            try:
                ats_rows, ats_errors = ats_from_urls(
                    filtered_urls,
                    search_text=field.strip(),
                )
                # Apply conservative local matching.  ATS boards are already
                # company-specific, so the industry text must NOT be required to
                # literally occur in every posting (e.g. Airbus + "airline").
                # Match the field/title/company first, then use location when it is
                # explicit.  This prevents valid Workday/Greenhouse jobs from being
                # discarded merely because the employer's industry label is not in
                # the posting text.
                field_terms = [x.strip().lower() for x in re.split(r"[,;/|]+", field or "") if x.strip()]
                wanted_location = (location or "").strip().lower()
                for row in ats_rows:
                    hay = " ".join(str(row.get(k) or "") for k in ("title", "company", "location", "description")).lower()
                    title_company = " ".join(str(row.get(k) or "") for k in ("title", "company")).lower()
                    field_ok = (not field_terms) or any(term in title_company or term in hay for term in field_terms)
                    if not field_ok:
                        continue
                    if wanted_location and wanted_location not in {"germany", "deutschland", "remote", "eu", "europe"}:
                        loc = str(row.get("location") or "").lower()
                        if not any(x in loc or x in hay for x in (wanted_location, "germany", "deutschland", "remote", "europe", "eu")):
                            continue
                    rows.append(row)
                failures.extend(ats_errors)
            except Exception as exc:
                failures.append(f"ATS collectors: {exc}")

    return rows, failures

def _linkedin_url(field: str, location: str, industry: str, date_window_days: int = 7) -> str:
    keywords = quote_plus(_common_query(field, industry))
    loc = quote_plus(location or "Germany")
    seconds = int(date_window_days) * 86400
    return f"https://www.linkedin.com/jobs/search/?keywords={keywords}&location={loc}&f_TPR=r{seconds}"


def build_actor_input(actor_id: str, field: str, location: str, industry: str, experience: str, limit: int, date_window_days: int = 7) -> dict:
    query = _common_query(field, industry)
    max_results = max(30, min(limit * 2, 90))

    if actor_id == "automation-lab/linkedin-jobs-scraper":
        data = {
            "searchQuery": query,
            "location": location or "Germany",
            "maxJobs": max_results,
            "datePosted": f"r{int(date_window_days) * 86400}",
            "scrapeJobDetails": True,
        }
        # automation-lab expects experienceLevel as a single string code, not a list.
        # Codes: 1 Internship, 2 Entry, 3 Associate, 4 Mid-Senior, 5 Director, 6 Executive.
        exp_codes = {
            "Any": "all",
            "Internship": "1",
            "Entry level": "2",
            "Associate": "3",
            "Mid-Senior level": "4",
            "Director": "5",
        }
        data["experienceLevel"] = exp_codes.get(experience, "all")
        data["datePosted"] = f"r{int(date_window_days) * 86400}"
        data["sortBy"] = "DD"
        return data

    if actor_id == "curious_coder/linkedin-jobs-scraper":
        return {"searchUrl": _linkedin_url(field, location, industry, date_window_days), "count": max_results}

    if actor_id == "johnvc/google-jobs-scraper":
        return {
            "query": field.strip() or query,
            "location": location or "Germany",
            "num_results": max_results,
            "max_pagination": max(1, min(6, (max_results + 9) // 10)),
            "output_file": "jobflow_google_jobs.json",
            "max_delay": 1,
        }

    if actor_id == "valig/indeed-jobs-scraper":
        # Current Actor releases have used search-query and location style fields.
        # validateInput below will surface a precise schema error if the Actor changes.
        return {
            "searchQuery": query,
            "location": location or "Germany",
            "maxJobs": max_results,
            "maxAge": int(date_window_days),
        }

    if actor_id == "valig/glassdoor-jobs-scraper":
        return {
            "searchQuery": query,
            "location": location or "Germany",
            "maxResults": max_results,
            "fromAge": int(date_window_days),
        }

    if actor_id == "khadinakbar/jobs-scraper":
        platforms = ["indeed", "linkedin", "glassdoor"]
        return {
            "searchQuery": query,
            "location": location or "Germany",
            "platforms": platforms,
            "maxResults": max_results,
            "hoursOld": int(date_window_days) * 24,
            "deduplicate": True,
        }

    raise ValueError(f"Unsupported configured job source: {actor_id}")


def _normalize_item(item: dict, source_name: str, industry: str, experience: str, dt) -> dict:
    title = _text(_first(item, ["title", "jobTitle", "position", "name", "job_title", "stellenangebotsTitel"]))
    company = _text(_first(item, ["companyName", "company", "employer", "company_name", "company_name_text", "arbeitgeber"]))
    loc = _text(_first(item, ["location", "jobLocation", "locations", "job_location"]))
    url = _text(_first(item, ["jobUrl", "url", "jobURL", "applyUrl", "apply_url", "link", "job_url", "applyLink"]))
    desc = _text(_first(item, ["description", "jobDescription", "descriptionText", "job_description", "stellenangebotsBeschreibung"]))
    posted_raw = _text(_first(item, ["postedTime", "posted", "datePosted", "date", "publishedAt"], ""))
    work_type = _text(_first(item, ["workType", "workplaceType", "workTypes", "workplace", "remoteType"], ""))
    contract_type = _text(_first(item, ["contractType", "employmentType", "contractTypes", "jobType"], ""))
    level = _text(_first(item, ["experienceLevel", "seniorityLevel", "seniority", "experience"], experience if experience != "Any" else ""))
    salary = _text(_first(item, ["salary", "salaryRaw", "salaryRange", "salarySnippet"], ""))
    source_field = _text(_first(item, ["platform", "source", "board", "site"], source_name)) or source_name

    if not title:
        return {}

    posted = dt.astimezone().strftime("%Y-%m-%d") if dt else (posted_raw or "Unknown")
    return {
        "id": _text(_first(item, ["id", "jobId", "job_id", "referenceNumber"], "")),
        "title": title,
        "company": company,
        "location": loc,
        "industry": industry,
        "experience": level,
        "work_type": work_type,
        "contract_type": contract_type,
        "salary": salary,
        "posted_date": posted,
        "posted_at": dt.isoformat() if dt else "",
        "url": url,
        "description": desc,
        "source": source_field,
        "actor": source_name,
        "raw": item,
    }


def _run_one_actor(client, actor_id: str, actor_name: str, field: str, location: str, industry: str, experience: str, limit: int, date_window_days: int = 7) -> list[dict]:
    run_input = build_actor_input(actor_id, field, location, industry, experience, limit, date_window_days)
    # Start the Actor and wait for it to finish.
    # Apify Client v3 returns a typed Run object, while older versions returned a dict.
    # Support both so JobFlow remains compatible with either client shape.
    result = client.actor(actor_id).call(run_input=run_input)
    if result is None:
        raise RuntimeError(f"{actor_name} did not return a completed run.")

    dataset_id = getattr(result, "default_dataset_id", None)
    if not dataset_id and isinstance(result, dict):
        dataset_id = result.get("default_dataset_id") or result.get("defaultDatasetId")
    if not dataset_id:
        raise RuntimeError(f"{actor_name} finished without a result dataset.")

    items = client.dataset(dataset_id).list_items().items
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=int(date_window_days))
    rows = []
    for item in items:
        dt = _published_date(item)
        if dt and dt < cutoff:
            continue
        row = _normalize_item(item, actor_name, industry, experience, dt)
        if row:
            rows.append(row)
    return rows


def search_jobs(
    field: str,
    location: str,
    industry: str,
    experience: str,
    language: str = "English",
    limit: int = 10000,
    actor_ids: list[str] | None = None,
    date_window_days: int | None = 7,
    search_mode: str | None = None,
    free_sources: list[str] | None = None,
    ats_urls: list[str] | None = None,
) -> list[dict]:
    """Search jobs using the selected collection method.

    Modes:
      free  - keyless/public APIs and public sources only (no Apify).
      apify - configured Apify Actors only.
      both  - free sources first, then Apify only when free results are insufficient.
    """
    if date_window_days is None:
        date_window_days = int(os.getenv("JOB_TRACKER_DATE_WINDOW_DAYS", "7") or "7")
    if not field and not location and not industry:
        raise RuntimeError("Enter at least a job field/title or a location before searching.")

    mode = (search_mode or os.getenv("JOB_TRACKER_SEARCH_MODE", "free")).strip().lower()
    if mode not in {"free", "apify", "both"}:
        mode = "free"

    all_rows: list[dict] = []
    failures: list[str] = []

    def deduplicate(rows: list[dict]) -> list[dict]:
        result: list[dict] = []
        seen: set[str] = set()
        for row in sorted(rows, key=lambda x: x.get("posted_at") or x.get("posted_date") or "", reverse=True):
            key = (row.get("url") or row.get("id") or "").strip().lower()
            if not key:
                key = (
                    _text(row.get("title", "")).lower(),
                    _text(row.get("company", "")).lower(),
                    _text(row.get("location", "")).lower(),
                )
            if key in seen:
                continue
            seen.add(key)
            result.append(row)
        return result

    # Free mode never imports or calls the Apify client.
    if mode in {"free", "both"}:
        free_rows, free_failures = _search_free_sources(
            field, location, industry, experience, int(date_window_days), limit,
            selected_sources=free_sources, ats_urls=ats_urls
        )
        all_rows.extend(free_rows)
        failures.extend(free_failures)
        all_rows = deduplicate(all_rows)

    # Apify runs only when the user explicitly chose Apify or the combined mode.
    if mode in {"apify", "both"}:
        token = os.getenv("APIFY_TOKEN", "").strip()
        if not token:
            failures.append("Apify is selected, but APIFY_TOKEN is not configured.")
        else:
            try:
                from apify_client import ApifyClient
                client = ApifyClient(token=token)
                ids = actor_ids or [ACTOR_CATALOG[name]["id"] for name in DEFAULT_ACTOR_NAMES]
                if not ids:
                    failures.append("Apify is selected, but no Apify Actor is configured.")
                else:
                    needed = limit if mode == "apify" else max(1, limit - len(all_rows))
                    for actor_id in ids:
                        actor_name = ACTOR_ID_TO_NAME.get(actor_id, actor_id)
                        try:
                            all_rows.extend(_run_one_actor(
                                client, actor_id, actor_name, field, location, industry,
                                experience, needed, int(date_window_days)
                            ))
                            all_rows = deduplicate(all_rows)
                            needed = max(0, limit - len(all_rows))
                            if needed == 0:
                                break
                        except Exception as exc:
                            failures.append(f"{actor_name}: {exc}")
            except Exception as exc:
                failures.append(f"Apify: {exc}")

    if not all_rows:
        extra = "\n\n".join(failures)
        raise RuntimeError(
            f"No jobs were returned by the selected job sources.\n\n{extra}"
            if extra else "No jobs were returned by the selected job sources."
        )

    all_rows = deduplicate(all_rows)
    # Do not truncate the final result set. The UI intentionally shows every
    # job returned by the selected collectors after de-duplication and the
    # requested language/date filters. Individual public APIs may still have
    # their own pagination limits.
    all_rows = _apply_language_filter(all_rows, language)

    if failures:
        for row in all_rows:
            row.setdefault("warnings", failures)
    return all_rows
