"""
core/sharp_filter.py
Classifies posts as sharp, public, news, or analytics based on account metadata.
Ported from nba-sentiment analysis/sharp_filter.py; the tracked-account
registry is loaded per sport via load_accounts() instead of a hardcoded path.

Account handles are matched case-insensitively and may be Twitter handles,
Reddit usernames, or Bluesky handles — the social adapters all normalize
the author into the post's "user" field.
"""

from typing import Optional

from .sentiment import SentimentScorer

_ACCOUNTS: dict = {}
_scorer = SentimentScorer()


def load_accounts(accounts_data: dict):
    """Load tracked accounts from a sport pack's accounts.json dict.
    Expects tier arrays: {tracked: [...], sharp: [...], analytics: [...], news: [...]}
    with entries like {handle, weight, tags}."""
    global _ACCOUNTS
    _ACCOUNTS = {}
    for tier_key in ("tracked", "sharp", "analytics", "news"):
        for entry in accounts_data.get(tier_key, []):
            handle = entry.get("handle", "").lower().lstrip("@").lstrip("u/")
            if handle:
                _ACCOUNTS[handle] = {
                    "tier": entry.get("tier", tier_key),
                    "weight": entry.get("weight", 1.0),
                    "tags": entry.get("tags", []),
                }


def enrich_post(post: dict) -> dict:
    """Add _sharp metadata to a normalized post dict."""
    handle = post.get("user", "").lower().lstrip("@")
    followers = post.get("user_followers", 0)
    verified = post.get("user_verified", False)

    if handle in _ACCOUNTS:
        acct = _ACCOUNTS[handle]
        tier = acct["tier"]
        weight = acct["weight"]
    elif followers >= 50000 and verified:
        tier = "analytics"
        weight = 0.7
    elif followers >= 10000:
        tier = "notable"
        weight = 0.4
    else:
        tier = "public"
        weight = 0.1

    if _scorer.is_news(post.get("text", "")):
        tier = "news"
        weight = max(weight, 0.8)

    post["_sharp"] = {
        "account_tier": tier,
        "account_weight": weight,
        "is_tracked": handle in _ACCOUNTS,
    }
    return post


# Back-compat alias (aggregator ported from the tweet-era code)
enrich_tweet = enrich_post


def detect_divergence(posts: list) -> dict:
    """Calculate sharp vs public sentiment divergence."""
    sharp_scores = []
    public_scores = []

    for t in posts:
        s = t.get("_sharp", {})
        score = t.get("sentiment_score", 0)
        tier = s.get("account_tier", "public")
        if tier in ("tracked", "sharp", "analytics"):
            sharp_scores.append(score)
        else:
            public_scores.append(score)

    sharp_avg = sum(sharp_scores) / len(sharp_scores) if sharp_scores else None
    public_avg = sum(public_scores) / len(public_scores) if public_scores else None

    if sharp_avg is not None and public_avg is not None:
        div = abs(sharp_avg - public_avg)
    else:
        div = 0.0

    signal = "insufficient_data"
    if sharp_avg is not None and public_avg is not None and div >= 0.15:
        signal = "follow_sharp" if sharp_avg > public_avg else "fade_public"
    elif sharp_avg is not None and div < 0.15:
        signal = "aligned"

    return {
        "sharp_sentiment": round(sharp_avg, 3) if sharp_avg is not None else None,
        "public_sentiment": round(public_avg, 3) if public_avg is not None else None,
        "sharp_tweet_count": len(sharp_scores),
        "public_tweet_count": len(public_scores),
        "divergence": round(div, 3),
        "signal": signal,
    }


def filter_posts(posts: list) -> dict:
    """Bucket posts by tier."""
    buckets = {"tracked": [], "sharp": [], "analytics": [], "notable": [], "public": [], "news": []}
    for t in posts:
        tier = t.get("_sharp", {}).get("account_tier", "public")
        buckets.setdefault(tier, []).append(t)
    return buckets


filter_tweets = filter_posts
