"""Pipeline-dashboard delivery — posts rendered digests to the local web
dashboard instead of Telegram. Telegram remains notification-only; the
dashboard server sends the one-line nudge itself when ``nudge=True``.

Enabled by DASHBOARD_URL in .env (e.g. http://127.0.0.1:3002/api/events).
When unset, callers fall back to the legacy Telegram path. Failures are
log-and-drop by design: suggestions JSON is persisted before delivery and a
rerun re-posts idempotently via dedupe_key.
"""
import logging
import os

import requests

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5


def enabled() -> bool:
    return bool(os.environ.get("DASHBOARD_URL"))


def post_event(source: str, kind: str, title: str, body: str,
               dedupe_key: str = None, meta: dict = None,
               nudge: bool = False) -> bool:
    url = os.environ.get("DASHBOARD_URL")
    if not url:
        log.info("Dashboard delivery skipped (no DASHBOARD_URL)")
        return False
    payload = {"source": source, "kind": kind, "title": title, "body": body,
               "nudge": nudge}
    if dedupe_key is not None:
        payload["dedupe_key"] = dedupe_key
    if meta is not None:
        payload["meta"] = meta
    try:
        r = requests.post(url, json=payload, timeout=TIMEOUT_SECONDS)
        if not r.ok:
            log.error(f"Dashboard post failed ({r.status_code}): {r.text[:200]}")
            return False
        deduped = r.json().get("deduped", False)
        log.info(f"Dashboard event posted{' (deduped)' if deduped else ''}")
        return True
    except requests.RequestException as e:
        log.error(f"Dashboard post error: {e}")
        return False
