"""
adapters/markets/signals.py
Combines Polymarket + Kalshi win probabilities into per-game signals:

    edge  — prediction-market home prob minus the devigged sportsbook
            moneyline prob. Positive = smart money likes home more than
            the books do.
    move  — change in prediction-market home prob since the previous
            run today (steam). Snapshots persist in
            state/market_probs.json.

Thin markets (combined volume < THIN_VOLUME) are flagged so the
suggestion engine can discount them.
"""

import json
import logging
from pathlib import Path

from . import polymarket, kalshi

log = logging.getLogger("pipeline.markets")

THIN_VOLUME = 1000


def ml_to_prob(american):
    """American odds -> raw implied probability."""
    if american is None:
        return None
    try:
        a = float(american)
    except (TypeError, ValueError):
        return None
    if a < 0:
        return -a / (-a + 100.0)
    return 100.0 / (a + 100.0)


def devig_home_prob(home_ml, away_ml):
    """Two-way devig: normalize the raw implied probs."""
    ph, pa = ml_to_prob(home_ml), ml_to_prob(away_ml)
    if ph is None or pa is None or ph + pa == 0:
        return None
    return ph / (ph + pa)


def build_signals(cfg, date: str, game_data: dict, state_dir: Path) -> dict:
    """{matchup: {pm_home_prob, book_home_prob, edge, move, volume,
    thin, sources}} for every slate game a prediction market prices."""
    games = game_data.get("games", {})
    if not games:
        return {}

    pm = polymarket.fetch_game_probs(cfg, date, games)
    ks = kalshi.fetch_game_probs(cfg, date, games)

    combined = {}
    for matchup in set(pm) | set(ks):
        entries = [e for e in (pm.get(matchup), ks.get(matchup)) if e]
        home_prob = sum(e["home_prob"] for e in entries) / len(entries)
        volume = sum(e["volume"] for e in entries)
        combined[matchup] = {
            "pm_home_prob": round(home_prob, 4),
            "volume": round(volume, 2),
            "thin": volume < THIN_VOLUME,
            "sources": [e["source"] for e in entries],
        }

    # Edge vs the sportsbook line
    for matchup, sig in combined.items():
        g = games.get(matchup, {})
        book = devig_home_prob(g.get("home_ml"), g.get("away_ml"))
        sig["book_home_prob"] = round(book, 4) if book is not None else None
        sig["edge"] = (round(sig["pm_home_prob"] - book, 4)
                       if book is not None else None)

    # Steam: delta vs previous snapshot from the same day
    state_dir.mkdir(parents=True, exist_ok=True)
    snap_path = state_dir / "market_probs.json"
    prev = {}
    if snap_path.exists():
        try:
            with open(snap_path) as f:
                prev = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    prev_probs = prev.get("probs", {}) if prev.get("date") == date else {}
    for matchup, sig in combined.items():
        before = prev_probs.get(matchup)
        sig["move"] = (round(sig["pm_home_prob"] - before, 4)
                       if before is not None else None)
    with open(snap_path, "w") as f:
        json.dump({"date": date,
                   "probs": {m: s["pm_home_prob"] for m, s in combined.items()}},
                  f, indent=1)

    priced_edges = sum(1 for s in combined.values() if s.get("edge") is not None)
    log.info(f"prediction markets: {len(combined)}/{len(games)} games priced, "
             f"{priced_edges} with book edge computable")
    return combined
