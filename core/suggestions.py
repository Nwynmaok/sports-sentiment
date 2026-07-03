"""
core/suggestions.py
Rolls the per-market signals (aggregator output + buzz + news alerts)
into scored betting suggestions — one card per game, props nested.

Deterministic scoring, 0-100 per market:

    divergence signal (fade_public/follow_sharp)   up to 40
    sharp buzz (2+ tracked/sharp accounts)         25 + 5/extra, cap 35
    sentiment magnitude                            up to 15
    post volume                                    up to 10
    sharp mentions                                 up to 12

Confidence: A >= 60, B >= 40, C >= EMIT_THRESHOLD (28). Below that a
market contributes nothing and the game may land in "no edge".

Unresolved injury news on a game caps its confidence at B and flags
the card. One-sided public sentiment with no sharp counter does NOT
create a suggestion — it goes on the flip watch, and only surfaces as
a play if a later run sees sharps take the other side.
"""

import logging
from datetime import datetime

log = logging.getLogger("pipeline.suggestions")

EMIT_THRESHOLD = 28
CONFIDENCE_BANDS = [(60, "A"), (40, "B"), (EMIT_THRESHOLD, "C")]

PUBLIC_LOCK_THRESHOLD = 0.50


def _confidence(score: float):
    for floor, band in CONFIDENCE_BANDS:
        if score >= floor:
            return band
    return None


def _nickname(full_name: str, team_keywords: dict) -> str:
    for kw, full in team_keywords.items():
        if full == full_name:
            return kw
    return full_name.split()[-1]


def _pick_side(market_data: dict):
    """Which side does the evidence point at? Returns 'over/home',
    'under/away', or None."""
    sharp_lean = market_data.get("sharp_lean")
    if market_data.get("sharp_mentions", 0) > 0 and sharp_lean and sharp_lean != "neutral":
        return sharp_lean
    sentiment = market_data.get("sentiment")
    if sentiment is None or sentiment == 0:
        return None
    return "over/home" if sentiment > 0 else "under/away"


def _score_market(market_data: dict, buzz_accounts: int):
    """Returns (score, basis). basis 'sharp' can reach A/B; basis
    'public' (no sharp evidence at all) is capped at C — volume alone
    should never look like conviction."""
    signal = market_data.get("signal", "")
    div = market_data.get("divergence") or 0.0
    sharp_mentions = market_data.get("sharp_mentions", 0)
    sentiment = market_data.get("sentiment") or 0.0
    count = market_data.get("tweet_count", 0)

    has_sharp = (signal in ("fade_public", "follow_sharp")
                 or buzz_accounts >= 2 or sharp_mentions > 0)
    if has_sharp:
        score = 0.0
        if signal in ("fade_public", "follow_sharp"):
            score += 40 * min(div / 0.5, 1.0)
        if buzz_accounts >= 2:
            score += min(25 + 5 * (buzz_accounts - 2), 35)
        score += abs(sentiment) * 15
        score += min(count / 50.0, 1.0) * 10
        score += min(sharp_mentions, 3) * 4
        return round(score, 1), "sharp"

    # Public-only: sentiment direction with real volume behind it
    score = abs(sentiment) * 40 + min(count / 50.0, 1.0) * 20
    return round(score, 1), "public"


def _pick_text(market: str, side: str, game: dict, team_keywords: dict) -> str:
    home_nick = _nickname(game.get("home", ""), team_keywords)
    away_nick = _nickname(game.get("away", ""), team_keywords)
    if market == "moneyline":
        return f"{home_nick} ML" if side == "over/home" else f"{away_nick} ML"
    if market == "total":
        line = game.get("total")
        word = "OVER" if side == "over/home" else "UNDER"
        return f"{word} {line}" if line is not None else f"{word} (no line)"
    if market == "spread":
        line = (game.get("spread") or {}).get("line")
        if side == "over/home":
            return f"{home_nick} {line:+g}" if line is not None else f"{home_nick} side"
        return f"{away_nick} {-line:+g}" if line is not None else f"{away_nick} side"
    # prop market: "<Player> <stat>"
    word = "OVER" if side == "over/home" else "UNDER"
    return f"{market} {word}"


