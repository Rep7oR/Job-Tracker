from __future__ import annotations

import html
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus, urlparse

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36")


def _session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    })
    return s


def _clean(v):
    """Return human-readable plain text from strings, HTML, Tags, lists or dicts."""
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(x for x in (_clean(item) for item in v) if x)
    if isinstance(v, dict):
        for k in ("name", "label", "text", "value"):
            if v.get(k):
                return _clean(v[k])
        return str(v)

    # BeautifulSoup Tag / HTML string handling. Some public job pages return
    # HTML that is itself entity-escaped (for example &lt;h3&gt;...&lt;/h3&gt;).
    text = str(v).strip()
    for _ in range(2):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return html.unescape(text).strip()


def _dt(v):
    if not v:
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(v / 1000 if v > 10_000_000_000 else v, tz=timezone.utc)
        except Exception:
            return None
    s = str(v).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        pass
    m = re.search(r"(\d+)\s*(hour|hours|day|days)\s*ago", s.lower())
    if m:
        n = int(m.group(1))
        return datetime.now(timezone.utc) - timedelta(hours=n if "hour" in m.group(2) else n * 24)
    return None


def _job(title, company="", location="", url="", description="", posted=None, source="", raw=None):
    key = url or f"{title}|{company}|{location}"
    import hashlib
    jid = hashlib.sha256(key.lower().encode()).hexdigest()[:24]
    return {
        "id": jid,
        "title": title.strip(),
        "company": company.strip(),
        "location": location.strip(),
        "industry": "",
        "experience": "",
        "work_type": "",
        "contract_type": "",
        "salary": "",
        "posted_date": posted.astimezone().strftime("%Y-%m-%d") if posted else "",
        "posted_at": posted.isoformat() if posted else "",
        "url": url,
        "description": description.strip(),
        "source": source,
        "actor": "",
        "raw": raw or {},
    }


