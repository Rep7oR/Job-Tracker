from __future__ import annotations

import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote_plus

from services.app_paths import BASE_DIR as _PACKAGED_BASE_DIR

_DEV_BASE = Path(__file__).resolve().parents[1]
_BASE = _PACKAGED_BASE_DIR if _PACKAGED_BASE_DIR else _DEV_BASE
PROFILE_DIR = _BASE / "data" / "linkedin_browser_profile"
PROFILE_DIR.mkdir(parents=True, exist_ok=True)


def build_linkedin_search_url(field: str, location: str, industry: str = "") -> str:
    keywords = " ".join(x.strip() for x in (field, industry) if x and x.strip()) or "jobs"
    loc = location.strip() or "Germany"
    # LinkedIn's current Jobs UI supports a "Past week" date filter. The rendered
    # search URL can carry f_TPR=r604800 for the last 7 days; sortBy=DD asks for
    # date order.
    return (
        "https://www.linkedin.com/jobs/search/?"
        f"keywords={quote_plus(keywords)}&location={quote_plus(loc)}"
        "&f_TPR=r604800&sortBy=DD"
    )


def _posted_days(text: str):
    t = (text or "").lower()
    if "today" in t or "just now" in t:
        return 0
    if "yesterday" in t:
        return 1
    m = re.search(r"(\d+)\s*(?:day|days)\s*ago", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*(?:hour|hours)\s*ago", t)
    if m:
        return 0
    m = re.search(r"(\d+)\s*(?:week|weeks)\s*ago", t)
    if m:
        return int(m.group(1)) * 7
    return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _parse_card(card_text: str, href: str, title_hint: str = "") -> dict:
    text = _clean(card_text)
    lines = [_clean(x) for x in card_text.splitlines() if _clean(x)]
    title = _clean(title_hint)

    if not title:
        for line in lines:
            if line and not re.search(r"ago|reposted|easy apply|promoted", line, re.I):
                title = line
                break

    # Heuristic: company and location are typically the next meaningful lines.
    company = ""
    location = ""
    if title and title in lines:
        idx = lines.index(title)
        candidates = lines[idx + 1: idx + 5]
    else:
        candidates = lines[:5]
    if candidates:
        company = candidates[0]
    for c in candidates[1:]:
        if re.search(r"\b(remote|germany|berlin|hamburg|hannover|munich|cologne|frankfurt|stuttgart|düsseldorf|dusseldorf|aachen|chemnitz)\b|,", c, re.I):
            location = c
            break
    posted_text = next((x for x in lines if _posted_days(x) is not None), "")
    days = _posted_days(posted_text)
    if days is not None and days > 7:
        return {}

    return {
        "id": href.rstrip("/").split("/")[-1].split("?")[0],
        "title": title or "LinkedIn job",
        "company": company,
        "location": location,
        "posted_date": (datetime.now(timezone.utc)).date().isoformat() if days is None else (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat(),
        "posted_at": (datetime.now(timezone.utc) - timedelta(days=days or 0)).isoformat(),
        "url": href,
        "description": text,
        "source": "LinkedIn (browser)",
        "actor": "linkedin-browser",
        "raw": {"card_text": card_text},
    }


def search_linkedin_browser(field: str, location: str, industry: str = "", limit: int = 30, login_wait_seconds: int = 120) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "LinkedIn browser search needs Playwright. Run: .\\.venv\\Scripts\\python.exe -m pip install playwright "
            "then: .\\.venv\\Scripts\\python.exe -m playwright install chromium"
        ) from exc

    url = build_linkedin_search_url(field, location, industry)
    results: list[dict] = []

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1440, "height": 1000},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)

            deadline = time.time() + login_wait_seconds
            while time.time() < deadline:
                cards = page.locator("a[href*='/jobs/view/']")
                if cards.count() > 0:
                    break
                page.wait_for_timeout(1500)

            cards = page.locator("a[href*='/jobs/view/']")
            count = min(cards.count(), 60)
            for i in range(count):
                a = cards.nth(i)
                href = a.get_attribute("href") or ""
                if not href:
                    continue
                if href.startswith("/"):
                    href = "https://www.linkedin.com" + href
                title = _clean(a.inner_text(timeout=3000) or a.get_attribute("aria-label") or "")
                # Walk to a card-like ancestor for company/location/posted text.
                card_text = ""
                for xpath in ["ancestor::li[1]", "ancestor::div[contains(@class,'base-card')[1]", "ancestor::div[1]"]:
                    try:
                        loc = a.locator(f"xpath={xpath}")
                        if loc.count():
                            txt = loc.first.inner_text(timeout=1500)
                            if txt and len(txt) > len(title):
                                card_text = txt
                                break
                    except Exception:
                        pass
                row = _parse_card(card_text or title, href, title)
                if row:
                    results.append(row)
            # De-duplicate by URL/id.
            dedup = []
            seen = set()
            for row in results:
                key = row.get("url") or row.get("id") or (row["title"], row["company"])
                if key in seen:
                    continue
                seen.add(key)
                dedup.append(row)
                if len(dedup) >= limit:
                    break
            return dedup
        finally:
            context.close()



