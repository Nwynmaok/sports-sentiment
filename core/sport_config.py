"""
core/sport_config.py
Loads a sport pack from sports/<key>/. A sport pack is:

    sports/<key>/
        config.json           required — sport keys, stat aliases, team keywords,
                              subreddits, query templates overrides
        accounts.json         optional — tracked sharp/news/analytics accounts
        players.json          optional — target players for prop tracking
        player_team_map.json  optional — player -> team abbreviation

Everything sport-specific in the pipeline flows through this object; the
core analysis modules never hardcode a sport.
"""

import json
from pathlib import Path

SPORTS_DIR = Path(__file__).resolve().parent.parent / "sports"


class SportConfig:
    def __init__(self, key: str, base_dir: Path = None):
        self.key = key
        self.dir = (base_dir or SPORTS_DIR) / key
        cfg_path = self.dir / "config.json"
        if not cfg_path.exists():
            available = sorted(p.name for p in (base_dir or SPORTS_DIR).iterdir()
                               if (p / "config.json").exists())
            raise FileNotFoundError(
                f"No sport config at {cfg_path}. Available sports: {available}")
        with open(cfg_path) as f:
            self.raw = json.load(f)

    # ── identity ─────────────────────────────────────────────────────
    @property
    def display_name(self) -> str:
        return self.raw.get("display_name", self.key.upper())

    # ── odds provider keys ───────────────────────────────────────────
    @property
    def espn_league_path(self) -> str:
        """e.g. 'basketball/nba' for ESPN's site API."""
        return self.raw.get("espn", {}).get("league_path", "")

    @property
    def sgo_league_id(self) -> str:
        """SportsGameOdds leagueID, e.g. 'NBA'."""
        return self.raw.get("sportsgameodds", {}).get("league_id", self.key.upper())

    @property
    def prop_markets(self) -> list:
        """[{key, label, stat}] prop markets to request from odds providers."""
        return self.raw.get("prop_markets", [])

    # ── language / matching ──────────────────────────────────────────
    @property
    def stat_aliases(self) -> dict:
        """{canonical_stat: [aliases...]} for query generation + matching."""
        return self.raw.get("stat_aliases", {})

    @property
    def team_keywords(self) -> dict:
        """{keyword: full_team_name} for matching posts to games."""
        return self.raw.get("team_keywords", {})

    # ── social sources ───────────────────────────────────────────────
    @property
    def subreddits(self) -> list:
        return self.raw.get("subreddits", [])

    @property
    def bluesky_extra_terms(self) -> list:
        """Terms appended to Bluesky queries to keep results on-topic."""
        return self.raw.get("bluesky_extra_terms", [])

    @property
    def telegram_channels(self) -> list:
        """Public Telegram channel usernames to read."""
        return self.raw.get("telegram_channels", [])

    @property
    def chan_boards(self) -> list:
        """4chan boards to scan (e.g. ["sp"])."""
        return self.raw.get("chan_boards", [])

    # ── sport pack data files ────────────────────────────────────────
    def _load_json(self, name: str, default):
        path = self.dir / name
        if path.exists():
            with open(path) as f:
                return json.load(f)
        return default

    @property
    def accounts(self) -> dict:
        return self._load_json("accounts.json", {})

    @property
    def players(self) -> dict:
        return self._load_json("players.json", {})

    @property
    def player_team_map(self) -> dict:
        return self._load_json("player_team_map.json", {})


def load_sport(key: str, base_dir: Path = None) -> SportConfig:
    return SportConfig(key, base_dir)


def available_sports(base_dir: Path = None) -> list:
    root = base_dir or SPORTS_DIR
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "config.json").exists())
