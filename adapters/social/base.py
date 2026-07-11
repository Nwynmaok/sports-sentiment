"""
adapters/social/base.py
Normalized post schema shared by every social source. Field names are kept
compatible with the tweet dicts the original nba-sentiment core consumed,
so the analysis layer works unchanged across Reddit, Bluesky, and Twitter.
"""

import time
import logging
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("pipeline.social")

USER_AGENT = "sports-sentiment/0.1 (personal research pipeline)"


def make_post(id="", user="", user_display="", user_followers=0,
              user_verified=False, text="", published="", likes=0,
              reposts=0, replies=0, views=0, url="", source="",
              source_query="", source_type="search", source_game=None,
              source_label=None, lang="", media=None) -> dict:
    return {
        "id": id,
        "media": media or [],
        "user": user,
        "user_display": user_display,
        "user_followers": user_followers,
        "user_verified": user_verified,
        "text": text,
        "published": published,
        "likes": likes,
        "retweets": reposts,
        "replies": replies,
        "views": views,
        "url": url,
        "source": source,
        "source_query": source_query,
        "source_type": source_type,
        "source_game": source_game,
        "source_label": source_label,
        "lang": lang,
    }


def get_json(url, params=None, headers=None, retries=3, backoff=2.0, timeout=20):
    """GET with retry/backoff; returns parsed JSON or None."""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=hdrs, timeout=timeout)
            if r.status_code == 429:
                wait = backoff * (attempt + 1)
                log.warning(f"429 from {url}, waiting {wait:.0f}s")
                time.sleep(wait)
                continue
            if not r.ok:
                log.warning(f"{r.status_code} from {url}")
                return None
            return r.json()
        except requests.RequestException as e:
            log.warning(f"Request error for {url}: {e}")
            time.sleep(backoff)
    return None


def parse_published(value):
    """Best-effort parse of the 'published' shapes the adapters emit —
    Twitter classic ('Sat Jul 04 23:15:52 +0000 2026'), ISO-8601 with or
    without 'Z' (Reddit/Bluesky/YouTube/4chan), epoch seconds — into an
    aware UTC datetime. Returns None when unparseable."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    s = str(value).strip()
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def filter_stale_posts(posts: list, max_age_hours: int = 36,
                       now: datetime = None) -> list:
    """Drop posts older than max_age_hours. Tracked-account timelines
    reach back as far as needed to fill their tweet limit, so a capper
    who posts twice a week hands the pipeline week-old picks about
    finished games — those must never feed today's sentiment. Posts
    whose timestamp can't be parsed are KEPT (a format quirk in one
    adapter shouldn't silently blind a whole source)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)
    fresh = []
    dropped = 0
    for p in posts:
        published = parse_published(p.get("published"))
        if published is None or published >= cutoff:
            fresh.append(p)
        else:
            dropped += 1
    if dropped:
        log.info(f"dropped {dropped} stale posts (older than {max_age_hours}h)")
    return fresh


def dedupe_posts(posts: list) -> list:
    """Dedupe by (source, id), preserving first occurrence (which carries
    the most specific source_game/source_label)."""
    seen = set()
    out = []
    for p in posts:
        key = (p.get("source"), p.get("id"))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out
