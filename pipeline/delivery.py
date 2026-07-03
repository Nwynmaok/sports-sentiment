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
    if not enabled():
        log.info("Telegram delivery skipped (no TELEGRAM_BOT_TOKEN/CHAT_ID)")
        return False
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    text = _digest_text(alert_data)

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
