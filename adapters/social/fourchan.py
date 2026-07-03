"""
adapters/social/fourchan.py
Reads 4chan's sports board (/sp/) via the public read-only JSON API
(a.4cdn.org). Completely free, no key, no account.

Anonymous board: every post lands in the "public" sharp-filter tier,
which is exactly how it should be weighted — this source feeds the
public side of the sharp/public divergence signal.

API rules honored: <=1 request/second, catalog-first to avoid
pulling threads we don't need.
"""

import html
import re
import time
import logging
from datetime import datetime, timezone

from .base import make_post, get_json

log = logging.getLogger("pipeline.social.4chan")

BASE = "https://a.4cdn.org"
PACE_SECONDS = 1.1
MAX_THREADS_PER_BOARD = 8

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(com: str) -> str:
    """4chan comments are HTML: strip tags, unescape entities."""
    if not com:
        return ""
    text = com.replace("<br>", "\n").replace("<wbr>", "")
    text = _TAG_RE.sub("", text)
    return html.unescape(text)


def _post_from_reply(reply: dict, board: str, thread_no: int,
                     source_query="") -> dict:
    ts = reply.get("time")
    published = (datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                 if ts else "")
    subject = _clean(reply.get("sub", ""))
    body = _clean(reply.get("com", ""))
    text = f"{subject}\n{body}".strip() if subject else body
    no = reply.get("no", 0)
    return make_post(
        id=f"{board}/{no}",
        user=f"anon-{board}",
        text=text[:2000],
        published=published,
        replies=reply.get("replies", 0),
        url=f"https://boards.4chan.org/{board}/thread/{thread_no}#p{no}",
        source="4chan",
        source_query=source_query,
        source_type="board",
    )


def _thread_matches(op: dict, keywords: list) -> bool:
    blob = f"{_clean(op.get('sub', ''))} {_clean(op.get('com', ''))}".lower()
    return any(kw.lower() in blob for kw in keywords)


def fetch_board(board: str, keywords: list, replies_per_thread: int = 40) -> list:
    """Scan the board catalog for threads mentioning any keyword (team
    nicknames, league name) and pull their recent replies."""
    catalog = get_json(f"{BASE}/{board}/catalog.json")
    time.sleep(PACE_SECONDS)
    if not catalog:
        log.warning(f"4chan /{board}/ catalog fetch failed")
        return []

    matching = []
    for page in catalog:
        for op in page.get("threads", []):
            if _thread_matches(op, keywords):
                matching.append(op)
    # Busiest threads first (game threads have high reply counts)
    matching.sort(key=lambda t: t.get("replies", 0), reverse=True)
    matching = matching[:MAX_THREADS_PER_BOARD]

    posts = []
    for op in matching:
        thread_no = op.get("no")
        subject = _clean(op.get("sub", ""))[:60] or f"thread {thread_no}"
        thread = get_json(f"{BASE}/{board}/thread/{thread_no}.json")
        time.sleep(PACE_SECONDS)
        if not thread:
            continue
        replies = thread.get("posts", [])[-replies_per_thread:]
        posts.extend(_post_from_reply(r, board, thread_no, source_query=subject)
                     for r in replies)

    log.info(f"4chan /{board}/: {len(matching)} threads matched, {len(posts)} posts")
    return posts
