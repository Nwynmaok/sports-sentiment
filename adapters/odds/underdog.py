"""
adapters/odds/underdog.py
Player props from Underdog Fantasy's public lines API. Keyless. Last
link in the props fallback chain (SGO -> Bovada -> Underdog): DFS
coverage is thinner than a sportsbook's and alternate ladders are
one-sided, so only "balanced" (main, two-sided) lines are used.

Overlay adapter like bovada.py: attaches props onto an existing slate,
never creates games. Unofficial endpoint; defensive parsing throughout.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..social.base import get_json
from .base import find_slate_game, resolve_stat

log = logging.getLogger("pipeline.odds.underdog")

URL = "https://api.underdogfantasy.com/beta/v5/over_under_lines"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/126.0 Safari/537.36"}
# Underdog stat phrasings no config alias covers (normalized form -> key).
STAT_EXTRAS = {
    "3 pointers made": "threes",
    "pts rebs asts": "pra",
}
SOURCE = "underdog"


def enabled(sport_cfg) -> bool:
    return bool(sport_cfg.underdog_sport_id)


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


def _local_date(iso_utc: str) -> str:
    """'2026-07-16T00:00:00Z' -> local 'YYYY-MM-DD' (pipeline dates are
    local, and evening games cross midnight UTC)."""
    try:
        dt = datetime.fromisoformat(str(iso_utc).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d")


def fill_props(sport_cfg, date: str, game_data: dict,
               debug_dir: Path = None) -> int:
    """Attach player props to game_data['games'] in place; returns the
    number of props added. Skips (player, stat) pairs already present
    (e.g. filled by Bovada moments earlier)."""
    data = get_json(URL, headers=HEADERS)
    if not data or not isinstance(data, dict):
        log.warning("Underdog fetch failed")
        return 0
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)
        with open(debug_dir / f"underdog_{date}.json", "w") as f:
            json.dump(data, f, indent=1)

    sport_id = sport_cfg.underdog_sport_id
    matches = {g.get("id"): g for g in data.get("games") or []
               if g.get("sport_id") == sport_id
               and _local_date(g.get("scheduled_at")) == date}
    players = {p.get("id"): p for p in data.get("players") or []}
    appearances = {a.get("id"): a for a in data.get("appearances") or []}
    games = game_data.get("games", {})

    added = 0
    for l in data.get("over_under_lines") or []:
        # alternate ladders are one-sided; only main two-way lines are odds
        if l.get("line_type") != "balanced":
            continue
        ap_stat = (l.get("over_under") or {}).get("appearance_stat") or {}
        appearance = appearances.get(ap_stat.get("appearance_id"))
        if not appearance:
            continue
        match = matches.get(appearance.get("match_id"))
        if not match:
            continue
        m = resolve_stat(ap_stat.get("display_stat"), sport_cfg, STAT_EXTRAS)
        line = _num(l.get("stat_value"))
        if not m or line is None:
            continue
        p = players.get(appearance.get("player_id")) or {}
        player = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        if not player:
            continue
        title = match.get("full_team_names_title") or ""
        parts = title.split(" @ ")
        if len(parts) != 2:
            continue
        key = find_slate_game(games, parts[0], parts[1],
                              sport_cfg.team_keywords)
        if not key:
            continue
        game = games[key]
        if any(pr.get("player") == player and pr.get("stat_key") == m["key"]
               for pr in game["props"]):
            continue
        over_p = under_p = None
        for o in l.get("options") or []:
            if o.get("choice") == "higher":
                over_p = _american(o.get("american_price"))
            elif o.get("choice") == "lower":
                under_p = _american(o.get("american_price"))
        if over_p is None and under_p is None:
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
        added += 1
    log.info(f"Underdog {sport_id}: {added} props added")
    return added