def _cards(url, source, params, cards, title_sel, company_sel, loc_sel):
    r = _session().get(url, params=params, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    rows = []
    seen = set()
    for card in soup.select(cards):
        t = card.select_one(title_sel)
        if not t:
            continue
        title = _clean(t)
        a = t if t.name == "a" else card.select_one("a[href]")
        href = a.get("href") if a else ""
        if not title or not href:
            continue
        full = requests.compat.urljoin(url, href)
        if full in seen:
            continue
        seen.add(full)
        company = _clean(card.select_one(company_sel))
        loc = _clean(card.select_one(loc_sel))
        time_el = card.select_one("time")
        posted = _dt(time_el.get("datetime") if time_el else "")
        rows.append(_job(title, company, loc, full, _clean(card), posted, source))
    return rows


def indeed(keyword, location="", days=1, limit=50):
    params = {"q": keyword, "l": location or "Germany", "fromage": days}
    return _cards("https://de.indeed.com/jobs", "Indeed", params,
                  "div.job_seen_beacon, td.resultContent",
                  "h2.jobTitle, h2 a", "span.companyName", "div.companyLocation")[:limit]


def stepstone(keyword, location="", days=1, limit=50):
    params = {"q": keyword, "l": location or "Germany"}
    rows = _cards("https://www.stepstone.de/jobs", "StepStone", params,
                  "article, [data-at='job-item']",
                  "h2, h3, [data-at='job-item-title']", 
                  "[data-at='job-item-company-name'], h4",
                  "[data-at='job-item-location']")
    return rows[:limit]


def monster(keyword, location="", days=1, limit=50):
    params = {"q": keyword, "where": location or "Germany"}
    rows = _cards("https://www.monster.de/jobs/search/", "Monster", params,
                  "article, [data-testid='svx-job-card'], .job-cardstyle",
                  "h2, h3, a[href*='/jobs/']",
                  ".company, [data-testid='svx-job-card-company']",
                  ".location, [data-testid='svx-job-card-location']")
    return rows[:limit]


def glassdoor(keyword, location="", days=1, limit=50):
    params = {"sc.keyword": keyword}
    if location:
        params["locKeyword"] = location
    rows = _cards("https://www.glassdoor.de/Job/jobs.htm", "Glassdoor", params,
                  "li[data-test='jobListing'], article, [data-test='jobListing']",
                  "a[data-test='job-title'], a.jobLink, h2",
                  "span[data-test='employer-name'], .EmployerProfile_compactEmployerName",
                  "div[data-test='emp-location'], .JobCard_location")
    return rows[:limit]


def linkedin_guest(keyword, location="", days=1, limit=50):
    params = {
        "keywords": keyword,
        "location": location or "Germany",
        "f_TPR": f"r{days * 86400}",
        "start": 0,
    }
    r = _session().get(
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
        params=params, timeout=25,
        headers={"Accept": "text/html,application/xhtml+xml"},
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    rows = []
    for card in soup.select("li"):
        t = card.select_one("h3")
        a = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
        if not t or not a:
            continue
        tm = card.select_one("time")
        rows.append(_job(
            _clean(t),
            _clean(card.select_one("h4")),
            _clean(card.select_one(".job-search-card__location")),
            a.get("href", "").split("?")[0],
            _clean(card),
            _dt(tm.get("datetime") if tm else ""),
            "LinkedIn",
        ))
        if len(rows) >= limit:
            break
    return rows


def _greenhouse(url, company):
    api = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true"
    data = _session().get(api, timeout=25).json()
    return [_job(
        _clean(x.get("title")), company,
        _clean((x.get("location") or {}).get("name")),
        _clean(x.get("absolute_url")),
        BeautifulSoup(_clean(x.get("content")), "html.parser").get_text(" ", strip=True),
        _dt(x.get("updated_at") or x.get("created_at")),
        "Greenhouse", x) for x in data.get("jobs", []) if x.get("title")]


def _lever(url, company):
    api = f"https://api.lever.co/v0/postings/{company}?mode=json"
    data = _session().get(api, timeout=25).json()
    rows = []
    for x in data:
        categories = x.get("categories") or {}
        rows.append(_job(
            _clean(x.get("text")), company,
            _clean(categories.get("location")),
            _clean(x.get("hostedUrl") or x.get("applyUrl")),
            _clean(x.get("descriptionPlain") or x.get("description")),
            _dt(x.get("createdAt")),
            "Lever", x))
    return rows


def _ashby(url, company):
    api = f"https://api.ashbyhq.com/posting-api/job-board/{company}?includeCompensation=true"
    data = _session().get(api, timeout=25).json()
    rows = []
    for x in data.get("jobs", data if isinstance(data, list) else []):
        rows.append(_job(
            _clean(x.get("title")), company,
            _clean(x.get("location")),
            _clean(x.get("jobUrl") or x.get("applyUrl")),
            BeautifulSoup(_clean(x.get("descriptionHtml") or x.get("description")), "html.parser").get_text(" ", strip=True),
            _dt(x.get("publishedAt") or x.get("createdAt")),
            "Ashby", x))
    return rows


def _smartrecruiters(url, company):
    rows = []
    offset = 0
    while offset < 200:
        api = f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
        data = _session().get(api, params={"limit": 100, "offset": offset}, timeout=25).json()
        items = data.get("content", [])
        if not items:
            break
        for x in items:
            loc = x.get("location") or {}
            rows.append(_job(
                _clean(x.get("name")), company,
                _clean(", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")] ))),
                f"https://jobs.smartrecruiters.com/{company}/{x.get('id')}",
                "",
                _dt(x.get("releasedDate") or x.get("updatedDate")),
                "SmartRecruiters", x))
        offset += len(items)
        if len(items) < 100:
            break
    return rows


def _workable(url, company):
    api = f"https://apply.workable.com/api/v1/widget/accounts/{company}"
    data = _session().get(api, timeout=25).json()
    items = data.get("jobs", data if isinstance(data, list) else [])
    return [_job(
        _clean(x.get("title")), company,
        _clean(x.get("location") or x.get("city")),
        _clean(x.get("url") or x.get("shortlink")),
        _clean(x.get("description")),
        _dt(x.get("created_at") or x.get("published_at")),
        "Workable", x) for x in items]


def _personio(url, company):
    # Personio exposes a public XML job feed on many hosted career sites.
    api = f"https://{company}.jobs.personio.com/xml"
    r = _session().get(api, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "xml")
    rows = []
    for x in soup.find_all("position"):
        rows.append(_job(
            _clean(x.find("name")),
            company,
            _clean(x.find("office")),
            _clean(x.find("jobDescriptions")),
            _clean(x),
            _dt(_clean(x.find("createdAt"))),
            "Personio",
        ))
    return rows


