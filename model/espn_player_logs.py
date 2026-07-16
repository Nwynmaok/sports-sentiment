"""
model/espn_player_logs.py
Player game stats from ESPN's undocumented site API (free, no key),
behind the same interface as model/player_logs.py. Two data paths:

    stat_on_date  grading — per-date box scores (scoreboard -> summary
                  per game), cached permanently once every game that
                  day is final
    stat_series   model priors — per-athlete season gamelogs, refetched
                  at most once/day; athlete ids resolved via team
                  rosters (one request per team, cached daily)

Sport-gated via config player_stats.source == "espn_site". ESPN's raw
stat keys are normalized to the canonical names in FIELD_MAPS (keyed by
the sport prefix of espn.league_path), plus derived fields like
basketball's pra and football's anytime touchdowns, so prop_markets
configs reference one stable vocabulary. Unofficial endpoints: shapes
can change without notice, so parsing is defensive throughout.
"""

import json
import time
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from adapters.social.base import get_json

log = logging.getLogger("pipeline.model.players.espn")

SITE = "https://site.api.espn.com/apis/site/v2/sports"
WEB = "https://site.web.api.espn.com/apis/common/v3/sports"
PACE_SECONDS = 0.3

# ESPN buckets slates by US Eastern dates; gamelog gameDate values are
# UTC, so convert before comparing against slate dates.
EASTERN = ZoneInfo("America/New_York")

# raw ESPN key -> canonical field. Box scores and gamelogs disagree on
# some names (rebounds vs totalRebounds), so both spellings map.
# Made-attempted composites ("2-5", "19/38") are reduced to the made
# count during parsing.
FIELD_MAPS = {
    "basketball": {
        "minutes": "minutes",
        "points": "points",
        "rebounds": "rebounds",
        "totalRebounds": "rebounds",
        "assists": "assists",
        "steals": "steals",
        "blocks": "blocks",
        "turnovers": "turnovers",
        "threePointFieldGoalsMade-threePointFieldGoalsAttempted": "threes",
    },
    "football": {
        "passingYards": "passingYards",
        "passingTouchdowns": "passingTouchdowns",
        "rushingYards": "rushingYards",
        "rushingTouchdowns": "rushingTouchdowns",
        "receivingYards": "receivingYards",
        "receptions": "receptions",
        "receivingTouchdowns": "receivingTouchdowns",
        "kickReturnTouchdowns": "kickReturnTouchdowns",
        "puntReturnTouchdowns": "puntReturnTouchdowns",
        "defensiveTouchdowns": "defensiveTouchdowns",
    },
}

# canonical field -> component fields summed to derive it
DERIVED = {
    "basketball": {
        "pra": ("points", "rebounds", "assists"),
    },
    "football": {
        # anytime TD: every non-passing way to score one
        "touchdowns": ("rushingTouchdowns", "receivingTouchdowns",
                       "kickReturnTouchdowns", "puntReturnTouchdowns",
                       "defensiveTouchdowns"),
    },
}


def _norm(name: str) -> str:
    return " ".join(name.lower().replace(".", "").replace("'", "").split())


def _parse_stat(raw):
    """'11' -> 11.0; '2-5' / '19/38' composites -> made count (2.0,
    19.0); '--', '', None -> None."""
    if raw is None:
        return None
    s = str(raw)
    for sep in ("-", "/"):
        if sep in s.lstrip("-"):
            s = s.split(sep)[0] if not s.startswith("-") else s
            break
    try:
        return float(s)
    except ValueError:
        return None


