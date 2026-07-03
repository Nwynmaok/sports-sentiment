"""
adapters/odds/sportsgameodds.py
Game lines + player props from SportsGameOdds (sportsgameodds.com).
Free tier available; set SGO_API_KEY in .env. Only used when the key is
set — the pipeline falls back to ESPN (lines only) without it.

NOTE: written against the v2 docs but not yet exercised against a live
key. Parsing is defensive; if the response shape differs, the debug dump
(data/<sport>/debug/sgo_<date>.json) has the raw payload to adjust from.

SGO oddIDs follow "{stat}-{entity}-{period}-{betType}-{side}", e.g.
    points-home-game-ml-home          moneyline
    points-home-game-sp-home          spread
    points-all-game-ou-over           total
    points-FIRSTNAME_LASTNAME_1_NBA-game-ou-over   player prop
"""

import os
import json
import logging
from pathlib import Path

from ..social.base import get_json
from .base import empty_game_data, make_game

log = logging.getLogger("pipeline.odds.sgo")

BASE = "https://api.sportsgameodds.com/v2"


def api_key() -> str:
    return os.environ.get("SGO_API_KEY", "")


def enabled() -> bool:
    return bool(api_key())


def _title_from_entity(entity: str) -> str:
    # "SHAI_GILGEOUS_ALEXANDER_1_NBA" -> "Shai Gilgeous Alexander"
    parts = entity.split("_")
    name_parts = [p for p in parts if not p.isdigit() and not p.isupper() or p.istitle()]
    # entity IDs are fully uppercase; strip trailing index + league tokens
    words = []
    for p in parts:
        if p.isdigit():
            break
        words.append(p.capitalize())
    return " ".join(words)


def fetch_game_data(sport_cfg, date: str, debug_dir: Path = None) -> dict:
    out = empty_game_data(date, "sportsgameodds")
    if not enabled():
        log.info("SGO disabled: no SGO_API_KEY set")
        return out

    data = get_json(
        f"{BASE}/events",
        params={"leagueID": sport_cfg.sgo_league_id,
                "startsAfter": f"{date}T00:00:00Z",
                "startsBefore": f"{date}T23:59:59Z",
                "oddsAvailable": "true", "limit": 50},
        headers={"X-Api-Key": api_key()},
    )
    if not data or not data.get("success", True) is True and not data.get("data"):
        log.error(f"SGO fetch failed: {str(data)[:200]}")
        return out

    events = data.get("data", data.get("events", []))
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)
        with open(debug_dir / f"sgo_{date}.json", "w") as f:
            json.dump(data, f, indent=1)

    prop_markets = {m.get("key"): m for m in sport_cfg.prop_markets}

    def _american(v):
        try:
            return int(str(v).replace("+", ""))
        except (ValueError, TypeError):
            return None

    def _num(v):
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    for ev in events:
        teams = ev.get("teams", {})
        home = (teams.get("home") or {}).get("names", {}).get("long", "")
        away = (teams.get("away") or {}).get("names", {}).get("long", "")
        if not home or not away:
            continue
        game = make_game(away, home, game_time=ev.get("status", {}).get("startsAt", ""))

        players = ev.get("players", {}) or {}
        props_acc = {}  # (playerID, statID) -> {line, over, under}

        for odd_id, odd in (ev.get("odds") or {}).items():
            parts = odd_id.split("-")
            if len(parts) != 5:
                continue
            stat, entity, period, bet_type, side = parts
            if period != "game":
                continue
            price = _american(odd.get("bookOdds") or odd.get("fairOdds"))

            if stat == "points" and bet_type == "ml" and entity in ("home", "away") \
                    and side == entity:
                game[f"{entity}_ml"] = price
            elif stat == "points" and bet_type == "sp":
                if entity == "home" and side == "home":
                    game["spread"]["line"] = _num(
                        odd.get("bookSpread") or odd.get("fairSpread"))
                    game["spread_home_odds"] = price
                elif entity == "away" and side == "away":
                    game["spread_away_odds"] = price
            elif stat == "points" and bet_type == "ou" and entity == "all":
                game["total"] = _num(
                    odd.get("bookOverUnder") or odd.get("fairOverUnder")) \
                    or game["total"]
                game[f"total_{side}_odds"] = price
            elif bet_type == "ou" and stat in prop_markets \
                    and entity not in ("home", "away", "all"):
                rec = props_acc.setdefault((entity, stat), {})
                rec["line"] = _num(
                    odd.get("bookOverUnder") or odd.get("fairOverUnder"))
                rec[side] = price

        for (player_id, stat), rec in props_acc.items():
            if rec.get("line") is None:
                continue
            pinfo = players.get(player_id, {})
            m = prop_markets[stat]
            game["props"].append({
                "player": pinfo.get("name") or _title_from_entity(player_id),
                "player_id": player_id,
                "stat": m.get("stat", stat),
                "stat_key": stat,
                "line": rec["line"],
                "over_odds": rec.get("over"),
                "under_odds": rec.get("under"),
                "best_over": str(rec.get("over", "N/A")),
                "best_under": str(rec.get("under", "N/A")),
            })

        matchup = f"{away} @ {home}"
        out["games"][matchup] = game

    log.info(f"SGO {sport_cfg.sgo_league_id}: {len(out['games'])} games, "
             f"{sum(len(g['props']) for g in out['games'].values())} props")
    return out
