"""Simple, explainable matching of postings against a user profile.

Deliberately no embeddings/AI calls here (phase 2 scope) — matching is
keyword/substring based:

  * Title match: does the profile's ``target_role`` (split into words)
    appear in the posting title? Each whole word of the target role found
    in the title contributes to the score; the full phrase matching is
    worth an extra bonus.
  * Location match: does the profile's ``location`` appear in the posting's
    location, or is the posting remote? A "remote" posting always passes
    the location gate, regardless of the profile's location.
  * Score: 0-100, roughly "how much of the target role's wording shows up
    in the title, plus a flat bonus for a location/remote match" — postings
    that don't match location at all are filtered out entirely.
"""

from __future__ import annotations

import re

_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def _is_remote(location: str) -> bool:
    return "remote" in (location or "").lower()


def location_matches(profile_location: str, posting_location: str) -> bool:
    """A posting passes the location gate if it's remote, the profile gave
    no location preference, or the profile's location is a substring of the
    posting's location (case-insensitive)."""
    if _is_remote(posting_location):
        return True
    profile_location = (profile_location or "").strip().lower()
    if not profile_location:
        return True
    return profile_location in (posting_location or "").lower()


def score_posting(profile: dict, posting: dict) -> int:
    """Score one posting 0-100 against a profile. Higher is a better match.

    Scoring: each target-role word that appears in the title is worth
    `60 / word_count` points (so a fully-matching title scores 60); an
    exact full-phrase substring match adds a +20 bonus; a location/remote
    match adds a flat +20. Postings that fail the location gate are not
    scored by callers (see ``rank_postings``) since they're filtered out.
    """
    target_role = (profile.get("target_role") or "").strip().lower()
    title = (posting.get("title") or "").lower()

    role_words = _words(target_role)
    score = 0.0
    if role_words:
        matched = sum(1 for word in role_words if word in title)
        score += 60.0 * matched / len(role_words)
        if target_role and target_role in title:
            score += 20.0

    if location_matches(profile.get("location", ""), posting.get("location", "")):
        score += 20.0

    return round(min(score, 100.0))


def rank_postings(profile: dict, postings: list[dict], top_n: int = 25) -> list[dict]:
    """Filter postings to those passing the location gate, score them, and
    return the top ``top_n`` sorted by descending score (ties keep original
    order). Each returned dict is the original posting plus a "score" key.
    """
    scored = []
    for posting in postings:
        if not location_matches(profile.get("location", ""), posting.get("location", "")):
            continue
        score = score_posting(profile, posting)
        scored.append({**posting, "score": score})

    scored.sort(key=lambda p: p["score"], reverse=True)
    return scored[:top_n]