def _launch_context(playwright):
    return playwright.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=False,
        viewport={"width": 1440, "height": 1000},
        args=["--disable-blink-features=AutomationControlled"],
    )


def _linkedin_authenticated(page, context=None) -> bool:
    """Best-effort check for a real LinkedIn authenticated session.

    LinkedIn changes its DOM frequently, so the persistent ``li_at`` cookie is
    the primary signal. DOM/URL checks are only fallbacks.
    """
    try:
        if context is not None:
            try:
                cookies = context.cookies()
                auth_names = {"li_at", "liap"}
                if any(c.get("name") in auth_names and c.get("value") for c in cookies):
                    return True
            except Exception:
                pass

        url = (page.url or "").lower()
        if any(x in url for x in ("/login", "/uas/login", "/checkpoint/", "/challenge/")):
            return False

        selectors = [
            "a[href*='/feed/']",
            "a[href*='/notifications/']",
            "button[aria-label*='Me']",
            "img.global-nav__me-photo",
            "div.global-nav__me",
            "nav[aria-label*='Primary']",
        ]
        return any(page.locator(sel).count() > 0 for sel in selectors)
    except Exception:
        return False


def _has_login_cookie(context) -> bool:
    try:
        cookies = context.cookies()
        return any(
            c.get("name") in {"li_at", "liap"} and c.get("value")
            for c in cookies
        )
    except Exception:
        return False


