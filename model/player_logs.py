"""
model/player_logs.py
Player game logs from MLB's public Stats API (statsapi.mlb.com — free,
no key). Two layers of caching in data/<sport>/stats/:

    players_index.json  all active players (one request, refreshed daily)
    player_logs.json    per-player game logs, refetched at most once/day

Sport-gated via config player_stats.source == "mlb_statsapi"; the
basketball/football leagues use espn_player_logs behind the same
interface (source == "espn_site").
"""

import json
import time
import logging
from datetime import datetime
from pathlib import Path

from adapters.social.base import get_json

log = logging.getLogger("pipeline.model.players")

BASE = "https://statsapi.mlb.com/api/v1"
PACE_SECONDS = 0.25


def _norm(name: str) -> str:
    return " ".join(name.lower().replace(".", "").replace("'", "").split())


class PlayerLogs:
    def __init__(self, data_dir: Path, season: int):
        self.dir = data_dir / "stats"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.season = season
        self.today = datetime.now().strftime("%Y-%m-%d")
        self._index = None
        self._logs = self._load(self.dir / "player_logs.json")

    def _load(self, path: Path):
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _player_index(self):
        """{normalized name: player id} for all active players."""
        if self._index is not None:
            return self._index
        path = self.dir / "players_index.json"
        cached = self._load(path)
        if cached.get("fetched") == self.today:
            self._index = cached["players"]
            return self._index
        data = get_json(f"{BASE}/sports/1/players",
                        params={"season": self.season})
        time.sleep(PACE_SECONDS)
        players = {}
        for p in (data or {}).get("people", []):
            players[_norm(p.get("fullName", ""))] = p.get("id")
        if players:
            with open(path, "w") as f:
                json.dump({"fetched": self.today, "players": players}, f)
            self._index = players
        else:
            log.warning("MLB player index fetch failed; using stale cache")
            self._index = cached.get("players", {})
        log.info(f"player index: {len(self._index)} active players")
        return self._index

    def game_log(self, player_name: str, group: str) -> list:
        """[{date, stats:{...}}] this season, oldest first. Cached daily."""
        pid = self._player_index().get(_norm(player_name))
        if not pid:
            return []
        key = f"{pid}:{group}"
        cached = self._logs.get(key)
        if cached and cached.get("fetched") == self.today:
            return cached["log"]

        data = get_json(f"{BASE}/people/{pid}/stats",
                        params={"stats": "gameLog", "season": self.season,
                                "group": group})
        time.sleep(PACE_SECONDS)
        splits = []
        for s in ((data or {}).get("stats") or [{}])[0].get("splits", []):
            splits.append({"date": s.get("date", ""), "stats": s.get("stat", {})})
        splits.sort(key=lambda s: s["date"])
        self._logs[key] = {"fetched": self.today, "log": splits}
        return splits

    def stat_series(self, player_name: str, group: str, field: str,
                    before_date: str = None, sample_filter: dict = None) -> list:
        """Numeric per-game values for one stat, oldest first.

        sample_filter {field, min} drops games below a participation
        floor (pinch-hit appearances, relief cameos) so per-game rates
        reflect games like the one being priced."""
        out = []
        for g in self.game_log(player_name, group):
            if before_date and g["date"] >= before_date:
                continue
            if sample_filter:
                try:
                    gate = float(g["stats"].get(sample_filter["field"], 0))
                except (TypeError, ValueError):
                    gate = 0
                if gate < sample_filter.get("min", 0):
                    continue
            v = g["stats"].get(field)
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                continue
        return out

    def stat_on_date(self, player_name: str, group: str, field: str,
                     date: str):
        """The stat value on a specific date (for grading), or None."""
        for g in self.game_log(player_name, group):
            if g["date"] == date:
                try:
                    return float(g["stats"].get(field))
                except (TypeError, ValueError):
                    return None
        return None

    def save(self):
        with open(self.dir / "player_logs.json", "w") as f:
            json.dump(self._logs, f)


def open_logs(cfg, data_dir: Path, season: int = None):
    """Returns a player-log source or None if the sport has none yet."""
    source = cfg.raw.get("player_stats", {}).get("source")
    if source == "mlb_statsapi":
        return PlayerLogs(data_dir, season or datetime.now().year)
    if source == "espn_site":
        from . import espn_player_logs
        return espn_player_logs.EspnPlayerLogs(
            cfg.espn_league_path, data_dir, season or datetime.now().year)
    return None
