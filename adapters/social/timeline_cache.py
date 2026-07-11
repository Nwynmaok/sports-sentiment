"""
adapters/social/timeline_cache.py
Shared, incremental cache in front of twitterapi.io timeline fetches.

Why: the same tracked-account roster serves every sport, but each sport
run used to re-fetch all 35 timelines independently — with game-anchored
cluster runs that would double or triple Twitter spend. Two mitigations
live here:

  1. TTL sharing — a timeline fetched in the last `ttl_seconds` is served
     from data/_shared/timeline_cache.json, so back-to-back sport runs
     (MLB + WNBA fired by the same dispatcher tick) pay for Twitter once.
  2. Incremental fetch — the first fetch of a handle each day pulls
     FIRST_LIMIT tweets; later fetches the same day pull INCR_LIMIT and
     merge by tweet id, since the morning run already captured the
     overnight backlog. twitterapi.io charges per tweet returned.

Single-writer assumption: only the dispatcher (which runs sports
sequentially) writes this cache.
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from . import twitterapi_io

log = logging.getLogger("pipeline.social.timeline_cache")

TTL_SECONDS = 45 * 60
FIRST_LIMIT = 20
INCR_LIMIT = 10
KEEP_PER_HANDLE = 40
PRUNE_AFTER_DAYS = 3


def _load(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            log.warning(f"unreadable timeline cache {path}, starting fresh")
    return {}


def _save(cache: dict, path: Path, now: datetime):
    cutoff = (now - timedelta(days=PRUNE_AFTER_DAYS)).isoformat()
    stale = [h for h, e in cache.items() if e.get("fetched_at", "") < cutoff]
    for h in stale:
        del cache[h]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache))
    tmp.replace(path)


def fetch_timelines(handles: list, cache_path, ttl_seconds: int = TTL_SECONDS,
                    now: datetime = None) -> list:
    """Return recent posts for every handle, fetching only what the cache
    can't serve. Posts are the normalized dicts from make_post (media
    included), newest first, up to FIRST_LIMIT per handle."""
    now = now or datetime.now().astimezone()
    today = now.strftime("%Y-%m-%d")
    cache_path = Path(cache_path)
    cache = _load(cache_path)
    posts = []
    fetched = served = 0
    dirty = False

    for handle in handles:
        if not handle:
            continue
        key = handle.lower().lstrip("@")
        entry = cache.get(key)

        fresh = False
        if entry:
            try:
                age = now - datetime.fromisoformat(entry["fetched_at"])
                fresh = age < timedelta(seconds=ttl_seconds)
            except (KeyError, ValueError):
                fresh = False

        if fresh:
            served += 1
        else:
            first_of_day = not entry or entry.get("date") != today
            limit = FIRST_LIMIT if first_of_day else INCR_LIMIT
            new = twitterapi_io.fetch_timeline(handle, limit=limit)
            fetched += 1
            merged = {p.get("id"): p for p in (entry or {}).get("posts", [])}
            for p in new:
                if p.get("id"):
                    merged[p["id"]] = p
            ordered = sorted(merged.values(),
                             key=lambda p: p.get("published", ""),
                             reverse=True)[:KEEP_PER_HANDLE]
            entry = {"fetched_at": now.isoformat(), "date": today,
                     "posts": ordered}
            cache[key] = entry
            dirty = True

        posts.extend(entry["posts"][:FIRST_LIMIT])

    if dirty:
        _save(cache, cache_path, now)
    log.info(f"timelines: {served} from cache, {fetched} fetched")
    return posts
