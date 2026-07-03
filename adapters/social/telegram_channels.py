"""
adapters/social/telegram_channels.py
Reads public Telegram channels (capper/picks channels) via the MTProto
API using Telethon. Free; credentials are instant:

    1. https://my.telegram.org -> API development tools -> create app
       -> set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env
    2. One-time login (sends a code to your Telegram app):
       python3 -m scripts.telegram_login
       This writes telegram.session in the repo root (gitignored).

Channels to read go in the sport config as "telegram_channels":
["channelusername", ...] (the @-name of any public channel).

Channel posts are authored by the channel itself, so add channel
usernames to accounts.json to weight them as tracked/sharp sources.

Note: TELEGRAM_API_ID/API_HASH (reading, this module) are different
credentials from TELEGRAM_BOT_TOKEN/CHAT_ID (alert delivery).
"""

import os
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .base import make_post

log = logging.getLogger("pipeline.social.telegram")

SESSION_PATH = Path(__file__).resolve().parent.parent.parent / "telegram.session"
MESSAGES_PER_CHANNEL = 50
LOOKBACK_HOURS = 36

try:
    from telethon.sync import TelegramClient
    TELETHON_AVAILABLE = True
except ImportError:
    TELETHON_AVAILABLE = False


def creds():
    return (os.environ.get("TELEGRAM_API_ID", ""),
            os.environ.get("TELEGRAM_API_HASH", ""))


def enabled() -> bool:
    api_id, api_hash = creds()
    if not (api_id and api_hash):
        return False
    if not TELETHON_AVAILABLE:
        log.warning("Telegram creds set but telethon not installed "
                    "(pip3 install telethon)")
        return False
    if not SESSION_PATH.exists():
        log.warning("Telegram creds set but no session — run: "
                    "python3 -m scripts.telegram_login")
        return False
    return True


def _post_from_message(msg, channel: str) -> dict:
    published = ""
    if msg.date:
        published = msg.date.astimezone(timezone.utc).isoformat()
    reactions = 0
    if getattr(msg, "reactions", None) and msg.reactions.results:
        reactions = sum(r.count for r in msg.reactions.results)
    return make_post(
        id=f"tg-{channel}-{msg.id}",
        user=channel,
        text=(msg.message or "")[:2000],
        published=published,
        likes=reactions,
        views=getattr(msg, "views", 0) or 0,
        replies=(getattr(msg, "replies", None) and msg.replies.replies) or 0,
        url=f"https://t.me/{channel}/{msg.id}",
        source="telegram",
        source_query=channel,
        source_type="timeline",
    )


def fetch_channels(channels: list) -> list:
    """Recent messages from a list of public channel usernames."""
    if not enabled() or not channels:
        return []
    api_id, api_hash = creds()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    posts = []
    try:
        with TelegramClient(str(SESSION_PATH.with_suffix("")), int(api_id), api_hash) as client:
            for channel in channels:
                channel = channel.lstrip("@")
                try:
                    count = 0
                    for msg in client.iter_messages(channel, limit=MESSAGES_PER_CHANNEL):
                        if msg.date and msg.date.astimezone(timezone.utc) < cutoff:
                            break
                        if msg.message:
                            posts.append(_post_from_message(msg, channel))
                            count += 1
                    log.info(f"telegram @{channel} -> {count} posts")
                except Exception as e:
                    log.warning(f"telegram @{channel} failed: {e}")
    except Exception as e:
        log.error(f"Telegram client failed: {e}")
    return posts
