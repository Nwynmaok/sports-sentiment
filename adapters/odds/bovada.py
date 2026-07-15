"""
adapters/odds/bovada.py
Player props from Bovada's public site API. Keyless. Fallback props
source when SportsGameOdds can't serve a league (e.g. WNBA sits outside
the SGO free tier and 400s every request).

Overlay adapter: it never builds the slate. Props are attached onto an
existing ESPN/SGO game_data dict so game keys stay canonical — ESPN team
names drive matching, grading, and dispatch, and a props source must not
introduce competing keys. Unofficial endpoint: shapes can change without
notice, so parsing is defensive and every failure degrades to "no props
added", never an exception.
"""

import re
import json
import logging
from datetime import datetime
from pathlib import Path

from ..social.base import get_json
from .base import find_slate_game, resolve_stat

log = logging.getLogger("pipeline.odds.bovada")

BASE = "https://www.bovada.lv/services/sports/event/v2/events/A/description"
# Bovada serves its own web app; non-browser user agents get blocked.
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/126.0 Safari/537.36"}
# Bovada stat phrasings no config alias covers (normalized form -> key).
STAT_EXTRAS = {
    "made 3 points shots": "threes",
    "points rebounds and assists": "pra",
}
SOURCE = "bovada"


def enabled(sport_cfg) -> bool:
    return bool(sport_cfg.bovada_league_path)


def _american(v):
    s = str(v).strip().upper()
    if s == "EVEN":
        return 100
    try:
        return int(s.replace("+", ""))
    except (ValueError, TypeError):
        return None


def _num(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _prop_from_market(market: dict, sport_cfg):
    """Parse one Bovada market into (player, prop_market, line, over, under)
    or None. Player prop markets read 'Total <Stat> - <Player> (<TEAM>)';
    'Milestones' ladders and 'To Record a ...' markets don't start with
    'Total' and are skipped (one-sided)."""
    if (market.get("period") or {}).get("main") is False:
        return None
    desc = market.get("description") or ""
    if " - " not in desc:
        return None
    stat_text, _, player_part = desc.partition(" - ")
    if not stat_text.lower().startswith("total "):
        return None
    m = resolve_stat(stat_text[6:], sport_cfg, STAT_EXTRAS)
    if not m:
        return None
    player = re.sub(r"\s*\([^)]*\)\s*$", "", player_part).strip()
    if not player:
        return None

    line = over_p = under_p = None
    for o in market.get("outcomes") or []:
        price = o.get("price") or {}
        otype = (o.get("type") or o.get("description") or "").upper()
        if otype.startswith("O"):
            over_p = _american(price.get("american"))
            line = _num(price.get("handicap"))
        elif otype.startswith("U"):
            under_p = _american(price.get("american"))
            if line is None:
                line = _num(price.get("handicap"))
    if line is None or (over_p is None and under_p is None):
        return None
    return player, m, line, over_p, under_p


def fill_props(sport_cfg, date: str, game_data: dict,
               debug_dir: Path = None) -> int:
    """Attach player props to game_data['games'] in place; returns the
    number of props added. Only fills (player, stat) pairs a game
    doesn't already have, so it can run after SGO partial coverage."""
    data = get_json(f"{BASE}/{sport_cfg.bovada_league_path}",
                    params={"lang": "en"}, headers=HEADERS)
    if not data or not isinstance(data, list):
        log.warning("Bovada fetch failed")
        return 0
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)
        with open(debug_dir / f"bovada_{date}.json", "w") as f:
            json.dump(data, f, indent=1)

    games = game_data.get("games", {})
    added = 0
    for section in data:
        for ev in section.get("events") or []:
            start_ms = ev.get("startTime")
            if start_ms and datetime.fromtimestamp(
                    start_ms / 1000).strftime("%Y-%m-%d") != date:
                continue
            home = away = None
            for c in ev.get("competitors") or []:
                if c.get("home"):
                    home = c.get("name")
                else:
                    away = c.get("name")
            if not home or not away:
                parts = (ev.get("description") or "").split(" @ ")
                if len(parts) == 2:
                    away, home = parts
            key = find_slate_game(games, away, home, sport_cfg.team_keywords)
            if not key:
                continue
            game = games[key]
            have = {(p.get("player"), p.get("stat_key"))
                    for p in game["props"]}
            for grp in ev.get("displayGroups") or []:
                for market in grp.get("markets") or []:
                    parsed = _prop_from_market(market, sport_cfg)
                    if not parsed:
                        continue
                    player, m, line, over_p, under_p = parsed
                    # team-total markets share the player-prop naming
                    # ("Total Points - Indiana Fever") — not props
                    if player in (game.get("away"), game.get("home")):
                        continue
                    if (player, m["key"]) in have:
                        continue
                    game["props"].append({
                        "player": player,
                        "stat": m.get("stat", m["key"]),
                        "stat_key": m["key"],
                        "line": line,
                        "over_odds": over_p,
                        "under_odds": under_p,
                        "best_over": str(over_p if over_p is not None else "N/A"),
                        "best_under": str(under_p if under_p is not None else "N/A"),
                    })
                    have.add((player, m["key"]))
                    added += 1
    log.info(f"Bovada {sport_cfg.bovada_league_path}: {added} props added")
    return added