def _workday(url, company="", search_text=""):
    """Fetch postings from a public Workday career board.

    Workday's public CXS endpoint uses POST and a hard page size of 20.  Some
    tenants reject perfectly valid requests from plain HTTP clients (including
    Airbus), so this implementation first uses the canonical request and then
    falls back to a real Chromium page context.  The browser fallback is only
    used when the direct request is rejected; it is not a login flow.
    """
    p = urlparse(url.strip())
    host = p.netloc
    host_low = host.lower()
    parts = [x for x in p.path.split("/") if x]
    if "myworkdayjobs.com" not in host_low or not parts:
        raise ValueError("Workday requires a public myworkdayjobs.com career-board URL.")

    m = re.match(r"^([^.]+)\.wd(\d+)\.myworkdayjobs\.com$", host_low)
    if not m:
        raise ValueError(f"Could not parse Workday tenant/datacenter from host: {host}")
    tenant = m.group(1)

    # /Airbus or /en-US/Airbus.  Preserve the exact site spelling because
    # Workday site identifiers are case-sensitive on some tenants.
    site = parts[-1]
    locale = ""
    if len(parts) >= 2 and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[-2]):
        locale = parts[-2]
        site = parts[-1]

    endpoint = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    requested_search = (search_text or "").strip()
    page_size = 20

    def _payload(offset, query):
        return {
            "appliedFacets": {},
            "limit": page_size,
            "offset": offset,
            "searchText": query,
        }

    def _direct_page(offset, query):
        # Keep headers intentionally close to Workday's documented public
        # request.  A few tenants reject an artificial Origin/Referer header.
        session = _session()
        session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
            "Content-Type": "application/json",
        })
        response = session.post(endpoint, json=_payload(offset, query), timeout=30)
        if response.status_code >= 400:
            detail = response.text[:300].replace("\n", " ")
            raise requests.HTTPError(
                f"Workday HTTP {response.status_code} for {endpoint}"
                + (f" ({detail})" if detail else ""), response=response
            )
        return response.json()

    def _browser_pages(query):
        """Use the public career page's own browser context to call CXS."""
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError(
                "Workday rejected the direct API request and Playwright is unavailable. "
                "Run SETUP_FIRST.bat to install Chromium."
            ) from exc

        results = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=UA,
                locale="en-US",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                # The Workday SPA can take a moment to initialize.  Waiting for
                # the network to settle also lets any tenant-side bot checks run.
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass

                script = """
                async ({endpoint, offset, query}) => {
                    const response = await fetch(endpoint, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'Accept': 'application/json, text/plain, */*'
                        },
                        body: JSON.stringify({
                            appliedFacets: {},
                            limit: 20,
                            offset: offset,
                            searchText: query
                        })
                    });
                    const text = await response.text();
                    return {status: response.status, text: text};
                }
                """
                offset = 0
                total = None
                while offset < 10000:
                    raw = page.evaluate(script, {
                        "endpoint": endpoint,
                        "offset": offset,
                        "query": query,
                    })
                    status = int(raw.get("status", 0))
                    if status >= 400:
                        raise RuntimeError(
                            f"Workday browser request returned HTTP {status}: {raw.get('text','')[:300]}"
                        )
                    data = __import__("json").loads(raw.get("text") or "{}")
                    if total is None:
                        total = data.get("total")
                    batch = data.get("jobPostings") or []
                    results.extend(batch)
                    if not batch:
                        break
                    offset += len(batch)
                    if total is not None:
                        try:
                            if offset >= int(total):
                                break
                        except Exception:
                            pass
                    if len(batch) < page_size:
                        break
            finally:
                browser.close()
        return results

    def _fetch_all(query):
        rows = []
        offset = 0
        total = None
        while offset < 10000:
            data = _direct_page(offset, query)
            if total is None:
                total = data.get("total")
            batch = data.get("jobPostings") or []
            rows.extend(batch)
            if not batch:
                break
            offset += len(batch)
            if total is not None:
                try:
                    if offset >= int(total):
                        break
                except Exception:
                    pass
            if len(batch) < page_size:
                break
        return rows

    fallback = False
    try:
        raw_items = _fetch_all(requested_search)
    except Exception:
        # A number of Workday boards reject keyword searches.  Retry with an
        # empty query before using Chromium.  Filtering is then performed locally.
        try:
            raw_items = _fetch_all("")
            fallback = bool(requested_search)
        except Exception:
            raw_items = _browser_pages(requested_search or "")
            fallback = bool(requested_search)

    rows = []
    public_base = f"https://{host}"
    if locale:
        public_base += f"/{locale}"
    public_base += f"/{site}"
    for x in raw_items:
        external = _clean(x.get("externalPath") or "")
        job_url = requests.compat.urljoin(public_base + "/", external.lstrip("/")) if external else url
        description = _clean(x.get("jobDescription") or x.get("bulletFields"))
        posted = _dt(x.get("postedOn") or x.get("startDate"))
        raw = dict(x)
        if fallback and requested_search:
            raw["workday_search_fallback"] = True
            raw["requested_search_text"] = requested_search
        rows.append(_job(
            _clean(x.get("title")),
            company or site,
            _clean(x.get("locationsText") or x.get("location")),
            job_url,
            description,
            posted,
            "Workday",
            raw,
        ))
    return rows

