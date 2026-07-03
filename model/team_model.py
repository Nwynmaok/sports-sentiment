"""
model/team_model.py
Team-level scoring model, sport-generic: works on nothing but final
scores, so one implementation covers MLB runs, NBA points, NFL points.

Ratings are multiplicative strength-of-schedule-free baselines:
    off(team) = avg scored / league avg
    def(team) = avg allowed / league avg
    exp_home  = league_avg * off(home) * def(away) * home_bump
Margins and totals are treated as normal with empirical sigmas from the
same history window — deliberately conservative (wider tails -> fewer
false +EV signals).

This is a baseline, not an oracle: it knows nothing about starters,
injuries, or rest. Its job is to flag prices that disagree with plain
scoring form; the grading loop decides whether it earns weight.
"""

import math
import logging
from collections import defaultdict

log = logging.getLogger("pipeline.model")

DEFAULTS = {"lookback_games": 25, "min_games": 10}


def norm_sf(x, mu, sigma):
    """P(X > x) for X ~ N(mu, sigma)."""
    if sigma <= 0:
        return 0.5
    return 0.5 * math.erfc((x - mu) / (sigma * math.sqrt(2)))


class TeamModel:
    def __init__(self, history: dict, lookback_games: int = 25,
                 min_games: int = 10):
        self.lookback = lookback_games
        self.min_games = min_games
        self._fit(history)

    def _fit(self, history: dict):
        # Per-team chronological game lines
        team_games = defaultdict(list)
        totals, margins, home_pts, away_pts = [], [], [], []
        for date in sorted(history):
            for g in history[date]:
                hs, as_ = g["home_score"], g["away_score"]
                team_games[g["home"]].append((hs, as_))
                team_games[g["away"]].append((as_, hs))
                totals.append(hs + as_)
                margins.append(hs - as_)
                home_pts.append(hs)
                away_pts.append(as_)

        self.n_games = len(totals)
        if not totals:
            self.league_avg = self.sigma_total = self.sigma_margin = None
            self.home_adv = 0.0
            self.ratings = {}
            return

        self.league_avg = (sum(home_pts) + sum(away_pts)) / (2 * len(totals))
        # Additive home advantage from mean margin. (A scoring RATIO is
        # biased in MLB: home teams skip the bottom 9th when leading,
        # deflating home run-scoring but not who won.)
        self.home_adv = sum(margins) / len(margins)
        mu_t = sum(totals) / len(totals)
        self.sigma_total = math.sqrt(
            sum((t - mu_t) ** 2 for t in totals) / max(len(totals) - 1, 1))
        mu_m = sum(margins) / len(margins)
        self.sigma_margin = math.sqrt(
            sum((m - mu_m) ** 2 for m in margins) / max(len(margins) - 1, 1))

        self.ratings = {}
        for team, games in team_games.items():
            recent = games[-self.lookback:]
            if len(recent) < self.min_games:
                continue
            scored = sum(g[0] for g in recent) / len(recent)
            allowed = sum(g[1] for g in recent) / len(recent)
            # Shrink toward league average: recent form overstates true
            # talent, and off*def compounds the error multiplicatively.
            # n/(n+k) weighting with k=lookback halves a full-window ratio.
            n = len(recent)
            w = n / (n + self.lookback)
            self.ratings[team] = {
                "off": 1 + (scored / self.league_avg - 1) * w,
                "def": 1 + (allowed / self.league_avg - 1) * w,
                "games": n,
            }
        log.info(f"model fit: {self.n_games} games, {len(self.ratings)} rated "
                 f"teams, league avg {self.league_avg:.2f}, "
                 f"sigma total/margin {self.sigma_total:.2f}/{self.sigma_margin:.2f}")

    def project(self, home: str, away: str):
        """{exp_home, exp_away, exp_total, exp_margin, p_home_ml,
        p_over(line), p_home_cover(line)} or None if unrated."""
        rh, ra = self.ratings.get(home), self.ratings.get(away)
        if not rh or not ra or not self.league_avg:
            return None
        exp_home = self.league_avg * rh["off"] * ra["def"] + self.home_adv / 2
        exp_away = self.league_avg * ra["off"] * rh["def"] - self.home_adv / 2
        exp_total = exp_home + exp_away
        exp_margin = exp_home - exp_away
        model = self

        class Projection:
            pass
        p = Projection()
        p.exp_home = round(exp_home, 2)
        p.exp_away = round(exp_away, 2)
        p.exp_total = round(exp_total, 2)
        p.exp_margin = round(exp_margin, 2)
        p.p_home_ml = round(norm_sf(0.0, exp_margin, model.sigma_margin), 4)
        p.p_over = lambda line: round(
            norm_sf(float(line), exp_total, model.sigma_total), 4)
        p.p_home_cover = lambda home_line: round(
            norm_sf(-float(home_line), exp_margin, model.sigma_margin), 4)
        return p
