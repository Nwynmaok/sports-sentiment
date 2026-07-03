"""
model/player_model.py
P(over line) for player props — empirical, not parametric.

The game log IS the distribution: P(over) = the fraction of qualifying
games where the player cleared the line. This handles burstiness (RBIs
arrive in clumps; a Poisson at the same mean overstates P(>=1)) and
skew automatically.

The estimate is shrunk toward the market's no-vig probability: with a
thin sample the model defers to the price, and only a large sample that
disagrees moves the needle. Playing-time mixing (pinch-hit games
deflating per-game averages) is handled upstream by sample filters on
the log (e.g. plateAppearances >= 2, gamesStarted >= 1).
"""


def prob_over(values: list, line: float, p_market: float = None,
              min_games: int = 8, shrink_k: int = 20):
    """P(stat > line) or None if the sample is too small.

    values: per-game stat values (already filtered to real appearances)
    p_market: no-vig market probability of the over, used as the
        shrinkage anchor when available."""
    n = len(values)
    if n < min_games:
        return None
    over = sum(1 for v in values if v > line)
    pushes = sum(1 for v in values if v == line)
    decided = n - pushes
    if decided < min_games:
        return None
    emp = over / decided
    w = decided / (decided + shrink_k)
    anchor = p_market if p_market is not None else emp
    return round(w * emp + (1 - w) * anchor, 4)


def no_vig_prob(implied_over: float, implied_under: float):
    """Normalize two-way implied probabilities."""
    total = implied_over + implied_under
    if total <= 0:
        return None
    return implied_over / total
