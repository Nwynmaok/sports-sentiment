"""
One-time interactive Telegram login. Prompts for your phone number and
the code Telegram sends you, then saves telegram.session in the repo
root so the pipeline can read public channels non-interactively.

Usage:
    python3 -m scripts.telegram_login
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from adapters.social.telegram_channels import creds, SESSION_PATH, TELETHON_AVAILABLE  # noqa: E402


def main():
    if not TELETHON_AVAILABLE:
        sys.exit("telethon not installed — run: pip3 install telethon")
    api_id, api_hash = creds()
    if not (api_id and api_hash):
        sys.exit("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first "
                 "(create an app at https://my.telegram.org)")

    from telethon.sync import TelegramClient
    with TelegramClient(str(SESSION_PATH.with_suffix("")), int(api_id), api_hash) as client:
        me = client.get_me()
        print(f"✅ Logged in as {me.first_name} (@{me.username or 'no username'})")
        print(f"   Session saved -> {SESSION_PATH}")
        print("   The pipeline can now read public channels listed in "
              "sports/<key>/config.json under \"telegram_channels\".")


if __name__ == "__main__":
    main()
