"""
adapters/odds/espn.py
Game lines from ESPN's undocumented site API. Free, no key, covers 20+
leagues via the sport config's espn.league_path (e.g. "baseball/mlb",
"basketball/nba", "football/nfl").

Provides moneyline / spread / total (usually DraftKings). No player
props — pair with the SportsGameOdds adapter when props matter.
Unofficial endpoints: shapes can change without notice, so parsing is
defensive throughout.
"""

import logging
from datetime import datetime

from ..social.base import get_json  # same retry/UA helper
from .base import empty_game_data, make_game

log = logging.getLogger("pipeline.odds.espn")

BASE = "https://site.api.espn.com/apis/site/v2/sports"


def _parse_int(s):
    try:
        return int(str(s).replace("+", ""))
    except (ValueError, TypeError):
        return None


def fetch_game_data(sport_cfg, date: str) -> dict:
    """Fetch a day's slate + lines for the sport. date: YYYY-MM-DD."""
    league_path = sport_cfg.espn_league_path
    out = empty_game_data(date, "espn")
    if not league_path:
        log.warning(f"No espn.league_path in {sport_cfg.key} config")
        return out

    data = get_json(f"{BASE}/{league_path}/scoreboard",
                    params={"dates": date.replace("-", "")})
    if not data:
        log.error("ESPN scoreboard fetch failed")
        return out

    for ev in data.get("events", []):
        comps = ev.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]

        home = away = None
        for c in comp.get("competitors", []):
            name = c.get("team", {}).get("displayName", "")
            if c.get("homeAway") == "home":
                home = name
            elif c.get("homeAway") == "away":
                away = name
        if not home or not away:
            continue

        game = make_game(away, home, game_time=ev.get("date", ""))

        odds_list = comp.get("odds") or []
        if odds_list:
            o = odds_list[0]
            game["total"] = o.get("overUnder")

            ml = o.get("moneyline") or {}
            game["home_ml"] = _parse_int(
                (ml.get("home") or {}).get("close", {}).get("odds"))
            game["away_ml"] = _parse_int(
                (ml.get("away") or {}).get("close", {}).get("odds"))

            # ESPN's "spread" is the favorite's line; express it as the
            # home team's line like the rest of the pipeline expects.
            spread = o.get("spread")
            if spread is not None:
                home_fav = (o.get("homeTeamOdds") or {}).get("favorite", False)
                game["spread"]["line"] = spread if home_fav else -spread
            game["odds_details"] = o.get("details", "")
            game["odds_provider"] = (o.get("provider") or {}).get("displayName", "")

        matchup = f"{away} @ {home}"
        out["games"][matchup] = game

    log.info(f"ESPN {league_path}: {len(out['games'])} games for {date}")
    return out


def fetch_scores(sport_cfg, date: str) -> list:
    """Final/live scores for results tracking. Returns
    [{matchup, home, away, home_score, away_score, state}]."""
    league_path = sport_cfg.espn_league_path
    data = get_json(f"{BASE}/{league_path}/scoreboard",
                    params={"dates": date.replace("-", "")})
    if not data:
        return []
    results = []
    for ev in data.get("events", []):
        comps = ev.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        entry = {"state": ev.get("status", {}).get("type", {}).get("state", "")}
        for c in comp.get("competitors", []):
            side = c.get("homeAway")
            if side in ("home", "away"):
                entry[side] = c.get("team", {}).get("displayName", "")
                try:
                    entry[f"{side}_score"] = int(c.get("score"))
                except (ValueError, TypeError):
                    entry[f"{side}_score"] = None
        if entry.get("home") and entry.get("away"):
            entry["matchup"] = f"{entry['away']} @ {entry['home']}"
            results.append(entry)
    return results