def _why(market_data: dict, buzz_accounts: int, basis: str = "sharp") -> str:
    parts = []
    signal = market_data.get("signal", "")
    div = market_data.get("divergence")
    if basis == "public":
        parts.append("public sentiment only — no sharp read yet")
    elif signal == "fade_public" and div:
        parts.append(f"sharp/public split {div:.2f}, fade the public "
                     f"({market_data.get('public_lean')})")
    elif signal == "follow_sharp" and div:
        parts.append(f"sharps on it (divergence {div:.2f})")
    elif market_data.get("sharp_mentions", 0) > 0:
        parts.append(f"{market_data['sharp_mentions']} sharp mention(s), aligned")
    if buzz_accounts >= 2:
        parts.append(f"{buzz_accounts} sharp accounts active")
    sentiment = market_data.get("sentiment")
    if sentiment is not None:
        parts.append(f"sentiment {sentiment:+.2f}")
    parts.append(f"{market_data.get('tweet_count', 0)} posts")
    return "; ".join(parts)


def _prop_line(prop_label: str, game: dict) -> str:
    """Find the odds line for a prop label ('Player stat') in game data."""
    for prop in game.get("props", []):
        if f"{prop.get('player')} {prop.get('stat')}" == prop_label:
            return str(prop.get("line", ""))
    return ""


def _buzz_index(alerts: list) -> dict:
    """{(game, prop_label): account_count} from BUZZ alerts."""
    idx = {}
    for a in alerts:
        if a.get("type") == "BUZZ":
            idx[(a.get("game"), a.get("prop"))] = a.get("account_count", 0)
    return idx


def _game_buzz(buzz_idx: dict, game_key: str) -> int:
    """Game-level buzz: accounts on game_general or 'multiple props'."""
    return max(buzz_idx.get((game_key, "game_general"), 0),
               buzz_idx.get((game_key, "_game_general"), 0),
               buzz_idx.get((game_key, "multiple props"), 0))


SIDE_LEAN_MIN = 0.1  # an account's avg sentiment must clear this to count


def _consensus(mdata: dict, side: str):
    """Sharp-account side convergence for a market.
    Returns (aligned_handles, opposed_handles) relative to the pick."""
    sides = mdata.get("sharp_sides") or {}
    over = sorted(h for h, s in sides.items() if s >= SIDE_LEAN_MIN)
    under = sorted(h for h, s in sides.items() if s <= -SIDE_LEAN_MIN)
    if side == "over/home":
        return over, under
    return under, over


def _consensus_evidence(aligned: list, opposed: list):
    """(points, why_bits, flags, aligned) from convergence. Convergence
    (2+ sharps, same side, none opposed) outranks raw buzz; a split
    among sharps is a warning, not a signal."""
    n_a, n_o = len(aligned), len(opposed)
    if n_a >= 2 and n_o == 0:
        pts = min(30 + 5 * (n_a - 2), 45)
        why = [f"{n_a} sharps converging: "
               + ", ".join("@" + h for h in aligned[:4])]
        return pts, why, ["consensus"]
    if n_a >= 1 and n_o >= 1:
        return -5.0, [f"sharps split ({n_a} with, {n_o} against)"], ["sharps-split"]
    if n_o >= 2 and n_a == 0:
        return -15.0, [f"{n_o} sharps on the OTHER side"], ["sharps-against"]
    return 0.0, [], []


