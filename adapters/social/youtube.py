"""
adapters/social/youtube.py
Comments from betting-picks videos via the YouTube Data API v3.
Free 10,000-unit daily quota; key is instant from Google Cloud Console
(enable "YouTube Data API v3", create API key). Set YOUTUBE_API_KEY.

Quota economics: search.list costs 100 units, commentThreads.list costs 1.
One search per game + comments on the top videos ≈ 105 units/game, so a
13-game slate uses ~1,400 of the 10,000 daily units.

Video uploaders (picks channels) can be tracked in accounts.json by
channel name; commenters land in the public tier — this source mostly
feeds public sentiment.
"""

import os
import time
import logging
from datetime import datetime, timedelta, timezone

from .base import make_post, get_json

log = logging.getLogger("pipeline.social.youtube")

BASE = "https://www.googleapis.com/youtube/v3"
PACE_SECONDS = 0.3
VIDEOS_PER_SEARCH = 4
COMMENTS_PER_VIDEO = 40


def api_key() -> str:
    return os.environ.get("YOUTUBE_API_KEY", "")


def enabled() -> bool:
    return bool(api_key())


def _video_post(item: dict, source_query="", source_game=None) -> dict:
    sn = item.get("snippet", {})
    vid = (item.get("id") or {}).get("videoId", "")
    return make_post(
        id=f"yt-video-{vid}",
        user=sn.get("channelTitle", ""),
        text=f"{sn.get('title', '')}\n{sn.get('description', '')}"[:2000],
        published=sn.get("publishedAt", ""),
        url=f"https://www.youtube.com/watch?v={vid}",
        source="youtube",
        source_query=source_query,
        source_type="video",
        source_game=source_game,
        source_label="_game_general",
    )


def _comment_post(item: dict, video_id: str, source_query="",
                  source_game=None) -> dict:
    top = (item.get("snippet", {}).get("topLevelComment", {})
           .get("snippet", {}))
    return make_post(
        id=f"yt-comment-{item.get('id', '')}",
        user=top.get("authorDisplayName", "").lstrip("@"),
        text=top.get("textOriginal", top.get("textDisplay", ""))[:2000],
        published=top.get("publishedAt", ""),
        likes=top.get("likeCount", 0),
        url=f"https://www.youtube.com/watch?v={video_id}",
        source="youtube",
        source_query=source_query,
        source_type="comment",
        source_game=source_game,
        source_label="_game_general",
    )


def search_game(query: str, source_game=None) -> list:
    """One search (100 units) + comments on the top videos (1 unit each)."""
    if not enabled():
        return []
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = get_json(f"{BASE}/search", params={
        "part": "snippet", "q": query, "type": "video", "order": "relevance",
        "publishedAfter": recent, "maxResults": VIDEOS_PER_SEARCH,
        "relevanceLanguage": "en", "key": api_key(),
    })
    time.sleep(PACE_SECONDS)
    if not data:
        return []

    posts = []
    for item in data.get("items", []):
        posts.append(_video_post(item, source_query=query, source_game=source_game))
        vid = (item.get("id") or {}).get("videoId")
        if not vid:
            continue
        comments = get_json(f"{BASE}/commentThreads", params={
            "part": "snippet", "videoId": vid, "order": "relevance",
            "maxResults": COMMENTS_PER_VIDEO, "textFormat": "plainText",
            "key": api_key(),
        })
        time.sleep(PACE_SECONDS)
        if comments:
            posts.extend(_comment_post(c, vid, source_query=query,
                                       source_game=source_game)
                         for c in comments.get("items", []))

    log.info(f"youtube '{query}' -> {len(posts)} posts")
    return posts