def _local_date(iso: str) -> str:
    """UTC ISO timestamp -> ESPN's Eastern slate date (YYYY-MM-DD)."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(EASTERN).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return (iso or "")[:10]


class EspnPlayerLogs:
    def __init__(self, league_path: str, data_dir: Path, season: int):
        self.league_path = league_path  # e.g. "basketball/wnba"
        sport = league_path.split("/")[0]
        self.field_map = FIELD_MAPS.get(sport, {})
        self.derived = DERIVED.get(sport, {})
        self.dir = data_dir / "stats"
        self.dir.mkdir(parents=True, exist_ok=True)
        # Cross-year leagues: ESPN labels NFL seasons by starting year
        # (a Jan 2027 playoff game is season 2026) and NBA seasons by
        # ending year (an Oct 2026 game is season 2027). Callers pass
        # calendar years; adjust here. WNBA/MLB fit one calendar year.
        month = datetime.now().month
        league = league_path.split("/")[-1]
        if league == "nfl" and month <= 6:
            season = season - 1
        elif league == "nba" and month >= 10:
            season = season + 1
        self.season = season
        self.today = datetime.now().strftime("%Y-%m-%d")
        self._index = None
        self._boxscores = self._load(self.dir / "espn_boxscores.json")
        self._gamelogs = self._load(self.dir / "espn_gamelogs.json")

    def _load(self, path: Path):
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    # ── stat normalization ───────────────────────────────────────────
    def _normalize(self, keys: list, values: list, into: dict):
        """Map one raw keys/values stat row into canonical fields."""
        for key, raw in zip(keys, values):
            field = self.field_map.get(key)
            if not field:
                continue
            val = _parse_stat(raw)
            if val is not None:
                into[field] = into.get(field, 0.0) + val

    def _finalize(self, stats: dict) -> dict:
        for field, parts in self.derived.items():
            present = [stats[p] for p in parts if p in stats]
            if present:
                stats[field] = sum(present)
        return stats

    # ── box scores by date (grading) ─────────────────────────────────
    def _boxscores_for(self, date: str) -> dict:
        """{normalized player name: {canonical stats}} for one date.
        Cached permanently once every game that day is final."""
        cached = self._boxscores.get(date)
        if cached and cached.get("final"):
            return cached["players"]

        board = get_json(f"{SITE}/{self.league_path}/scoreboard",
                         params={"dates": date.replace("-", "")})
        time.sleep(PACE_SECONDS)
        events = (board or {}).get("events", [])
        players, all_final = {}, bool(events)
        for ev in events:
            state = ev.get("status", {}).get("type", {}).get("state", "")
            if state != "post":
                all_final = False
                continue
            summary = get_json(f"{SITE}/{self.league_path}/summary",
                               params={"event": ev.get("id")})
            time.sleep(PACE_SECONDS)
            for team in (summary or {}).get("boxscore", {}).get("players", []):
                for grp in team.get("statistics", []):
                    keys = grp.get("keys", [])
                    for a in grp.get("athletes", []):
                        stats = a.get("stats") or []
                        if not stats or a.get("didNotPlay"):
                            continue
                        name = _norm(a.get("athlete", {}).get("displayName", ""))
                        if not name:
                            continue
                        self._normalize(keys, stats, players.setdefault(name, {}))
        for stats in players.values():
            self._finalize(stats)
        if board is not None:
            self._boxscores[date] = {"final": all_final, "players": players}
        log.info(f"ESPN box scores {date}: {len(players)} player lines"
                 + ("" if all_final else " (incomplete — will refetch)"))
        return players

    def stat_on_date(self, player_name: str, group: str, field: str,
                     date: str):
        """The stat value on a specific date (for grading), or None.
        group is unused — canonical fields are flat per sport."""
        stats = self._boxscores_for(date).get(_norm(player_name))
        if stats is None:
            return None
        return stats.get(field)

    # ── athlete gamelogs (model priors) ──────────────────────────────
    def _player_index(self):
        """{normalized name: athlete id} via team rosters, cached daily."""
        if self._index is not None:
            return self._index
        path = self.dir / "espn_players_index.json"
        cached = self._load(path)
        if cached.get("fetched") == self.today:
            self._index = cached["players"]
            return self._index

        players = {}
        teams_data = get_json(f"{SITE}/{self.league_path}/teams",
                              params={"limit": 50})
        time.sleep(PACE_SECONDS)
        try:
            teams = teams_data["sports"][0]["leagues"][0]["teams"]
        except (TypeError, LookupError):
            teams = []
        for entry in teams:
            team_id = entry.get("team", {}).get("id")
            if not team_id:
                continue
            roster = get_json(
                f"{SITE}/{self.league_path}/teams/{team_id}/roster")
            time.sleep(PACE_SECONDS)
            groups = (roster or {}).get("athletes", [])
            # NFL rosters group athletes by position ({items: [...]});
            # basketball rosters are a flat list
            for g in groups:
                for a in g.get("items", []) if "items" in g else [g]:
                    name = _norm(a.get("displayName", ""))
                    if name and a.get("id"):
                        players[name] = a["id"]
        if players:
            with open(path, "w") as f:
                json.dump({"fetched": self.today, "players": players}, f)
            self._index = players
        else:
            log.warning("ESPN roster index fetch failed; using stale cache")
            self._index = cached.get("players", {})
        log.info(f"ESPN player index: {len(self._index)} players")
        return self._index

    def game_log(self, player_name: str, group: str) -> list:
        """[{date, stats:{...}}] this season, oldest first. Cached daily.
        group is unused — canonical fields are flat per sport."""
        pid = self._player_index().get(_norm(player_name))
        if not pid:
            return []
        key = str(pid)
        cached = self._gamelogs.get(key)
        if cached and cached.get("fetched") == self.today:
            return cached["log"]

        data = get_json(f"{WEB}/{self.league_path}/athletes/{pid}/gamelog",
                        params={"season": self.season})
        time.sleep(PACE_SECONDS)
        if not data:
            return cached["log"] if cached else []
        names = data.get("names", [])
        dates = {eid: _local_date(ev.get("gameDate", ""))
                 for eid, ev in (data.get("events") or {}).items()}
        entries = []
        for st in data.get("seasonTypes", []):
            for cat in st.get("categories", []):
                for ev in cat.get("events", []):
                    date = dates.get(ev.get("eventId"))
                    if not date:
                        continue
                    stats = {}
                    self._normalize(names, ev.get("stats", []), stats)
                    entries.append({"date": date,
                                    "stats": self._finalize(stats)})
        entries.sort(key=lambda e: e["date"])
        self._gamelogs[key] = {"fetched": self.today, "log": entries}
        return entries

    def stat_series(self, player_name: str, group: str, field: str,
                    before_date: str = None, sample_filter: dict = None) -> list:
        """Numeric per-game values for one stat, oldest first.
        sample_filter {field, min} drops games below a participation
        floor (garbage-time cameos) — same contract as player_logs."""
        out = []
        for g in self.game_log(player_name, group):
            if before_date and g["date"] >= before_date:
                continue
            if sample_filter:
                gate = g["stats"].get(sample_filter.get("field"), 0) or 0
                if gate < sample_filter.get("min", 0):
                    continue
            v = g["stats"].get(field)
            if v is not None:
                out.append(v)
        return out

    def save(self):
        with open(self.dir / "espn_boxscores.json", "w") as f:
            json.dump(self._boxscores, f)
        with open(self.dir / "espn_gamelogs.json", "w") as f:
            json.dump(self._gamelogs, f)
