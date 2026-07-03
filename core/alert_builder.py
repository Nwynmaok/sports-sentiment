"""
core/alert_builder.py
Reads aggregated sentiment data and generates actionable betting alerts.
Ported from nba-sentiment analysis/alert_builder.py (image-prop alerts
dropped along with the Apify image extractor). Sport-agnostic.
"""

import logging
from datetime import datetime
from collections import defaultdict

log = logging.getLogger("pipeline.alerts")

THRESHOLDS = {
    "divergence_high": 0.15,
    "divergence_medium": 0.08,
    "public_one_sided": 0.50,
    "sharp_conviction": 0.20,
    "min_tweet_count": 1,
    "min_sharp_mentions": 0,
    "buzz_min_accounts": 2,
}

ALERT_TYPES = {
    "FADE_PUBLIC":    "🔴 Fade Public",
    "FOLLOW_SHARP":   "🟢 Follow Sharp",
    "INJURY_NEWS":    "🚨 Injury/News Alert",
    "SHARP_LEAN":     "📊 Sharp Lean",
    "PUBLIC_LOCK":    "📢 Public Lock (fade candidate)",
    "BUZZ":           "🔥 Sharp Buzz",
}


def generate_alerts_for_prop(game: str, prop: str, data: dict) -> list:
    alerts = []
    tweet_count = data.get("tweet_count", 0)
    if tweet_count < THRESHOLDS["min_tweet_count"]:
        return []

    sentiment = data.get("sentiment")
    divergence = data.get("divergence")
    signal = data.get("signal", "insufficient_data")
    sharp_mentions = data.get("sharp_mentions", 0)
    public_lean = data.get("public_lean")
    sharp_lean = data.get("sharp_lean")
    news_alerts = data.get("news_alerts", [])
    top_sharp = data.get("top_sharp_tweets", [])

    base = {
        "game": game, "prop": prop, "tweet_count": tweet_count,
        "sentiment": sentiment, "divergence": divergence,
        "sharp_mentions": sharp_mentions, "public_lean": public_lean,
        "sharp_lean": sharp_lean, "top_sharp_tweets": top_sharp[:3],
        "timestamp": datetime.now().isoformat(),
    }

    for news in news_alerts:
        alerts.append({**base, "type": "INJURY_NEWS", "priority": 1,
            "label": ALERT_TYPES["INJURY_NEWS"],
            "message": f"News: {game} / {prop}: @{news.get('user')} — {news.get('text', '')[:120]}",
            "action": "Check injury/lineup status before betting"})

    if sharp_mentions >= 1 and sharp_lean and sharp_lean != "neutral":
        if divergence is not None and divergence >= THRESHOLDS["divergence_high"]:
            priority = 1
        elif divergence is not None and divergence >= THRESHOLDS["divergence_medium"]:
            priority = 2
        else:
            priority = 3

        if signal == "fade_public":
            alerts.append({**base, "type": "FADE_PUBLIC", "priority": priority,
                "label": ALERT_TYPES["FADE_PUBLIC"],
                "message": f"{game} | {prop}: Public {public_lean}, Sharp {sharp_lean}. Divergence={divergence:.2f}.",
                "action": f"Fade public ({public_lean}), lean {sharp_lean}"})
        elif signal == "follow_sharp":
            alerts.append({**base, "type": "FOLLOW_SHARP", "priority": priority,
                "label": ALERT_TYPES["FOLLOW_SHARP"],
                "message": f"{game} | {prop}: Sharp on {sharp_lean}. {sharp_mentions} sharp posts. Divergence={divergence:.2f}.",
                "action": f"Follow sharp side ({sharp_lean})"})
        elif signal == "aligned" or signal == "insufficient_data":
            sent_str = f"{sentiment:.2f}" if sentiment is not None else "N/A"
            alerts.append({**base, "type": "SHARP_LEAN", "priority": 3,
                "label": ALERT_TYPES["SHARP_LEAN"],
                "message": f"{game} | {prop}: Sharp lean {sharp_lean}. {sharp_mentions} sharp post(s). Sentiment={sent_str}.",
                "action": f"Lean {sharp_lean} (sharp-aligned)"})

    elif sentiment is not None and abs(sentiment) >= THRESHOLDS["sharp_conviction"] and tweet_count >= 1:
        lean = "over/home" if sentiment > 0 else "under/away"
        alerts.append({**base, "type": "SHARP_LEAN", "priority": 3,
            "label": ALERT_TYPES["SHARP_LEAN"],
            "message": f"{game} | {prop}: Sentiment={sentiment:.2f} ({lean}). {tweet_count} post(s).",
            "action": f"Lean {lean}"})

    raw = data.get("_raw", {})
    public_sentiment = raw.get("public_sentiment")
    if public_sentiment is not None and abs(public_sentiment) >= THRESHOLDS["public_one_sided"] and sharp_mentions == 0:
        lean = "over/home" if public_sentiment > 0 else "under/away"
        alerts.append({**base, "type": "PUBLIC_LOCK", "priority": 3,
            "label": ALERT_TYPES["PUBLIC_LOCK"],
            "message": f"{game} | {prop}: Public heavily on {lean} (score={public_sentiment:.2f}). No sharp counter.",
            "action": "Monitor for sharp counter — potential fade setup"})

    return alerts


