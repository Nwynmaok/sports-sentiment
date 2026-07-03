"""
pipeline/delivery.py
Sends the alert digest to Telegram via the Bot API (TELEGRAM_BOT_TOKEN +
TELEGRAM_CHAT_ID, carried over from the original nba-sentiment setup).
These are delivery credentials — unrelated to TELEGRAM_API_ID/HASH used
for reading channels.
"""

import os
import logging

import requests

log = logging.getLogger("pipeline.delivery")

MAX_MESSAGE_CHARS = 3800  # Telegram hard limit is 4096; leave headroom
MAX_ALERTS_IN_DIGEST = 15
MAX_CARDS = 8

CONF_EMOJI = {"A": "🎯", "B": "🟢", "C": "🟡"}


def format_digest(sug_data: dict, sport: str, grading_text: str = None,
                  team_keywords: dict = None) -> str:
    """Consolidated digest: ranked play cards with props nested, then
    no-edge/news/flip-watch footer."""
    team_keywords = team_keywords or {}
    suggestions = sug_data.get("suggestions", [])
    no_edge = sug_data.get("no_edge", [])
    plays = len(suggestions)

    lines = [f"📣 {sport} — {sug_data.get('date', '')} · "
             f"{plays} play{'s' if plays != 1 else ''}, {len(no_edge)} games no edge"]
    if grading_text:
        lines += ["", grading_text]
    if sug_data.get("flip_triggered"):
        lines += ["", "🔁 FLIP TRIGGERED: " + "; ".join(sug_data["flip_triggered"])]
    lines.append("")

    if not suggestions:
        lines.append("No plays today — nothing cleared the edge threshold.")
    else:
        # Group by game, keep the order of each game's best suggestion
        by_game = {}
        for s in suggestions:
            by_game.setdefault(s["game"], []).append(s)
        games_ranked = sorted(by_game.items(),
                              key=lambda kv: kv[1][0]["score"], reverse=True)

        for i, (game_key, sugs) in enumerate(games_ranked[:MAX_CARDS]):
            best = sugs[0]
            header = "🏆 BEST BET" if i == 0 else None
            if header:
                lines.append(header)
            lines.append(f"{CONF_EMOJI.get(best['confidence'], '⚪')} {game_key}")
            for s in sugs:
                prefix = "  · prop: " if s["is_prop"] else "  "
                marks = ""
                if "consensus" in s.get("flags", []):
                    marks += " 🤝"
                if "flip" in s.get("flags", []):
                    marks += " 🔁"
                lines.append(f"{prefix}{s['pick']}  [{s['confidence']}]{marks}")
                lines.append(f"    {s['why']}")
            for n in best.get("news", []):
                lines.append(f"    ⚠ {n}")
            lines.append("")

        hidden = len(games_ranked) - min(len(games_ranked), MAX_CARDS)
        if hidden > 0:
            lines.append(f"(+{hidden} lower-scored game(s) in the suggestions file)")
            lines.append("")

    footer = []
    if no_edge:
        def nick(matchup):
            parts = matchup.split(" @ ")
            if len(parts) != 2:
                return matchup
            rev = {v: k for k, v in team_keywords.items()}
            return f"{rev.get(parts[0], parts[0].split()[-1])}@{rev.get(parts[1], parts[1].split()[-1])}"
        shown = [nick(g) for g in no_edge[:8]]
        more = f", +{len(no_edge) - 8} more" if len(no_edge) > 8 else ""
        footer.append("No edge: " + ", ".join(shown) + more)
    news_watch = sug_data.get("news_watch", [])
    if news_watch:
        footer.append("News watch: " + " | ".join(
            n["text"][:60] for n in news_watch[:4]))
    if sug_data.get("flip_watch"):
        footer.append(f"Flip watch: {len(sug_data['flip_watch'])} market(s) "
                      "public-heavy, waiting on sharp counter")
    if footer:
        lines.append("——")
        lines.extend(footer)
    return "\n".join(lines)


def enabled() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def _digest_text(alert_data: dict) -> str:
    summary = alert_data.get("summary", {})
    sport = alert_data.get("sport", "")
    lines = [
        f"📣 {sport} sentiment — {alert_data.get('date', '')}",
        f"{summary.get('total_alerts', 0)} alerts | "
        f"🔴 {summary.get('priority_1', 0)}  🟡 {summary.get('priority_2', 0)}  "
        f"⚪ {summary.get('priority_3', 0)}",
        "",
    ]
    alerts = alert_data.get("alerts", [])
    # Digest carries P1/P2 in full; P3 only if there's room in the cap
    ranked = [a for a in alerts if a["priority"] <= 2] + \
             [a for a in alerts if a["priority"] == 3]
    for alert in ranked[:MAX_ALERTS_IN_DIGEST]:
        lines.append(f"{alert['label']} [P{alert['priority']}]")
        lines.append(alert["message"])
        action = alert.get("action")
        if action:
            lines.append(f"→ {action}")
        lines.append("")
    shown = min(len(ranked), MAX_ALERTS_IN_DIGEST)
    if len(alerts) > shown:
        lines.append(f"(+{len(alerts) - shown} more in the alerts file)")
    return "\n".join(lines)


def _chunks(text: str):
    while text:
        if len(text) <= MAX_MESSAGE_CHARS:
            yield text
            return
        cut = text.rfind("\n\n", 0, MAX_MESSAGE_CHARS)
        if cut < MAX_MESSAGE_CHARS // 2:
            cut = MAX_MESSAGE_CHARS
        yield text[:cut]
        text = text[cut:].lstrip("\n")


def send_telegram(alert_data: dict) -> bool:
    """Legacy raw-alert digest sender (kept for --raw-digest style use)."""
    return send_text(_digest_text(alert_data))


def send_text(text: str) -> bool:
    if not enabled():
        log.info("Telegram delivery skipped (no TELEGRAM_BOT_TOKEN/CHAT_ID)")
        return False
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    ok = True
    for chunk in _chunks(text):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk,
                      "disable_web_page_preview": True},
                timeout=20,
            )
            if not r.ok:
                log.error(f"Telegram send failed ({r.status_code}): {r.text[:200]}")
                ok = False
        except requests.RequestException as e:
            log.error(f"Telegram send error: {e}")
            ok = False
    if ok:
        log.info("Telegram digest delivered")
    return ok
