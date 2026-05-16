"""Helper: print your Telegram chat_id so you can configure the bot.

Usage:
    1. Create a bot via @BotFather in Telegram, get your bot token.
    2. Search for your new bot in Telegram, hit Start, send any message.
    3. Run: TELEGRAM_BOT_TOKEN=123:abc python scripts/get_chat_id.py
"""
from __future__ import annotations

import os
import sys

import requests


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Set TELEGRAM_BOT_TOKEN first", file=sys.stderr)
        return 2

    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        print(f"Telegram API error: {data}", file=sys.stderr)
        return 1

    updates = data.get("result", [])
    if not updates:
        print(
            "No messages yet. Open Telegram, search your bot, hit Start, "
            "send any message, then re-run this script.",
            file=sys.stderr,
        )
        return 1

    chats: dict[str, str] = {}
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is None:
            continue
        name = chat.get("title") or chat.get("first_name") or chat.get("username") or ""
        chats[str(cid)] = name

    print("Found chat IDs:")
    for cid, name in chats.items():
        print(f"  TELEGRAM_CHAT_ID={cid}    ({name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
