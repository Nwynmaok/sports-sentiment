"""
core/sentiment.py
Post sentiment scoring for sports-betting context (sport-agnostic).
Ported from nba-sentiment analysis/sentiment.py.
"""

import re
from typing import Optional


# ---------------------------------------------------------------------------
# Keyword + pattern scorer
# ---------------------------------------------------------------------------

BULLISH_OVER = [
    "lock", "hammer", "smash", "love", "best bet", "max bet", "strong play",
    "fire", "confident", "slam", "cash it", "money", "easy money",
    "gonna hit", "will hit", "riding", "all over", "pounding",
    "taking the over", "over is", "going over", "like the over",
    "o/u over", "lean over", "over hits", "smash the over",
]

BEARISH_UNDER = [
    "fade", "avoid", "stay away", "trap", "overvalued", "no value", "pass",
    "sketchy", "sit this out", "overrated", "inflated", "too high",
    "taking the under", "under is", "going under", "like the under",
    "lean under", "under hits", "smash the under",
]

# Explicit over/under pick patterns
OVER_PATTERNS = [
    r'\bo\s*\d+\.?\d*\b',          # o25.5, o 25.5
    r'\bover\s+\d+\.?\d*\b',       # over 25.5
    r'\b\w+\s+over\b',             # "points over", "rebounds over"
]

UNDER_PATTERNS = [
    r'\bu\s*\d+\.?\d*\b',          # u25.5, u 25.5
    r'\bunder\s+\d+\.?\d*\b',      # under 25.5
    r'\b\w+\s+under\b',            # "points under"
]

# Result indicators (sharp accounts posting results)
WIN_MARKERS = ["✅", "💰", "winner", "cashed", "hit", "W ", " W"]
LOSS_MARKERS = ["❌", "loss", "missed", "L ", " L"]

INJURY_KEYWORDS = [
    "out tonight", "ruled out", "questionable", "doubtful", "dnp",
    "injury", "injured", "sidelined", "rest", "load management",
    "gtd", "game-time decision", "will not play", "won't play",
    "sitting out", "lineup change", "scratched", "inactive",
]


class SentimentScorer:
    """Multi-signal sentiment scorer for sports-betting posts."""

    def __init__(self, method: str = "keyword"):
        self.method = method

    def score(self, text: str) -> float:
        return _score_tweet(text)

    def is_news(self, text: str) -> bool:
        text_lower = text.lower()
        return any(kw in text_lower for kw in INJURY_KEYWORDS)


def score_tweet(text: str, scorer: Optional[SentimentScorer] = None) -> float:
    """Score a tweet's sentiment. Returns float in [-1.0, 1.0].
    Positive = bullish/over lean. Negative = bearish/under lean."""
    if scorer:
        return scorer.score(text)
    return _score_tweet(text)


def _score_tweet(text: str) -> float:
    """Combined keyword + pattern scorer."""
    text_lower = text.lower()
    score = 0.0
    signals = 0

    # Keyword matching
    bull = sum(1 for kw in BULLISH_OVER if kw in text_lower)
    bear = sum(1 for kw in BEARISH_UNDER if kw in text_lower)

    if bull + bear > 0:
        score += (bull - bear) / (bull + bear)
        signals += 1

    # Explicit over/under patterns (stronger signal)
    over_count = sum(len(re.findall(p, text_lower)) for p in OVER_PATTERNS)
    under_count = sum(len(re.findall(p, text_lower)) for p in UNDER_PATTERNS)

    if over_count + under_count > 0:
        pattern_score = (over_count - under_count) / (over_count + under_count)
        score += pattern_score * 1.5  # weight patterns higher
        signals += 1

    # Result markers (sharp posting wins/losses = track record signal)
    wins = sum(1 for m in WIN_MARKERS if m in text or m.lower() in text_lower)
    losses = sum(1 for m in LOSS_MARKERS if m in text or m.lower() in text_lower)
    if wins + losses > 0:
        # Winning sharps get a slight credibility boost
        score += 0.2 * (wins - losses) / (wins + losses)
        signals += 1

    if signals == 0:
        return 0.0

    # Normalize to [-1, 1]
    result = score / signals
    return round(max(-1.0, min(1.0, result)), 3)
