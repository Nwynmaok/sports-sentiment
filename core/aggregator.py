"""
core/aggregator.py
Combines normalized social posts + sentiment scores into per-prop signals.
Ported from nba-sentiment analysis/aggregator.py; operates on in-memory
data shaped by core.matcher and is sport-agnostic.
"""

import logging
from datetime import datetime
from typing import Optional

from .sentiment import score_tweet, SentimentScorer
from .sharp_filter import enrich_post, detect_divergence, filter_posts

log = logging.getLogger("pipeline.aggregator")


def aggregate_prop(posts: list, scorer: SentimentScorer) -> dict:
    """Given a list of normalized posts for one prop, returns a signal dict."""
    if not posts:
        return {
            "tweet_count": 0,
            "sentiment": None,
            "signal": "insufficient_data",
            "top_tweets": [],
        }

    for post in posts:
        post["sentiment_score"] = score_tweet(post.get("text", ""), scorer)

    enriched = [enrich_post(t) for t in posts]

    total_weight = sum(max(t["_sharp"]["account_weight"], 0.05) for t in enriched)
    weighted_sentiment = sum(
        t["sentiment_score"] * max(t["_sharp"]["account_weight"], 0.05)
        for t in enriched
    ) / total_weight

    divergence_data = detect_divergence(enriched)

    # Per-account sentiment for sharp-tier accounts, so the suggestion
    # engine can detect side convergence across accounts
    sharp_sides = {}
    for t in enriched:
        tier = t["_sharp"]["account_tier"]
        if tier not in ("tracked", "sharp", "analytics"):
            continue
        handle = t.get("user", "").lower()
        if handle:
            sharp_sides.setdefault(handle, []).append(t["sentiment_score"])
    sharp_sides = {h: round(sum(v) / len(v), 3) for h, v in sharp_sides.items()}

    buckets = filter_posts(enriched)
    tracked = buckets.get("tracked", [])
    sharp = buckets.get("sharp", []) + buckets.get("analytics", [])
    top_sharp = sorted(tracked + sharp, key=lambda t: (
        1 if t["_sharp"]["account_tier"] == "tracked" else 0,
        t.get("likes", 0)
    ), reverse=True)[:5]
    top_all = sorted(enriched, key=lambda t: t.get("likes", 0), reverse=True)[:5]
    news_posts = buckets.get("news", [])

    return {
        "tweet_count": len(posts),
        "sentiment": round(weighted_sentiment, 3),
        "sharp_mentions": divergence_data["sharp_tweet_count"],
        "public_lean": _lean_label(divergence_data.get("public_sentiment")),
        "sharp_lean": _lean_label(divergence_data.get("sharp_sentiment")),
        "divergence": divergence_data["divergence"],
        "signal": divergence_data["signal"],
        "top_tweets": [_format_post(t) for t in top_all],
        "top_sharp_tweets": [_format_post(t) for t in top_sharp],
        "news_alerts": [_format_post(t) for t in news_posts],
        "sharp_sides": sharp_sides,
        "_raw": divergence_data,
    }


def _lean_label(score: Optional[float]) -> Optional[str]:
    if score is None:
        return None
    if score >= 0.6:
        return "over/home"
    elif score <= -0.2:
        return "under/away"
    else:
        return "neutral"


def _format_post(post: dict) -> dict:
    return {
        "user": post.get("user", ""),
        "text": post.get("text", "")[:280],
        "likes": post.get("likes", 0),
        "published": post.get("published", ""),
        "source": post.get("source", ""),
        "url": post.get("url", ""),
        "tier": post.get("_sharp", {}).get("account_tier", "unknown"),
        "sentiment_score": round(post.get("sentiment_score", 0), 3),
    }


def run_aggregation(raw_data: dict, date: str) -> dict:
    """Aggregate game-keyed posts (core.matcher.shape_posts output) into signals."""
    scorer = SentimentScorer()

    output: dict = {
        "date": date,
        "generated_at": datetime.now().isoformat(),
        "games": {},
        "summary": {
            "total_tweets": 0,
            "high_divergence_props": [],
            "news_alerts": [],
        },
    }

    for game_key, game_data in raw_data.items():
        if game_key.startswith("_"):
            continue
        if not isinstance(game_data, dict):
            log.warning(f"Skipping non-dict game data for key: {game_key}")
            continue

        game_result: dict = {"props": {}, "spread": None, "total": None}

        for prop_label, posts in game_data.get("props", {}).items():
            if not isinstance(posts, list):
                continue
            agg = aggregate_prop(posts, scorer)
            game_result["props"][prop_label] = agg
            output["summary"]["total_tweets"] += agg["tweet_count"]

            if agg.get("divergence") and agg["divergence"] >= 0.35:
                output["summary"]["high_divergence_props"].append({
                    "game": game_key, "prop": prop_label,
                    "signal": agg["signal"], "divergence": agg["divergence"],
                    "sharp_lean": agg["sharp_lean"], "public_lean": agg["public_lean"],
                })

            for alert in agg.get("news_alerts", []):
                output["summary"]["news_alerts"].append(
                    {"game": game_key, "prop": prop_label, **alert})

        general_posts = game_data.get("_game_general", [])
        if general_posts and isinstance(general_posts, list):
            agg = aggregate_prop(general_posts, scorer)
            game_result["game_general"] = agg
            output["summary"]["total_tweets"] += agg["tweet_count"]
            if agg.get("divergence") and agg["divergence"] >= 0.35:
                output["summary"]["high_divergence_props"].append({
                    "game": game_key, "prop": "_game_general",
                    "signal": agg["signal"], "divergence": agg["divergence"],
                    "sharp_lean": agg["sharp_lean"], "public_lean": agg["public_lean"],
                })
            for alert in agg.get("news_alerts", []):
                output["summary"]["news_alerts"].append(
                    {"game": game_key, "prop": "_game_general", **alert})

        for market in ("spread", "total"):
            posts = game_data.get(market, [])
            if posts and isinstance(posts, list):
                game_result[market] = aggregate_prop(posts, scorer)
                output["summary"]["total_tweets"] += game_result[market]["tweet_count"]

        output["games"][game_key] = game_result

    log.info(f"Aggregation complete: {output['summary']['total_tweets']} posts, "
             f"{len(output['games'])} games, "
             f"{len(output['summary']['high_divergence_props'])} high-divergence, "
             f"{len(output['summary']['news_alerts'])} news alerts")
    return output
