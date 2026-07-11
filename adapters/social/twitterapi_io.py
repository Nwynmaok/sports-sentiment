"""
adapters/social/twitterapi_io.py
Optional paid Twitter/X source via twitterapi.io (~$0.15/1K tweets).
Only active when TWITTERAPI_IO_KEY is set; the pipeline runs fine
without it on Reddit + Bluesky alone.

Used primarily for tracked sharp-account timelines, which are
Twitter-native and don't transfer to the free platforms.
"""

import os
import time
import logging

from .base import make_post, get_json

log = logging.getLogger("pipeline.social.twitter")

BASE = "https://api.twitterapi.io/twitter"
PACE_SECONDS = 1.0


def api_key() -> str:
    return os.environ.get("TWITTERAPI_IO_KEY", "")


def enabled() -> bool:
    return bool(api_key())


def _media_from_tweet(t: dict) -> list:
    """Normalize attached media. Sharps often post their card as an image
    (or a video scrolling through picks); the analysis layer runs vision
    extraction on these, so capture photo URLs, video preview frames, and
    the best mp4 variant. Media schema: {type, image_url, video_url}."""
    entries = ((t.get("extendedEntities") or t.get("extended_entities") or {})
               .get("media")
               or (t.get("entities") or {}).get("media")
               or t.get("media") or [])
    out = []
    for m in entries:
        if not isinstance(m, dict):
            continue
        mtype = m.get("type", "photo")
        image = (m.get("media_url_https") or m.get("media_url")
                 or m.get("url_https") or "")
        video = ""
        variants = (m.get("video_info") or {}).get("variants") or m.get("variants") or []
        mp4s = [v for v in variants
                if isinstance(v, dict) and "mp4" in (v.get("content_type") or v.get("contentType") or "")]
        if mp4s:
            video = max(mp4s, key=lambda v: v.get("bitrate", 0) or 0).get("url", "")
        if image or video:
            out.append({"type": mtype, "image_url": image, "video_url": video})
    return out


def _post_from_tweet(t: dict, source_query="", source_type="search",
                     source_game=None, source_label=None) -> dict:
    author = t.get("author", {}) or {}
    return make_post(
        id=str(t.get("id", "")),
        user=author.get("userName", author.get("username", "")),
        user_display=author.get("name", ""),
        user_followers=author.get("followers", author.get("followersCount", 0)),
        user_verified=bool(author.get("isBlueVerified") or author.get("isVerified")),
        text=t.get("text", ""),
        published=t.get("createdAt", ""),
        likes=t.get("likeCount", 0),
        reposts=t.get("retweetCount", 0),
        replies=t.get("replyCount", 0),
        views=t.get("viewCount", 0) or 0,
        url=t.get("url", t.get("twitterUrl", "")),
        source="twitter",
        source_query=source_query,
        source_type=source_type,
        source_game=source_game,
        source_label=source_label,
        lang=t.get("lang", ""),
        media=_media_from_tweet(t),
    )


def _get(path: str, params: dict):
    if not enabled():
        return None
    return get_json(f"{BASE}/{path}", params=params,
                    headers={"X-API-Key": api_key()})


def search(query: str, limit: int = 20, source_game=None, source_label=None) -> list:
    data = _get("tweet/advanced_search",
                params={"query": query, "queryType": "Latest"})
    time.sleep(PACE_SECONDS)
    if not data:
        return []
    tweets = data.get("tweets", []) or []
    posts = [_post_from_tweet(t, source_query=query, source_type="search",
                              source_game=source_game, source_label=source_label)
             for t in tweets[:limit]]
    log.info(f"twitter search '{query}' -> {len(posts)} posts")
    return posts


def fetch_timeline(handle: str, limit: int = 20) -> list:
    """Recent tweets from a tracked account."""
    data = _get("user/last_tweets",
                params={"userName": handle.lstrip("@"), "limit": limit})
    time.sleep(PACE_SECONDS)
    if not data:
        return []
    tweets = data.get("tweets") or data.get("data", {}).get("tweets") or []
    posts = [_post_from_tweet(t, source_query=handle, source_type="timeline")
             for t in tweets[:limit]]
    log.info(f"twitter timeline @{handle} -> {len(posts)} posts")
    return posts
