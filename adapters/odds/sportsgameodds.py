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
        params={"leagueID": sport_cfg.sgo_league_id, "startsAfter": date,
                "startsBefore": date, "oddsAvailable": "true"},
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

    prop_stats = {m.get("key"): m.get("stat", m.get("key"))
                  for m in sport_cfg.prop_markets}

    for ev in events:
        teams = ev.get("teams", {})
        home = (teams.get("home") or {}).get("names", {}).get("long", "") or \
               (teams.get("home") or {}).get("name", "")
        away = (teams.get("away") or {}).get("names", {}).get("long", "") or \
               (teams.get("away") or {}).get("name", "")
        if not home or not away:
            continue
        game = make_game(away, home, game_time=ev.get("status", {}).get("startsAt", ""))

        players = ev.get("players", {}) or {}
        seen_props = set()

        for odd_id, odd in (ev.get("odds") or {}).items():
            parts = odd_id.split("-")
            if len(parts) != 5:
                continue
            stat, entity, period, bet_type, side = parts
            if period != "game":
                continue

            price = odd.get("bookOdds") or odd.get("odds")
            line = odd.get("bookOverUnder") or odd.get("overUnder") or \
                odd.get("bookSpread") or odd.get("spread")

            if bet_type == "ml" and entity in ("home", "away"):
                try:
                    game[f"{entity}_ml"] = int(str(price).replace("+", ""))
                except (ValueError, TypeError):
                    pass
            elif bet_type == "sp" and entity == "home" and side == "home":
                try:
                    game["spread"]["line"] = float(line)
                except (ValueError, TypeError):
                    pass
            elif bet_type == "ou" and entity == "all" and side == "over":
                try:
                    game["total"] = float(line)
                except (ValueError, TypeError):
                    pass
            elif bet_type == "ou" and entity not in ("home", "away", "all"):
                # player prop
                canonical = prop_stats.get(stat, stat)
                pinfo = players.get(entity, {})
                pname = pinfo.get("name") or _title_from_entity(entity)
                key = (pname, canonical)
                if side == "over" and key not in seen_props:
                    seen_props.add(key)
                    game["props"].append({
                        "player": pname,
                        "stat": canonical,
                        "line": line,
                        "best_over": str(price) if price is not None else "N/A",
                        "best_under": "N/A",
                    })

        matchup = f"{away} @ {home}"
        out["games"][matchup] = game

    log.info(f"SGO {sport_cfg.sgo_league_id}: {len(out['games'])} games, "
             f"{sum(len(g['props']) for g in out['games'].values())} props")
    return out
