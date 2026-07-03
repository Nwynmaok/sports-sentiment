"""
adapters/social/reddit.py
Reads public Reddit content via the official API using a free "script" app
(https://www.reddit.com/prefs/apps -> create app -> type "script").

Set in .env:
    REDDIT_CLIENT_ID=...
    REDDIT_CLIENT_SECRET=...

Uses the application-only OAuth grant (client_credentials): read-only,
no user login, free tier is 100 queries/minute — far more than this
pipeline needs. Reddit blocks the old unauthenticated *.json endpoints
(403), so credentials are required for this source.
"""

import os
import time
import logging
from datetime import datetime, timezone

import requests

from .base import make_post, get_json, USER_AGENT

log = logging.getLogger("pipeline.social.reddit")

OAUTH_BASE = "https://oauth.reddit.com"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
PACE_SECONDS = 1.0

_token = {"value": None, "expires": 0.0}


def enabled() -> bool:
    cid = os.environ.get("REDDIT_CLIENT_ID", "")
    secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
    # template placeholders don't count as configured
    return bool(cid and secret and not cid.startswith("your_"))


def _get_token():
    # covers both a live token and the negative-cache window after a
    # failed auth (value None, expires in the future)
    if time.time() < _token["expires"] - 60:
        return _token["value"]
    if not enabled():
        return None
    try:
        r = requests.post(
            TOKEN_URL,
            auth=(os.environ["REDDIT_CLIENT_ID"], os.environ["REDDIT_CLIENT_SECRET"]),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        _token["value"] = data["access_token"]
        _token["expires"] = time.time() + data.get("expires_in", 3600)
        return _token["value"]
    except (requests.RequestException, KeyError) as e:
        log.error(f"Reddit OAuth failed: {e}")
        # negative-cache so one bad credential doesn't retry on every
        # call in the same run
        _token["value"] = None
        _token["expires"] = time.time() + 360
        return None


def _api_get(path: str, params: dict):
    token = _get_token()
    if not token:
        log.warning("Reddit disabled: set REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET")
        return None
    return get_json(f"{OAUTH_BASE}{path}", params=params,
                    headers={"Authorization": f"Bearer {token}"})


def _post_from_child(child: dict, source_query="", source_type="search",
                     source_game=None, source_label=None) -> dict:
    d = child.get("data", {})
    title = d.get("title", "")
    selftext = d.get("selftext", "") or ""
    text = title if not selftext else f"{title}\n{selftext}"
    created = d.get("created_utc")
    published = (datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
                 if created else "")
    return make_post(
        id=d.get("name", d.get("id", "")),
        user=d.get("author", ""),
        text=text[:2000],
        published=published,
        likes=d.get("score", 0),
        replies=d.get("num_comments", 0),
        url=f"https://www.reddit.com{d.get('permalink', '')}",
        source="reddit",
        source_query=source_query,
        source_type=source_type,
        source_game=source_game,
        source_label=source_label,
    )


def search(query: str, subreddits: list, limit: int = 25, time_filter: str = "day",
           source_game=None, source_label=None) -> list:
    """Search one query across a multireddit (sub1+sub2) in a single request."""
    if not subreddits:
        return []
    multi = "+".join(subreddits)
    data = _api_get(f"/r/{multi}/search",
                    params={"q": query, "restrict_sr": "on", "sort": "new",
                            "t": time_filter, "limit": limit})
    time.sleep(PACE_SECONDS)
    if not data:
        return []
    children = data.get("data", {}).get("children", [])
    posts = [_post_from_child(c, source_query=query, source_type="search",
                              source_game=source_game, source_label=source_label)
             for c in children if c.get("kind") == "t3"]
    log.info(f"reddit search '{query}' -> {len(posts)} posts")
    return posts


def fetch_new(subreddit: str, limit: int = 50) -> list:
    """Newest posts from a subreddit (general chatter, matched later by text)."""
    data = _api_get(f"/r/{subreddit}/new", params={"limit": limit})
    time.sleep(PACE_SECONDS)
    if not data:
        return []
    children = data.get("data", {}).get("children", [])
    posts = [_post_from_child(c, source_query=f"r/{subreddit}/new",
                              source_type="timeline")
             for c in children if c.get("kind") == "t3"]
    log.info(f"reddit r/{subreddit}/new -> {len(posts)} posts")
    return posts