def detect_buzz(sentiment_data: dict) -> list:
    """Find props/games mentioned by multiple unique sharp accounts."""
    buzz_alerts = []
    sharp_map = defaultdict(lambda: {"handles": set(), "tweets": [], "sentiment_sum": 0.0})

    for game_key, game_data in sentiment_data.get("games", {}).items():
        for prop_label, prop_data in game_data.get("props", {}).items():
            for post in prop_data.get("top_sharp_tweets", []):
                if post.get("tier", "") in ("tracked", "sharp", "analytics"):
                    key = (game_key, prop_label)
                    sharp_map[key]["handles"].add(post["user"].lower())
                    sharp_map[key]["tweets"].append(post)
                    sharp_map[key]["sentiment_sum"] += post.get("sentiment_score", 0)

        general = game_data.get("game_general", {}) or {}
        for post in general.get("top_sharp_tweets", []):
            if post.get("tier", "") in ("tracked", "sharp", "analytics"):
                key = (game_key, "game_general")
                sharp_map[key]["handles"].add(post["user"].lower())
                sharp_map[key]["tweets"].append(post)
                sharp_map[key]["sentiment_sum"] += post.get("sentiment_score", 0)

    game_handles = defaultdict(set)
    game_tweets = defaultdict(list)
    for (game_key, _), data in sharp_map.items():
        game_handles[game_key].update(data["handles"])
        game_tweets[game_key].extend(data["tweets"])

    min_accts = THRESHOLDS["buzz_min_accounts"]

    for (game_key, prop_label), data in sharp_map.items():
        if len(data["handles"]) >= min_accts:
            avg = data["sentiment_sum"] / len(data["tweets"]) if data["tweets"] else 0
            lean = "over/home" if avg > 0 else "under/away" if avg < 0 else "mixed"
            buzz_alerts.append({
                "type": "BUZZ", "priority": 2, "label": ALERT_TYPES["BUZZ"],
                "game": game_key, "prop": prop_label,
                "sharp_accounts": sorted(data["handles"]),
                "account_count": len(data["handles"]),
                "tweet_count": len(data["tweets"]),
                "avg_sentiment": round(avg, 3), "lean": lean,
                "message": f"{game_key} | {prop_label}: {len(data['handles'])} sharp accounts ({', '.join('@' + h for h in sorted(data['handles']))}). Lean: {lean}.",
                "action": f"Multiple sharps on {prop_label} — high-conviction {lean}",
                "top_sharp_tweets": data["tweets"][:3],
                "timestamp": datetime.now().isoformat(),
            })

    for game_key, handles in game_handles.items():
        if len(handles) >= min_accts + 1:
            tweets = game_tweets[game_key]
            avg = sum(t.get("sentiment_score", 0) for t in tweets) / len(tweets) if tweets else 0
            buzz_alerts.append({
                "type": "BUZZ", "priority": 2, "label": "🔥 Game Buzz",
                "game": game_key, "prop": "multiple props",
                "sharp_accounts": sorted(handles),
                "account_count": len(handles),
                "tweet_count": len(tweets),
                "avg_sentiment": round(avg, 3),
                "lean": "over/home" if avg > 0 else "under/away" if avg < 0 else "mixed",
                "message": f"{game_key}: {len(handles)} sharp accounts active ({', '.join('@' + h for h in sorted(handles))})",
                "action": "High sharp interest — check all props",
                "top_sharp_tweets": tweets[:3],
                "timestamp": datetime.now().isoformat(),
            })

    return buzz_alerts


