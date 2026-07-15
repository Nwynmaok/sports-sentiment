"""
adapters/odds/base.py
Common shape for odds adapters. Every adapter returns "game data":

    {
      "date": "YYYY-MM-DD",
      "generated_at": iso,
      "source": "espn" | "sportsgameodds",
      "games": {
        "Away Team @ Home Team": {
          "away": ..., "home": ..., "game_time": ...,
          "away_ml": int|None, "home_ml": int|None,
          "spread": {"home": ..., "away": ..., "line": float|None},
          "total": float|None,
          "props": [{"player", "stat", "line", "best_over", "best_under"}],
        }
      }
    }

This matches what core.queries and core.matcher consume (same shape the
original nba-sentiment pipeline used).
"""

from datetime import datetime


def empty_game_data(date: str, source: str) -> dict:
    return {
        "date": date,
        "generated_at": datetime.now().isoformat(),
        "source": source,
        "games": {},
    }


def make_game(away: str, home: str, game_time: str = "") -> dict:
    return {
        "away": away,
        "home": home,
        "game_time": game_time,
        "away_ml": None,
        "home_ml": None,
        "spread": {"home": home, "away": away, "line": None},
        "total": None,
        "props": [],
    }


def find_slate_game(games: dict, away: str, home: str, team_keywords: dict):
    """Match a props provider's team names to an existing slate key.
    Exact "Away @ Home" first, then via team_keywords (keyword -> full
    name) so e.g. 'LA Sparks' still lands on 'Los Angeles Sparks'."""
    if not away or not home:
        return None
    key = f"{away} @ {home}"
    if key in games:
        return key

    def to_full(name):
        for kw, full in team_keywords.items():
            if kw.lower() in name.lower():
                return full
        return name

    key = f"{to_full(away)} @ {to_full(home)}"
    return key if key in games else None


def _norm_stat_text(text: str) -> str:
    for ch in ("-", ",", "+", "."):
        text = text.replace(ch, " ")
    return " ".join(text.lower().split())


def resolve_stat(stat_text: str, sport_cfg, extras: dict = None):
    """Map a provider's stat label to a config prop_markets entry:
    provider-specific extras first, then key/stat/label, then
    stat_aliases. None means the sport doesn't track that market."""
    text = _norm_stat_text(stat_text or "")
    if extras and text in extras:
        text = extras[text]
    for m in sport_cfg.prop_markets:
        if text in (_norm_stat_text(m.get("key", "")),
                    _norm_stat_text(m.get("stat", "")),
                    _norm_stat_text(m.get("label", ""))):
            return m
    for canonical, aliases in sport_cfg.stat_aliases.items():
        if text == canonical or text in (a.lower() for a in aliases):
            for m in sport_cfg.prop_markets:
                if canonical in (m.get("key", "").lower(),
                                 m.get("stat", "").lower()):
                    return m
    return None


