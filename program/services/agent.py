"""Phase 2 agent orchestration: fetch postings, match against the profile,
and stage results into the account's queue.

This is a manual-trigger loop, not a background scheduler — a "Run agent
now" button on the Queue page calls ``run_agent_once``. Building an actual
scheduler/cron is a premature abstraction before search+match are proven
out end to end.
"""

from __future__ import annotations

from services import job_sources, matcher, storage

TOP_N_MATCHES = 25


def run_agent_once(email: str) -> dict:
    """Fetch postings from the seeded sources, match them against the
    account's profile, and stage the top matches into its queue.

    Returns a small summary dict: {"fetched", "matched", "added"}.
    """
    profile = storage.get_profile(email)
    postings = job_sources.fetch_all_postings()
    ranked = matcher.rank_postings(profile, postings, top_n=TOP_N_MATCHES)
    added = storage.add_to_queue(email, ranked)
    return {
        "fetched": len(postings),
        "matched": len(ranked),
        "added": added,
    }