def _company_from_url(url, source):
    p = urlparse(url.strip())
    host = p.netloc.lower()
    path = [x for x in p.path.split("/") if x]
    if source == "Greenhouse":
        if "greenhouse.io" in host and path:
            return path[-1]
    if source == "Lever":
        if "lever.co" in host and path:
            return path[-1]
    if source == "Ashby":
        if "ashbyhq.com" in host and path:
            return path[-1]
    if source == "SmartRecruiters":
        if "smartrecruiters.com" in host and path:
            return path[-1]
    if source == "Workable":
        if "workable.com" in host and path:
            return path[0]
    if source == "Personio":
        if "personio" in host:
            return host.split(".")[0]
    if source == "Workday":
        # Workday career URLs are commonly /Airbus or /en-US/Airbus.
        # The final non-locale path component is the public career-site name.
        for value in reversed(path):
            if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", value):
                return value
    return ""


def ats_from_urls(urls, search_text=""):
    """Fetch jobs from explicitly supplied public ATS career URLs.

    URL detection is based on the actual ATS hostname, not on whether the
    literal word "Workday"/"Greenhouse"/etc. appears in the URL. This matters
    for normal Workday URLs such as ``https://ag.wd3.myworkdayjobs.com/Airbus``.
    """
    dispatch = {
        "Greenhouse": _greenhouse,
        "Lever": _lever,
        "Ashby": _ashby,
        "SmartRecruiters": _smartrecruiters,
        "Workable": _workable,
        "Personio": _personio,
        "Workday": _workday,
    }
    rows, errors = [], []
    for raw in urls or []:
        url = raw.strip()
        if not url:
            continue
        p = urlparse(url)
        host = p.netloc.lower()
        path = p.path.lower()

        if "myworkdayjobs.com" in host:
            source = "Workday"
        elif "greenhouse.io" in host:
            source = "Greenhouse"
        elif "lever.co" in host:
            source = "Lever"
        elif "ashbyhq.com" in host:
            source = "Ashby"
        elif "smartrecruiters.com" in host:
            source = "SmartRecruiters"
        elif "workable.com" in host:
            source = "Workable"
        elif "personio" in host or "jobs.personio.com" in host:
            source = "Personio"
        else:
            # Keep the old literal-name fallback for unusual/custom ATS URLs.
            low = url.lower()
            source = next((k for k in dispatch if k.lower() in low), None)

        if not source:
            errors.append(f"Unknown ATS URL: {url}")
            continue
        try:
            company = _company_from_url(url, source)
            if source == "Workday":
                rows.extend(_workday(url, company, search_text=search_text))
            else:
                rows.extend(dispatch[source](url, company))
        except Exception as exc:
            errors.append(f"{source}: {exc}")
    return rows, errors


FREE_SOURCE_NAMES = [
    "Bundesagentur für Arbeit", "LinkedIn", "Indeed", "StepStone", "Monster",
    "Glassdoor", "Arbeitnow", "Remote OK", "Remotive", "Greenhouse", "Lever",
    "SmartRecruiters", "Workable", "Personio", "Ashby", "Workday",
]


def collect_board_sources(keyword, location, days=1, selected=None, limit=50):
    selected = set(selected or FREE_SOURCE_NAMES)
    dispatch = {
        "LinkedIn": linkedin_guest,
        "Indeed": indeed,
        "StepStone": stepstone,
        "Monster": monster,
        "Glassdoor": glassdoor,
    }
    rows, errors = [], []
    for name, fn in dispatch.items():
        if name not in selected:
            continue
        try:
            rows.extend(fn(keyword, location, days, limit))
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return rows, errors
