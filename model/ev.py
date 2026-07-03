"""
model/ev.py
Expected value of a bet given a model probability and American odds.
"""


def profit_per_unit(american):
    """Winning profit on a 1-unit stake."""
    a = float(american)
    return a / 100.0 if a > 0 else 100.0 / -a


def implied_prob(american):
    a = float(american)
    if a < 0:
        return -a / (-a + 100.0)
    return 100.0 / (a + 100.0)


def expected_value(p_win: float, american) -> float:
    """EV per unit staked. Positive = +EV."""
    win = profit_per_unit(american)
    return round(p_win * win - (1.0 - p_win), 4)


def kelly_fraction(p_win: float, american, multiplier: float = 0.25) -> float:
    """Fractional Kelly stake (quarter-Kelly default). 0 if -EV."""
    b = profit_per_unit(american)
    f = (p_win * (b + 1) - 1) / b
    return round(max(f, 0.0) * multiplier, 4)
