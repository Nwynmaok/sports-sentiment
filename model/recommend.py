"""
model/recommend.py
Turns model projections + slate lines into +EV recommendations.

Markets covered now (team model):
    total over/under   line from odds adapter, price assumed -110
    spread             line from odds adapter, price assumed -110
    moneyline          prices from odds adapter

Player-prop EV activates when a props source (SGO) supplies prop lines
and a player model exists — the rec shape already supports it.

A rec must clear ev_threshold (config, default +3%) AND a 2% raw
probability edge over the implied price. If a prediction-market prob
disagrees with the model by more than 8% on a moneyline, the rec is
flagged pm-conflict (two independent estimates disagreeing is a reason
for caution, not conviction).
"""

import logging

from . import ev as ev_mod
from .team_model import TeamModel

log = logging.getLogger("pipeline.model.recommend")

DEFAULT_PRICE = -110  # standard spread/total juice when no price feed
MIN_PROB_EDGE = 0.02
PM_CONFLICT = 0.08
# If the model disagrees with the market's implied prob by more than
# this, the model is presumed wrong, not the market (baseline humility)
MAX_PROB_GAP = 0.15


def _consider(recs, game_key, market, side, line, p_win, price, why_prefix,
              ev_threshold, pm_conflict=False):
    if p_win is None:
        return
    edge = p_win - ev_mod.implied_prob(price)
    value = ev_mod.expected_value(p_win, price)
    if value < ev_threshold or edge < MIN_PROB_EDGE or edge > MAX_PROB_GAP:
        return
    recs.append({
        "game": game_key,
        "market": market,
        "side": side,
        "line": line,
        "price": price,
        "p_model": p_win,
        "ev": value,
        "kelly": ev_mod.kelly_fraction(p_win, price),
        "why": f"{why_prefix}; p={p_win:.0%} vs implied "
               f"{ev_mod.implied_prob(price):.0%}; EV {value:+.1%}",
        "flags": ["pm-conflict"] if pm_conflict else [],
    })


def build_recommendations(cfg, history: dict, game_data: dict,
                          market_signals: dict = None) -> list:
    market_signals = market_signals or {}
    mcfg = cfg.raw.get("model", {})
    ev_threshold = mcfg.get("ev_threshold", 0.03)
    model = TeamModel(history,
                      lookback_games=mcfg.get("lookback_games", 25),
                      min_games=mcfg.get("min_games", 10))
    if not model.ratings:
        log.info("model: no rated teams (insufficient history)")
        return []

    # First pass: project every game, gate junk lines
    slate = []
    skipped = 0
    for game_key, g in game_data.get("games", {}).items():
        proj = model.project(g["home"], g["away"])
        if not proj:
            skipped += 1
            continue
        total_line = g.get("total")
        if total_line is not None and total_line < mcfg.get("min_total_line", 1):
            total_line = None
        slate.append((game_key, g, proj, total_line))

    # De-bias totals against the slate's market average: a scores-only
    # baseline is qualified to rank games relative to each other, not to
    # hold a league-run-environment opinion against the books.
    total_bias = 0.0
    lined = [(p.exp_total, tl) for _, _, p, tl in slate if tl is not None]
    if mcfg.get("debias_totals", True) and len(lined) >= 4:
        total_bias = sum(et - tl for et, tl in lined) / len(lined)
        log.info(f"model totals de-bias: {total_bias:+.2f} vs market avg")

    recs = []
    for game_key, g, proj, total_line in slate:
        base_why = (f"model: exp {proj.exp_away:g}-{proj.exp_home:g} "
                    f"(total {proj.exp_total:g}, margin {proj.exp_margin:+g})")

        # Totals (assume -110 both ways)
        if total_line is not None:
            p_over = proj.p_over(total_line + total_bias)
            _consider(recs, game_key, "total", "over/home", total_line,
                      p_over, DEFAULT_PRICE, base_why, ev_threshold)
            _consider(recs, game_key, "total", "under/away", total_line,
                      round(1 - p_over, 4), DEFAULT_PRICE, base_why, ev_threshold)

        # Spread (assume -110 both ways); 0.0 placeholders gated the
        # same way (MLB run lines are always +-1.5, never pk)
        spread_line = (g.get("spread") or {}).get("line")
        if (spread_line is not None
                and abs(spread_line) < mcfg.get("min_spread_line", 0)):
            spread_line = None
        if spread_line is not None:
            p_cover = proj.p_home_cover(spread_line)
            _consider(recs, game_key, "spread", "over/home", spread_line,
                      p_cover, DEFAULT_PRICE, base_why, ev_threshold)
            _consider(recs, game_key, "spread", "under/away", spread_line,
                      round(1 - p_cover, 4), DEFAULT_PRICE, base_why, ev_threshold)

        # Moneyline (real prices)
        pm_prob = (market_signals.get(game_key) or {}).get("pm_home_prob")
        conflict = (pm_prob is not None
                    and abs(proj.p_home_ml - pm_prob) > PM_CONFLICT)
        if g.get("home_ml") is not None:
            _consider(recs, game_key, "moneyline", "over/home", None,
                      proj.p_home_ml, g["home_ml"], base_why, ev_threshold,
                      pm_conflict=conflict)
        if g.get("away_ml") is not None:
            _consider(recs, game_key, "moneyline", "under/away", None,
                      round(1 - proj.p_home_ml, 4), g["away_ml"], base_why,
                      ev_threshold, pm_conflict=conflict)

    recs.sort(key=lambda r: r["ev"], reverse=True)
    log.info(f"model recs: {len(recs)} +EV candidates "
             f"({skipped} games unrated)")
    return recs
