"""
adapters/social/bluesky.py
Reads public Bluesky content. Everything is free:

- Author feeds work unauthenticated via the public AppView
  (public.api.bsky.app).
- searchPosts now requires a session (403 unauthenticated), so search
  needs a free app password: Bluesky Settings -> App Passwords, then set
  BLUESKY_HANDLE and BLUESKY_APP_PASSWORD in .env.
"""

import os
import time
import logging

import requests

from .base import make_post, get_json, USER_AGENT

log = logging.getLogger("pipeline.social.bluesky")

BASE = "https://public.api.bsky.app/xrpc"
AUTH_BASE = "https://bsky.social/xrpc"
PACE_SECONDS = 0.5

_session = {"jwt": None, "created": 0.0}


def search_enabled() -> bool:
    return bool(os.environ.get("BLUESKY_HANDLE") and os.environ.get("BLUESKY_APP_PASSWORD"))


def _get_jwt():
    # accessJwt lives ~2h; refresh well before that
    if _session["jwt"] and time.time() - _session["created"] < 5400:
        return _session["jwt"]
    if not search_enabled():
        return None
    try:
        r = requests.post(
            f"{AUTH_BASE}/com.atproto.server.createSession",
            json={"identifier": os.environ["BLUESKY_HANDLE"],
                  "password": os.environ["BLUESKY_APP_PASSWORD"]},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        r.raise_for_status()
        _session["jwt"] = r.json()["accessJwt"]
        _session["created"] = time.time()
        return _session["jwt"]
    except (requests.RequestException, KeyError) as e:
        log.error(f"Bluesky session failed: {e}")
        return None


def _post_from_item(item: dict, source_query="", source_type="search",
                    source_game=None, source_label=None) -> dict:
    author = item.get("author", {}) or {}
    record = item.get("record", {}) or {}
    return make_post(
        id=item.get("uri", ""),
        user=author.get("handle", ""),
        user_display=author.get("displayName", ""),
        text=record.get("text", ""),
        published=record.get("createdAt", ""),
        likes=item.get("likeCount", 0),
        reposts=item.get("repostCount", 0),
        replies=item.get("replyCount", 0),
        url=_web_url(item.get("uri", ""), author.get("handle", "")),
        source="bluesky",
        source_query=source_query,
        source_type=source_type,
        source_game=source_game,
        source_label=source_label,
        lang=(record.get("langs") or [""])[0],
    )


def _web_url(at_uri: str, handle: str) -> str:
    # at://did:plc:xxx/app.bsky.feed.post/rkey -> https://bsky.app/profile/handle/post/rkey
    rkey = at_uri.rsplit("/", 1)[-1] if at_uri else ""
    return f"https://bsky.app/profile/{handle}/post/{rkey}" if handle and rkey else ""


def search(query: str, limit: int = 25, source_game=None, source_label=None) -> list:
    jwt = _get_jwt()
    if not jwt:
        log.warning("Bluesky search disabled: set BLUESKY_HANDLE / BLUESKY_APP_PASSWORD")
        return []
    data = get_json(
        f"{AUTH_BASE}/app.bsky.feed.searchPosts",
        params={"q": query, "limit": min(limit, 100), "sort": "latest", "lang": "en"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    time.sleep(PACE_SECONDS)
    if not data:
        return []
    posts = [_post_from_item(p, source_query=query, source_type="search",
                             source_game=source_game, source_label=source_label)
             for p in data.get("posts", [])]
    log.info(f"bluesky search '{query}' -> {len(posts)} posts")
    return posts


def fetch_author_feed(handle: str, limit: int = 20) -> list:
    """Recent posts from a tracked Bluesky account."""
    data = get_json(
        f"{BASE}/app.bsky.feed.getAuthorFeed",
        params={"actor": handle, "limit": min(limit, 100), "filter": "posts_no_replies"},
    )
    time.sleep(PACE_SECONDS)
    if not data:
        return []
    posts = []
    for item in data.get("feed", []):
        post = item.get("post")
        if post:
            posts.append(_post_from_item(post, source_query=handle,
                                         source_type="timeline"))
    log.info(f"bluesky feed @{handle} -> {len(posts)} posts")
    return posts