def _market_evidence(sig: dict, side: str):
    """Score adjustment + why-text + flags from a prediction-market
    signal for a game-winner-correlated market (the spread).
    Returns (points, basis_upgrade, why_bits, flags)."""
    if not sig or sig.get("edge") is None:
        return 0.0, False, [], []
    edge = sig["edge"]
    move = sig.get("move")
    thin = sig.get("thin", False)
    picked_home = side == "over/home"
    aligned = (edge > 0) == picked_home

    points = 0.0
    upgrade = False
    why = []
    flags = []
    pm_txt = (f"PM {sig['pm_home_prob']:.0%} vs books "
              f"{sig['book_home_prob']:.0%} home ({edge:+.1%})")
    if aligned and abs(edge) >= 0.03:
        points = min(abs(edge) * 250, 25)
        if thin:
            points /= 2
        upgrade = True
        why.append("prediction mkts agree: " + pm_txt
                   + (" [thin]" if thin else ""))
    elif not aligned and abs(edge) >= 0.04:
        points = -10.0
        flags.append("market-against")
        why.append("prediction mkts DISAGREE: " + pm_txt)
    if move is not None and abs(move) >= 0.04 and (move > 0) == picked_home:
        points += 8
        flags.append("steam")
        why.append(f"steam {move:+.1%} today")
    return round(points, 1), upgrade, why, flags


def _market_only_play(game_key: str, game: dict, market_signals: dict,
                      news: list, team_keywords: dict, date: str):
    """A play from prediction-market/book divergence alone (edge >= 6%).
    Capped at B — price disagreement is real-money evidence but not
    sharp-social confirmation."""
    sig = market_signals.get(game_key)
    if not sig or sig.get("edge") is None or abs(sig["edge"]) < 0.06:
        return None
    side = "over/home" if sig["edge"] > 0 else "under/away"
    # Steeper scale than the evidence bonus: a 6%+ dislocation in an
    # efficient market is the entire thesis here, not a tiebreaker.
    # 6% -> C, ~10% -> B; thin books halved.
    score = min(abs(sig["edge"]) * 400, 40)
    if sig.get("thin"):
        score /= 2
    _, _, mkt_why, mkt_flags = _market_evidence(sig, side)
    if "steam" in mkt_flags:
        score += 8
    score = round(score + 5, 1)
    confidence = _confidence(score)
    if not confidence:
        return None
    return {
        "game": game_key,
        "market": "spread",
        "is_prop": False,
        "side": side,
        "line": (game.get("spread") or {}).get("line"),
        "pick": _pick_text("spread", side, game, team_keywords),
        "score": score,
        "confidence": "B" if confidence == "A" else confidence,
        "why": "; ".join(mkt_why) or "prediction-market edge vs books",
        "flags": mkt_flags + ["market-only"],
        "basis": "market",
        "news": list(news),
        "date": date,
    }


def _apply_caps(score: float, basis: str, flags: list):
    """Confidence from score with basis/news caps applied."""
    confidence = _confidence(score)
    if not confidence:
        return None
    if basis == "public":
        return "C"
    if basis in ("market", "model") and confidence == "A":
        confidence = "B"
    if "news" in flags and confidence == "A":
        confidence = "B"
    return confidence


MAX_MODEL_ONLY_PLAYS = 3


def _merge_model_recs(suggestions: list, model_recs: list, games: dict,
                      news_by_game: dict, team_keywords: dict, date: str):
    """Model recs either reinforce/contradict existing plays on the same
    market, or become model-only plays. Model-only plays are capped per
    slate (a baseline disagreeing with the market everywhere is telling
    on itself, not finding edges) and never created from pm-conflict
    recs — the full candidate list stays in data/<sport>/model/."""
    by_key = {(s["game"], s["market"]): s for s in suggestions}
    model_only_count = 0
    for rec in model_recs:
        key = (rec["game"], rec["market"])
        existing = by_key.get(key)
        if existing:
            if existing["side"] == rec["side"]:
                existing["score"] = round(existing["score"] + 12, 1)
                existing["why"] += f"; model agrees (EV {rec['ev']:+.1%})"
                existing["flags"].append("model")
            else:
                existing["score"] = round(existing["score"] - 8, 1)
                existing["why"] += (f"; model DISAGREES "
                                    f"(likes {rec['side']}, EV {rec['ev']:+.1%})")
                existing["flags"].append("model-against")
            new_conf = _apply_caps(existing["score"],
                                   existing.get("basis", "sharp"),
                                   existing["flags"])
            if new_conf:
                existing["confidence"] = new_conf
            continue

        # Model-only play: EV drives the score. +3% -> C, ~+5% -> B.
        if ("pm-conflict" in rec.get("flags", [])
                or model_only_count >= MAX_MODEL_ONLY_PLAYS):
            continue
        game = games.get(rec["game"], {})
        score = round(25 + min(rec["ev"] * 300, 20), 1)
        flags = ["model-only"] + rec.get("flags", [])
        confidence = _apply_caps(score, "model", flags)
        if not confidence:
            continue
        entry = {
            "game": rec["game"],
            "market": rec["market"],
            "is_prop": rec["market"] not in ("spread", "total", "moneyline"),
            "side": rec["side"],
            "line": rec["line"],
            "pick": _pick_text(rec["market"], rec["side"], game, team_keywords),
            "score": score,
            "confidence": confidence,
            "why": rec["why"],
            "flags": flags,
            "basis": "model",
            "news": news_by_game.get(rec["game"], [])[:2],
            "date": date,
        }
        suggestions.append(entry)
        by_key[key] = entry
        model_only_count += 1


