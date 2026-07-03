"""
adapters/markets/polymarket.py
Game-winner probabilities from Polymarket's public Gamma API. Free, no
key. Winner events carry slugs like "mlb-tb-hou-2026-07-04" (prop-style
events append suffixes like "-first-five-winner", which we skip) and
their first market lists full team names in `outcomes` with matching
`outcomePrices`.
"""

import json
import re
import time
import logging

from ..social.base import get_json

log = logging.getLogger("pipeline.markets.polymarket")

BASE = "https://gamma-api.polymarket.com"
PACE_SECONDS = 0.4
MAX_PAGES = 3
PAGE_SIZE = 100


def _parse_json_field(value):
    """Gamma encodes list fields as JSON strings."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return value or []


def fetch_game_probs(cfg, date: str, games: dict) -> dict:
    """{matchup: {home_prob, away_prob, volume, source}} for slate games
    that have a Polymarket winner market on `date`."""
    tag = cfg.raw.get("polymarket", {}).get("tag_slug", "")
    if not tag:
        return {}

    winner_slug = re.compile(rf"^{re.escape(tag)}-.*-{re.escape(date)}$")
    events = []
    for page in range(MAX_PAGES):
        batch = get_json(f"{BASE}/events", params={
            "tag_slug": tag, "closed": "false", "limit": PAGE_SIZE,
            "offset": page * PAGE_SIZE,
        })
        time.sleep(PACE_SECONDS)
        if not batch:
            break
        events.extend(e for e in batch if winner_slug.match(e.get("slug", "")))
        if len(batch) < PAGE_SIZE:
            break

    # Full team name -> (matchup, home?) index for the slate
    team_index = {}
    for matchup, g in games.items():
        team_index[g["home"]] = (matchup, True)
        team_index[g["away"]] = (matchup, False)

    out = {}
    for ev in events:
        markets = ev.get("markets") or []
        if not markets:
            continue
        m = markets[0]  # winner market is listed first on game events
        outcomes = _parse_json_field(m.get("outcomes"))
        prices = _parse_json_field(m.get("outcomePrices"))
        if len(outcomes) != 2 or len(prices) != 2:
            continue
        hits = [(team_index.get(o), float(p)) for o, p in zip(outcomes, prices)]
        if any(h[0] is None for h in hits):
            continue
        (matchup_a, is_home_a), prob_a = hits[0]
        (matchup_b, _), prob_b = hits[1]
        if matchup_a != matchup_b:
            continue
        home_prob = prob_a if is_home_a else prob_b
        away_prob = prob_b if is_home_a else prob_a
        try:
            volume = float(ev.get("volume") or 0)
        except (TypeError, ValueError):
            volume = 0.0
        out[matchup_a] = {"home_prob": home_prob, "away_prob": away_prob,
                          "volume": volume, "source": "polymarket"}

    log.info(f"polymarket {tag}: {len(out)}/{len(games)} slate games priced")
    return out
