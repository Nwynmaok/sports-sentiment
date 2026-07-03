"""
pipeline/news_dedup.py
Two-layer noise reduction for INJURY_NEWS alerts:

1. In-run dedup — the same news post matched to several buckets (a
   game's spread AND total, or multiple props) should alert once.
2. Cross-run cooldown — a news post already alerted in a previous run
   is suppressed for COOLDOWN_DAYS, so daily runs don't re-announce
   yesterday's transaction-bot posts. Seen keys persist in
   data/<sport>/state/news_seen.json.
"""

import json
import hashlib
import logging
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("pipeline.news_dedup")

COOLDOWN_DAYS = 3


def _news_key(alert: dict) -> str:
    """Stable key for a news alert's underlying post (user + text prefix)."""
    msg = alert.get("message", "")
    # message format: "News: <game> / <prop>: @<user> — <text>"
    payload = msg.split(": @", 1)[-1][:150]
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _load_seen(state_path: Path) -> dict:
    if state_path.exists():
        try:
            with open(state_path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            log.warning(f"Corrupt news state at {state_path}, starting fresh")
    return {}


def filter_news_alerts(alert_data: dict, state_dir: Path, now: datetime = None) -> dict:
    """Mutates alert_data: drops duplicate/cooled-down INJURY_NEWS alerts,
    recomputes the summary, persists the seen-store."""
    now = now or datetime.now()
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "news_seen.json"
    seen = _load_seen(state_path)

    # Expire old entries
    cutoff = (now - timedelta(days=COOLDOWN_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}

    kept = []
    dropped_dup = dropped_cooldown = 0
    run_keys = set()
    for alert in alert_data.get("alerts", []):
        if alert.get("type") != "INJURY_NEWS":
            kept.append(alert)
            continue
        key = _news_key(alert)
        if key in run_keys:
            dropped_dup += 1
            continue
        if key in seen:
            dropped_cooldown += 1
            continue
        run_keys.add(key)
        seen[key] = now.isoformat()
        kept.append(alert)

    alert_data["alerts"] = kept

    summary = alert_data.get("summary", {})
    summary["total_alerts"] = len(kept)
    for p in (1, 2, 3):
        summary[f"priority_{p}"] = sum(1 for a in kept if a["priority"] == p)
    types = {}
    for a in kept:
        types[a["type"]] = types.get(a["type"], 0) + 1
    summary["types"] = types
    summary["news_suppressed"] = {"duplicates": dropped_dup,
                                  "cooldown": dropped_cooldown}

    with open(state_path, "w") as f:
        json.dump(seen, f, indent=1)

    if dropped_dup or dropped_cooldown:
        log.info(f"News alerts suppressed: {dropped_dup} in-run duplicates, "
                 f"{dropped_cooldown} on cooldown")
    return alert_data
