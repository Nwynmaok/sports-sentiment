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
