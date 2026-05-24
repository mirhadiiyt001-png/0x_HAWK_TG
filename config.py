"""
Zone SMS Monitor Bot — Configuration
"""

import os
from dotenv import load_dotenv

load_dotenv(override=True)

# Telegram
BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
OWNER_CHAT_ID: int = int(os.getenv("OWNER_CHAT_ID", "0"))

# Upstream 0xhawk API
UPSTREAM_API_URL: str = os.getenv("UPSTREAM_API_URL", "https://0xhawk-api.up.railway.app")

# Forwarding toggle
FORWARD_SMS: bool = os.getenv("FORWARD_SMS", "true").lower() == "true"

# Polling intervals (seconds)
SMS_POLL_INTERVAL: int = 5
NUMS_POLL_INTERVAL: int = 15

# Dedup
DEDUP_BODY_WINDOW: int = 90  # seconds

# Emoji packs to load from Telegram
EMOJI_PACKS: list[str] = [
    "Taj_Mehyar",
    "GiftsGiftsGifts",
    "Icon_2023",
    "GameEmoji",
    "TONEmoji",
    "NewsEmoji",
    "RestrictedEmoji",
    "Ntgbbvddf_by_fStikBot",
]

# Max callback data bytes
MAX_CALLBACK_DATA: int = 60

# Line separator for messages
LINE_SEPARATOR: str = "——————————————————————"
