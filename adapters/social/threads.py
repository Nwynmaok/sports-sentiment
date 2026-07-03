"""
adapters/social/threads.py
Public post keyword search via Meta's official Threads API.

Setup (free, Meta-typical paperwork):
    1. developers.facebook.com -> create app -> add the "Threads API"
       use case, tied to a Threads profile.
    2. Generate a long-lived access token with the
       threads_keyword_search permission.
    3. Set THREADS_ACCESS_TOKEN in .env.

Hard platform limit: 500 keyword searches per rolling 7-day window.
A persistent budget counter (data/threads_usage.json) refuses to go
past SAFETY_CAP so one busy week can't lock the token out entirely.
Like/reply counts aren't exposed on public search results, so posts
land with zero engagement — fine for sentiment, and authors can still
be tier-weighted via accounts.json.
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .base import make_post, get_json

log = logging.getLogger("pipeline.social.threads")

BASE = "https://graph.threads.net/v1.0"
PACE_SECONDS = 1.0
SAFETY_CAP = 450  # of the 500/7d platform limit
USAGE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "threads_usage.json"

FIELDS = "id,text,username,permalink,timestamp,media_type"


def access_token() -> str:
    return os.environ.get("THREADS_ACCESS_TOKEN", "")


def enabled() -> bool:
    return bool(access_token())


def _load_usage() -> list:
    if USAGE_PATH.exists():
        try:
            with open(USAGE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _budget_remaining(usage: list, now: datetime) -> int:
    cutoff = (now - timedelta(days=7)).isoformat()
    return SAFETY_CAP - sum(1 for ts in usage if ts >= cutoff)


def _record_usage(usage: list, now: datetime):
    cutoff = (now - timedelta(days=7)).isoformat()
    usage = [ts for ts in usage if ts >= cutoff]
    usage.append(now.isoformat())
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(USAGE_PATH, "w") as f:
        json.dump(usage, f)
    return usage


def budget_remaining() -> int:
    return _budget_remaining(_load_usage(), datetime.now(timezone.utc))


def _post_from_item(item: dict, source_query="", source_game=None,
                    source_label=None) -> dict:
    return make_post(
        id=f"threads-{item.get('id', '')}",
        user=item.get("username", ""),
        text=item.get("text", "")[:2000],
        published=item.get("timestamp", ""),
        url=item.get("permalink", ""),
        source="threads",
        source_query=source_query,
        source_type="search",
        source_game=source_game,
        source_label=source_label,
    )


def search(query: str, limit: int = 25, source_game=None, source_label=None) -> list:
    """One keyword search (counts against the 500/7d platform limit)."""
    if not enabled():
        return []
    now = datetime.now(timezone.utc)
    usage = _load_usage()
    if _budget_remaining(usage, now) <= 0:
        log.warning("Threads weekly search budget exhausted "
                    f"({SAFETY_CAP}/7d safety cap) — skipping '{query}'")
        return []

    data = get_json(f"{BASE}/keyword_search", params={
        "q": query, "search_type": "RECENT", "fields": FIELDS,
        "limit": min(limit, 50), "access_token": access_token(),
    })
    _record_usage(usage, now)
    time.sleep(PACE_SECONDS)
    if not data:
        return []
    if data.get("error"):
        log.error(f"Threads API error: {data['error'].get('message', '')[:150]}")
        return []
    posts = [_post_from_item(p, source_query=query, source_game=source_game,
                             source_label=source_label)
             for p in data.get("data", [])]
    log.info(f"threads search '{query}' -> {len(posts)} posts "
             f"(budget left: {_budget_remaining(_load_usage(), now)})")
    return posts
