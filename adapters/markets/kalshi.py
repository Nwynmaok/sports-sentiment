"""
adapters/markets/kalshi.py
Game-winner probabilities from Kalshi's public market-data API. Free,
no key for read-only. Game markets live in per-sport series
(KXMLBGAME / KXNBAGAME / KXNFLGAME); each game is an event with one
market per team, identified by a city-prefix `yes_sub_title`
(e.g. "Los Angeles D" for the Dodgers). Prices are cents; mid of
yes_bid/yes_ask when quoted, last_price as fallback. Thin books are
common on sports — markets without any price are skipped, so Kalshi is
an opportunistic second source next to Polymarket.

Date filtering uses the event-ticker date token (26JUL03): close_time
lags games by days and can't be trusted.
"""

import time
import logging
from datetime import datetime

from ..social.base import get_json

log = logging.getLogger("pipeline.markets.kalshi")

BASE = "https://api.elections.kalshi.com/trade-api/v2"
PACE_SECONDS = 0.4
MAX_PAGES = 4
PAGE_SIZE = 200


def _date_token(date: str) -> str:
    return datetime.strptime(date, "%Y-%m-%d").strftime("%y%b%d").upper()


def _price(m: dict):
    bid, ask = m.get("yes_bid"), m.get("yes_ask")
    if bid and ask:
        return (bid + ask) / 2 / 100.0
    last = m.get("last_price")
    if last:
        return last / 100.0
    return None


def fetch_game_probs(cfg, date: str, games: dict) -> dict:
    series = cfg.raw.get("kalshi", {}).get("series_ticker", "")
    if not series:
        return {}
    token = _date_token(date)

    markets = []
    cursor = None
    for _ in range(MAX_PAGES):
        params = {"series_ticker": series, "limit": PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        data = get_json(f"{BASE}/markets", params=params)
        time.sleep(PACE_SECONDS)
        if not data:
            break
        markets.extend(m for m in data.get("markets", [])
                       if token in m.get("event_ticker", ""))
        cursor = data.get("cursor")
        if not cursor or not data.get("markets"):
            break

    # Group per event: one market per team
    by_event = {}
    for m in markets:
        by_event.setdefault(m["event_ticker"], []).append(m)

    out = {}
    for ev_markets in by_event.values():
        priced = [(m.get("yes_sub_title", ""), _price(m), m.get("volume") or 0)
                  for m in ev_markets]
        priced = [p for p in priced if p[0] and p[1] is not None]
        if not priced:
            continue
        # Match city prefixes to a unique slate game
        for matchup, g in games.items():
            sides = {}
            for sub_title, prob, vol in priced:
                if g["home"].startswith(sub_title):
                    sides["home"] = (prob, vol)
                elif g["away"].startswith(sub_title):
                    sides["away"] = (prob, vol)
            if not sides:
                continue
            if "home" in sides:
                home_prob = sides["home"][0]
            else:
                home_prob = 1.0 - sides["away"][0]
            volume = sum(v for _, v in sides.values())
            out[matchup] = {"home_prob": home_prob,
                            "away_prob": round(1.0 - home_prob, 4),
                            "volume": volume, "source": "kalshi"}
            break

    log.info(f"kalshi {series}: {len(out)}/{len(games)} slate games priced")
    return out
