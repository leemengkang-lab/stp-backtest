"""
Outbound-only Telegram alerts for the ARS bot.

Reuses the existing bot token + chat id(s) from the environment
(TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_CHAT_IDS). No inbound polling, so there is
no getUpdates conflict with anything else and no network-retry noise.

Fail-safe by construction: a send failure logs a warning and returns; it can
never raise into the trading loop. Control is still via the HALT/FLATTEN files.
"""
import os

import requests


class Telegram:
    def __init__(self, log):
        self.log = log
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chats = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
        self.chats = [c.strip() for c in chats.split(",") if c.strip()]
        self.enabled = bool(self.token and self.chats)
        if not self.enabled:
            log.warning("Telegram alerts DISABLED (no TELEGRAM_BOT_TOKEN / "
                        "TELEGRAM_ALLOWED_CHAT_IDS in env)")

    def send(self, text):
        if not self.enabled:
            return
        for chat in self.chats:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": chat, "text": text,
                          "disable_web_page_preview": True},
                    timeout=10,
                )
            except Exception as ex:            # never let a notification break the bot
                self.log.warning(f"telegram send failed: {ex}")