def build_alerts(sentiment_data: dict, sport_label: str = "") -> dict:
    all_alerts = []
    for game_key, game_data in sentiment_data.get("games", {}).items():
        for prop_label, prop_data in game_data.get("props", {}).items():
            all_alerts.extend(generate_alerts_for_prop(game_key, prop_label, prop_data))
        if game_data.get("spread"):
            all_alerts.extend(generate_alerts_for_prop(game_key, "spread", game_data["spread"]))
        if game_data.get("total"):
            all_alerts.extend(generate_alerts_for_prop(game_key, "total", game_data["total"]))
        if game_data.get("game_general"):
            all_alerts.extend(generate_alerts_for_prop(game_key, "game_general", game_data["game_general"]))

    buzz = detect_buzz(sentiment_data)
    all_alerts.extend(buzz)

    all_alerts.sort(key=lambda a: (a["priority"], -(a.get("divergence") or 0)))

    summary = {
        "total_alerts": len(all_alerts),
        "priority_1": sum(1 for a in all_alerts if a["priority"] == 1),
        "priority_2": sum(1 for a in all_alerts if a["priority"] == 2),
        "priority_3": sum(1 for a in all_alerts if a["priority"] == 3),
        "types": {}, "buzz_count": len(buzz),
    }
    for a in all_alerts:
        summary["types"][a["type"]] = summary["types"].get(a["type"], 0) + 1

    return {"date": sentiment_data.get("date"), "sport": sport_label,
            "generated_at": datetime.now().isoformat(),
            "alerts": all_alerts, "summary": summary}


def format_alerts_console(alert_data: dict) -> str:
    lines = []
    summary = alert_data.get("summary", {})
    sport = alert_data.get("sport", "")
    lines.append(f"\n{'='*60}")
    lines.append(f"  {sport} SENTIMENT ALERTS — {alert_data.get('date', '')}")
    lines.append(f"{'='*60}")
    lines.append(f"  Total: {summary['total_alerts']}  |  🔴 P1: {summary['priority_1']}  🟡 P2: {summary['priority_2']}  ⚪ P3: {summary['priority_3']}")
    if summary.get("buzz_count"):
        lines.append(f"  🔥 Buzz alerts: {summary['buzz_count']}")
    lines.append("")
    for alert in alert_data.get("alerts", []):
        lines.append(f"  {alert['label']} [P{alert['priority']}]")
        lines.append(f"  {alert['message']}")
        lines.append(f"  → {alert.get('action', '')}")
        for t in alert.get("top_sharp_tweets", [])[:2]:
            lines.append(f"    @{t['user']}: \"{t['text'][:100]}\"")
        lines.append("")
    lines.append(f"{'='*60}\n")
    return "\n".join(lines)


def format_alerts_markdown(alert_data: dict) -> str:
    lines = []
    summary = alert_data.get("summary", {})
    sport = alert_data.get("sport", "")
    lines.append(f"## 📣 {sport} Sentiment Alerts — {alert_data.get('date', '')}\n")
    lines.append(f"**{summary['total_alerts']} alerts** | P1: {summary['priority_1']} | P2: {summary['priority_2']} | P3: {summary['priority_3']}")
    if summary.get("buzz_count"):
        lines.append(f"**🔥 {summary['buzz_count']} buzz alerts**\n")
    lines.append("")
    priority_labels = {1: "🔴 High Priority", 2: "🟡 Medium Priority", 3: "⚪ Low Priority"}
    current_priority = None
    for alert in alert_data.get("alerts", []):
        if alert["priority"] != current_priority:
            current_priority = alert["priority"]
            lines.append(f"\n### {priority_labels[current_priority]}\n")
        lines.append(f"**{alert['label']}** — {alert['game']} / `{alert['prop']}`")
        lines.append(f"> {alert['message']}")
        lines.append(f"**Action:** {alert.get('action', '')}")
        for t in alert.get("top_sharp_tweets", [])[:2]:
            lines.append(f"*@{t['user']}: \"{t['text'][:120]}\"*")
        lines.append("")
    return "\n".join(lines)
