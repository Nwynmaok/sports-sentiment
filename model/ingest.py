"""
model/ingest.py
Historical game results for the model layer, built on the ESPN scores
adapter (keyless, works for every sport pack). Incremental: results
persist in data/<sport>/stats/games.json keyed by date, and only
missing dates are fetched. A first backfill of ~90 days is ~90 requests
once; daily runs add one date.
"""

import json
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

from adapters.odds import espn

log = logging.getLogger("pipeline.model.ingest")

PACE_SECONDS = 0.3


def _store_path(data_dir: Path) -> Path:
    return data_dir / "stats" / "games.json"


def load_history(data_dir: Path) -> dict:
    """{date: [{matchup, home, away, home_score, away_score}]}"""
    path = _store_path(data_dir)
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            log.warning(f"Corrupt history at {path}, starting fresh")
    return {}


def update_history(cfg, data_dir: Path, lookback_days: int = 90,
                   end_date: str = None) -> dict:
    """Fetch final scores for any date in the window we don't have yet.
    Returns the full history dict."""
    history = load_history(data_dir)
    end = datetime.strptime(end_date, "%Y-%m-%d") if end_date else datetime.now()

    missing = []
    for i in range(1, lookback_days + 1):
        d = (end - timedelta(days=i)).strftime("%Y-%m-%d")
        if d not in history:
            missing.append(d)
    if missing:
        log.info(f"Backfilling {len(missing)} missing date(s) of "
                 f"{cfg.display_name} results")
    for d in sorted(missing):
        results = espn.fetch_scores(cfg, d)
        finals = [
            {"matchup": r["matchup"], "home": r["home"], "away": r["away"],
             "home_score": r["home_score"], "away_score": r["away_score"]}
            for r in results
            if r.get("state") == "post"
            and r.get("home_score") is not None and r.get("away_score") is not None
        ]
        # Store empty lists too — off days shouldn't be refetched forever
        history[d] = finals
        time.sleep(PACE_SECONDS)

    # Trim beyond the window so the file doesn't grow unbounded
    cutoff = (end - timedelta(days=lookback_days + 30)).strftime("%Y-%m-%d")
    history = {d: g for d, g in history.items() if d >= cutoff}

    path = _store_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(history, f)

    total_games = sum(len(g) for g in history.values())
    log.info(f"History: {total_games} completed games across "
             f"{sum(1 for g in history.values() if g)} slates")
    return history