def _wait_for_linkedin_login(page, context, timeout_seconds: int) -> bool:
    """Wait for login/MFA/checkpoint completion without depending on DOM markup."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _has_login_cookie(context) or _linkedin_authenticated(page, context):
            return True
        try:
            page.wait_for_timeout(1000)
        except Exception:
            time.sleep(1)
    return _has_login_cookie(context) or _linkedin_authenticated(page, context)


def connect_linkedin(login_wait_seconds: int = 300) -> bool:
    """Open a persistent visible LinkedIn browser and save the authenticated session.

    The user performs the login directly in Chromium. The app never receives or
    stores the LinkedIn password. Authentication is considered successful as soon
    as LinkedIn establishes its persistent login cookie; this avoids false failures
    caused by LinkedIn changing its navigation DOM after login.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "LinkedIn needs Playwright. Run: .\\.venv\\Scripts\\python.exe -m pip install playwright "
            "then: .\\.venv\\Scripts\\python.exe -m playwright install chromium"
        ) from exc

    with sync_playwright() as p:
        try:
            context = _launch_context(p)
        except Exception as exc:
            raise RuntimeError(
                "Could not open the LinkedIn browser. Close any existing Chromium window using the same "
                "LinkedIn session, then try Connect LinkedIn again."
            ) from exc
        try:
            page = context.pages[0] if context.pages else context.new_page()

            # If a previous connection already exists, don't force another login.
            try:
                page.goto("https://www.linkedin.com/", wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            if _linkedin_authenticated(page, context):
                return True

            # No session yet: open the normal login page and let the user complete
            # password, MFA, passkey, CAPTCHA, or any LinkedIn checkpoint manually.
            try:
                page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            page.wait_for_timeout(1500)

            if not _wait_for_linkedin_login(page, context, login_wait_seconds):
                raise RuntimeError(
                    "LinkedIn login was not detected. Please finish login/MFA in the browser window, "
                    "wait until LinkedIn shows your signed-in account, and click Connect LinkedIn again."
                )

            # Give LinkedIn a moment to finish persisting the session before closing
            # the browser. Do not navigate to Notifications here; that navigation can
            # trigger a fresh challenge and caused false 'login failed' messages.
            page.wait_for_timeout(2000)
            return True
        finally:
            context.close()

def sync_linkedin_notifications(profile_url: str = "", limit: int = 30, login_wait_seconds: int = 120) -> list[dict]:
    """Read the signed-in LinkedIn notification feed using the existing persistent browser profile.

    This does not require an Apify actor. The first run may ask the user to sign in to
    LinkedIn in the opened Chromium window. The session is then kept in PROFILE_DIR.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "LinkedIn notifications need Playwright. Run: .\\.venv\\Scripts\\python.exe -m pip install playwright "
            "then: .\\.venv\\Scripts\\python.exe -m playwright install chromium"
        ) from exc

    results: list[dict] = []
    with sync_playwright() as p:
        try:
            context = _launch_context(p)
        except Exception as exc:
            raise RuntimeError(
                "Could not open the LinkedIn browser. Close any existing Chromium window using the same "
                "LinkedIn session, then try again."
            ) from exc
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://www.linkedin.com/notifications/?filter=all", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            deadline = time.time() + login_wait_seconds
            while time.time() < deadline:
                if _linkedin_authenticated(page, context):
                    break
                # LinkedIn may move the login flow through /checkpoint/ or /challenge/.
                page.wait_for_timeout(1000)
            if not _linkedin_authenticated(page, context):
                raise RuntimeError(
                    "LinkedIn is not authenticated in the saved browser session. Open Settings → Connect LinkedIn, "
                    "finish login/MFA in the browser window, wait until your LinkedIn home page is visible, "
                    "then try notification sync again."
                )

            selectors = [
                "div.nt-card", "article", "li.notification-card", "main li",
                "div[data-view-name*='notification']",
            ]
            cards = None
            for selector in selectors:
                loc = page.locator(selector)
                if loc.count() > 0:
                    cards = loc
                    break
            if cards is None:
                return []

            seen = set()
            for i in range(min(cards.count(), max(limit * 3, 30))):
                card = cards.nth(i)
                try:
                    text = _clean(card.inner_text(timeout=2000))
                except Exception:
                    continue
                if not text or len(text) < 8:
                    continue
                try:
                    link = card.locator("a[href]").first
                    href = link.get_attribute("href") if link.count() else ""
                except Exception:
                    href = ""
                if href and href.startswith("/"):
                    href = "https://www.linkedin.com" + href
                key = (text[:220], href)
                if key in seen:
                    continue
                seen.add(key)
                now = datetime.now(timezone.utc)
                results.append({
                    "id": f"linkedin-notification-{abs(hash(key))}",
                    "message": text,
                    "received_at": now.isoformat(timespec="seconds"),
                    "url": href or "https://www.linkedin.com/notifications/",
                    "profile_url": profile_url.strip(),
                    "source": "LinkedIn",
                })
                if len(results) >= limit:
                    break
            return results
        finally:
            context.close()