def build_suggestions(game_data: dict, sentiment_data: dict, alert_data: dict,
                      team_keywords: dict, flip_state: dict = None,
                      market_signals: dict = None,
                      model_recs: list = None) -> dict:
    """Returns {suggestions, flip_watch, flip_triggered, no_edge,
    news_watch, new_flip_state}."""
    flip_state = dict(flip_state or {})
    market_signals = market_signals or {}
    games = game_data.get("games", {})
    buzz_idx = _buzz_index(alert_data.get("alerts", []))

    # News context per game (post-dedup INJURY_NEWS alerts)
    news_by_game = {}
    for a in alert_data.get("alerts", []):
        if a.get("type") == "INJURY_NEWS":
            raw_text = a.get("message", "").split("— ", 1)[-1]
            snippet = " ".join(w for w in raw_text.split()
                               if not w.startswith("http"))[:80]
            news_by_game.setdefault(a.get("game"), []).append(snippet)

    suggestions = []
    flip_triggered = []
    no_edge = []
    date = sentiment_data.get("date", "")

    for game_key, g_sent in sentiment_data.get("games", {}).items():
        game = games.get(game_key, {})
        game_had_play = False
        game_buzz = _game_buzz(buzz_idx, game_key)
        news = news_by_game.get(game_key, [])

        markets = {}
        for market in ("spread", "total"):
            if g_sent.get(market):
                markets[market] = g_sent[market]
        # game_general sentiment reinforces the spread read if the spread
        # bucket itself is thin
        general = g_sent.get("game_general")
        if general and "spread" not in markets:
            markets["spread"] = general
        for prop_label, prop_data in g_sent.get("props", {}).items():
            if not prop_label.endswith(" general"):
                markets[prop_label] = prop_data

        for market, mdata in markets.items():
            if not mdata or mdata.get("tweet_count", 0) == 0:
                continue
            is_prop = market not in ("spread", "total")
            buzz_accounts = buzz_idx.get((game_key, market), 0)
            if not is_prop:
                buzz_accounts = max(buzz_accounts, game_buzz)

            side = _pick_side(mdata)
            raw = mdata.get("_raw", {})
            public_sent = raw.get("public_sentiment")
            sharp_count = mdata.get("sharp_mentions", 0)

            # Public lock -> flip watch, not a play
            if (public_sent is not None and abs(public_sent) >= PUBLIC_LOCK_THRESHOLD
                    and sharp_count == 0):
                key = f"{game_key}|{market}"
                public_side = "over/home" if public_sent > 0 else "under/away"
                flip_state[key] = {"date": date, "public_side": public_side,
                                   "public_sentiment": round(public_sent, 3)}
                continue

            if side is None:
                continue

            # Flip trigger: previously public-locked market, sharps now
            # visibly on the other side
            key = f"{game_key}|{market}"
            watched = flip_state.get(key)
            flipped = bool(watched and sharp_count > 0
                           and side != watched["public_side"])

            # Sharp side-convergence: outranks buzz (which only counts
            # mentions), so buzz is muted when consensus fires
            aligned, opposed = _consensus(mdata, side)
            cons_pts, cons_why, cons_flags = _consensus_evidence(aligned, opposed)
            score, basis = _score_market(
                mdata, 0 if "consensus" in cons_flags else buzz_accounts)
            score = round(score + cons_pts, 1)
            if flipped:
                score += 15  # the fade setup completing is the signal

            # Prediction-market evidence applies to the winner-correlated
            # market (spread), not totals or props
            mkt_why, mkt_flags = [], []
            if market == "spread":
                pts, upgrade, mkt_why, mkt_flags = _market_evidence(
                    market_signals.get(game_key), side)
                score = round(score + pts, 1)
                if upgrade and basis == "public":
                    basis = "market"

            confidence = _confidence(score)
            if basis == "public" and confidence:
                confidence = "C"
            elif basis == "market" and confidence == "A":
                confidence = "B"  # A needs sharp evidence, not price alone
            if not confidence:
                continue

            flags = cons_flags + list(mkt_flags)
            if basis == "public":
                flags.append("public-only")
            if news:
                flags.append("news")
                if confidence == "A":
                    confidence = "B"
            if flipped:
                flags.append("flip")
                flip_state.pop(key, None)

            line = None
            if market == "total":
                line = game.get("total")
            elif market == "spread":
                line = (game.get("spread") or {}).get("line")
            else:
                line = _prop_line(market, game) or None

            entry = {
                "game": game_key,
                "market": market,
                "is_prop": is_prop,
                "side": side,
                "line": line,
                "pick": _pick_text(market, side, game, team_keywords),
                "score": score,
                "confidence": confidence,
                "why": "; ".join(cons_why + mkt_why
                                 + [_why(mdata, buzz_accounts, basis)]),
                "flags": flags,
                "basis": basis,
                "consensus_accounts": aligned if "consensus" in cons_flags else [],
                "news": news[:2],
                "date": date,
            }
            suggestions.append(entry)
            if flipped:
                flip_triggered.append(entry)
            game_had_play = True

        # Market-only play: big prediction-market/book divergence with no
        # social suggestion on the game
        if not game_had_play:
            entry = _market_only_play(game_key, game, market_signals,
                                      news, team_keywords, date)
            if entry:
                suggestions.append(entry)
                game_had_play = True

        if not game_had_play:
            no_edge.append(game_key)

    # Slate games with zero social posts never enter the loop above —
    # they can still carry a prediction-market edge, and belong in
    # no-edge otherwise
    for game_key, game in games.items():
        if game_key in sentiment_data.get("games", {}):
            continue
        entry = _market_only_play(game_key, game, market_signals,
                                  news_by_game.get(game_key, [])[:2],
                                  team_keywords, date)
        if entry:
            suggestions.append(entry)
        else:
            no_edge.append(game_key)

    # Model +EV recommendations: reinforce, contradict, or add plays
    if model_recs:
        _merge_model_recs(suggestions, model_recs, games, news_by_game,
                          team_keywords, date)
        with_plays = {s["game"] for s in suggestions}
        no_edge = [g for g in no_edge if g not in with_plays]

    # Expire flip entries from earlier dates (their games are over)
    flip_state = {k: v for k, v in flip_state.items() if v.get("date") == date}

    suggestions.sort(key=lambda s: s["score"], reverse=True)

    news_watch = []
    for game_key, items in news_by_game.items():
        for snippet in items:
            news_watch.append({"game": game_key, "text": snippet})

    log.info(f"Suggestions: {len(suggestions)} plays "
             f"({sum(1 for s in suggestions if s['confidence'] == 'A')} A / "
             f"{sum(1 for s in suggestions if s['confidence'] == 'B')} B / "
             f"{sum(1 for s in suggestions if s['confidence'] == 'C')} C), "
             f"{len(no_edge)} no-edge games, "
             f"{len(flip_state)} on flip watch, {len(flip_triggered)} flips")

    return {
        "date": date,
        "generated_at": datetime.now().isoformat(),
        "suggestions": suggestions,
        "no_edge": no_edge,
        "news_watch": news_watch,
        "flip_watch": sorted(flip_state.keys()),
        "flip_triggered": [f"{s['game']} {s['pick']}" for s in flip_triggered],
        "new_flip_state": flip_state,
    }
