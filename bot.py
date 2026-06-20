"""
Zone SMS Monitor Bot — Main Entry Point
Complete Python port of the Zone SMS Monitor with premium emoji,
styled buttons, OTP detection, dedup, and fail-safe delivery.
"""

import asyncio
import html
import re
import time
import logging
from datetime import datetime, timedelta
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Bot,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

from config import (
    BOT_TOKEN,
    OWNER_CHAT_ID,
    UPSTREAM_API_URL,
    FORWARD_SMS,
    SMS_POLL_INTERVAL,
    NUMS_POLL_INTERVAL,
    DEDUP_BODY_WINDOW,
    EMOJI_PACKS,
    MAX_CALLBACK_DATA,
    LINE_SEPARATOR,
)

# ──────────────────────────────────────────────────────────────────────────────
#  Logging
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("zone_bot")

# ──────────────────────────────────────────────────────────────────────────────
#  In-Memory Stores
# ──────────────────────────────────────────────────────────────────────────────
message_store: dict[str, dict] = {}
msg_store_counter: int = 0

approved_users: set[int] = set()
pending_users: dict[int, dict] = {}  # userId → user info dict

# ──────────────────────────────────────────────────────────────────────────────
#  Custom Emoji Store
# ──────────────────────────────────────────────────────────────────────────────
custom_emoji_map: dict[str, str] = {}  # unicode emoji → custom_emoji_id

VS16 = "\uFE0F"


async def load_custom_emoji_packs(bot: Bot) -> None:
    """Load custom emoji IDs from Telegram sticker packs."""
    global custom_emoji_map
    loaded = 0
    for pack_name in EMOJI_PACKS:
        try:
            sticker_set = await bot.get_sticker_set(pack_name)
            for sticker in sticker_set.stickers:
                if (
                    sticker.type == "custom_emoji"
                    and sticker.custom_emoji_id
                    and sticker.emoji
                ):
                    # First pack that has a given emoji wins
                    if sticker.emoji not in custom_emoji_map:
                        custom_emoji_map[sticker.emoji] = sticker.custom_emoji_id
                        loaded += 1
        except Exception:
            pass  # Ignore per-pack errors
    logger.info(f"Custom emoji packs loaded: {len(EMOJI_PACKS)} packs, {len(custom_emoji_map)} unique emojis")


def ce_id(emoji: str) -> Optional[str]:
    """Resolve the custom_emoji_id for a unicode emoji."""
    result = custom_emoji_map.get(emoji)
    if result:
        return result
    # Try without variation selector
    result = custom_emoji_map.get(emoji.replace(VS16, ""))
    if result:
        return result
    # Try adding variation selector
    if not emoji.endswith(VS16):
        result = custom_emoji_map.get(emoji + VS16)
    return result


def ce(emoji: str) -> str:
    """Wrap emoji in <tg-emoji> if a custom version is available."""
    eid = ce_id(emoji)
    if eid:
        return f'<tg-emoji emoji-id="{eid}">{emoji}</tg-emoji>'
    return emoji


# ──────────────────────────────────────────────────────────────────────────────
#  Message Store
# ──────────────────────────────────────────────────────────────────────────────
def store_message(sms: dict) -> str:
    global msg_store_counter
    msg_store_counter += 1
    store_id = str(msg_store_counter)
    message_store[store_id] = sms
    # Keep store manageable
    if len(message_store) > 1000:
        keys = list(message_store.keys())[:200]
        for k in keys:
            del message_store[k]
    return store_id


# ──────────────────────────────────────────────────────────────────────────────
#  SMS Parsing
# ──────────────────────────────────────────────────────────────────────────────
def try_fix_mojibake(text: str) -> str:
    """Detect and heal UTF-8 Cyrillic Mojibake read as ISO-8859-1 or cp1252."""
    if not text:
        return text
    # Cyrillic UTF-8 Mojibake usually has recognizable signatures like Ð or Ñ
    if any(c in text for c in ("Ð", "Ñ", "ð", "ñ", "×", "Ø", "æ", "ç")):
        try:
            decoded = text.encode("iso-8859-1").decode("utf-8")
            if any(0x0400 <= ord(c) <= 0x04FF for c in decoded):
                return decoded
        except Exception:
            pass
        try:
            decoded = text.encode("cp1252").decode("utf-8")
            if any(0x0400 <= ord(c) <= 0x04FF for c in decoded):
                return decoded
        except Exception:
            pass
    return text


def parse_sms_record(rec: dict) -> dict:
    """Parse an upstream API record into our internal SmsMessage format."""
    body = rec.get("message", "") or ""
    body = try_fix_mojibake(body)
    return {
        "timestamp": rec.get("date", ""),
        "sim": rec.get("termination", ""),
        "phone": rec.get("number", ""),
        "device": rec.get("cli", ""),
        "currency": (rec.get("currency", "") or "").replace("&euro;", "€").replace("&amp;", "&"),
        "plan": rec.get("payterm", "Weekly"),
        "status": 0,
        "body": body,
    }


# ──────────────────────────────────────────────────────────────────────────────
#  OTP Detection
# ──────────────────────────────────────────────────────────────────────────────
OTP_PATTERNS = [
    # Keyword before digit
    re.compile(
        r'(?:OTP|otp|code|رمز|کد|verification|verify|confirm(?:ation)?|auth|pin|passcode|пароль|код|senha|doğrulama|mã|Ð¿Ð°ÑÐºÐ¾Ð´|Ð¿Ð°Ñ\s+ÐºÐ¾Ð´|ÐÐ°ÑÐºÐ¾Ð´|ÐÐ°Ñ\s+ÐºÐ¾Ð´|Ð¿Ð°ÑÐ¾Ð»Ñ|Ð¿Ð°Ñ\s+Ð¾Ð»Ñ|ÐºÐ¾Ð´|Ð¿Ð°Ñ|ÐÐ°Ñ|паскод|пас\s+код)'
        r'[^0-9]{0,40}(\d{4,16})',
        re.IGNORECASE,
    ),
    # Digit before keyword
    re.compile(
        r'(\d{4,16})[^A-Za-z0-9\n]{0,40}(?:OTP|otp|code|کد|رمز|verification|verify|confirm(?:ation)?|пароль|код|паскод|пас\s+код|Ð¿Ð°ÑÐºÐ¾Ð´|Ð¿Ð°Ñ\s+ÐºÐ¾Ð´|ÐÐ°ÑÐºÐ¾Ð´|ÐÐ°Ñ\s+ÐºÐ¾Ð´|Ð¿Ð°ÑÐ¾Ð»Ñ|Ð¿Ð°Ñ\s+Ð¾Ð»Ñ|ÐºÐ¾Ð´|Ð¿Ð°Ñ|ÐÐ°Ñ)',
        re.IGNORECASE,
    ),
    # After is/:/=/-
    re.compile(r'(?:is|:|-|=)\s*(\d{4,16})\b'),
    # Standalone 6-12 digit number
    re.compile(r'(?<!\d)(\d{6,12})(?!\d)'),
    # Standalone 4-digit number
    re.compile(r'(?<!\d)(\d{4})(?!\d)'),
    # Start of message
    re.compile(r'^(\d{4,16})\b'),
]

OTP_KEYWORDS = [
    "otp", "one-time", "one time", "verification code", "verify", "confirm",
    "رمز", "کد", "تأیید", "code", "passcode", "pin",
    "authentication", "auth", "token", "secret",
    "пароль", "код", "подтвержд", "паскод", "пас код",
    "doğrulama", "şifre",
    "senha", "verificação",
    "mã xác", "ma xac",
    # Mojibake equivalents of Russian words (lowercase & raw)
    "ð¿ð°ñðºð¾ð´", "ð¿ð°ñ ðºð¾ð´", "ðð°ñðºð¾ð´", "ðð°ñ ðºð¾ð´", "ðºð¾ð´", "ð¿ð°ñ", "ðð°ñ",
    "ð¿ð°ñð¾ð»ñ", "ð¿ð°ñ ð¾ð»ñ", "ðð°ñð¾ð»ñ", "ðð°ñ ð¾ð»ñ", "ð²ñð¾ð´", "ð²ñ ð¾ð´",
    "Ð¿Ð°ÑÐºÐ¾Ð´", "Ð¿Ð°Ñ ÐºÐ¾Ð´", "ÐÐ°ÑÐºÐ¾Ð´", "ÐÐ°Ñ ÐºÐ¾Ð´", "ÐºÐ¾Ð´", "Ð¿Ð°Ñ", "ÐÐ°Ñ",
    "Ð¿Ð°ÑÐ¾Ð»Ñ", "Ð¿Ð°Ñ Ð¾Ð»Ñ", "ÐÐ°ÑÐ¾Ð»Ñ", "ÐÐ°Ñ Ð¾Ð»Ñ", "Ð²ÑÐ¾Ð´", "Ð²Ñ Ð¾Ð´",
]


def extract_otp(text: str) -> Optional[str]:
    """Extract OTP code from SMS text."""
    for pattern in OTP_PATTERNS:
        match = pattern.search(text)
        if match and match.group(1):
            return match.group(1)
    return None


def is_otp_message(text: str) -> bool:
    """Check if message contains OTP-related content."""
    lower = text.lower()
    if any(kw in lower for kw in OTP_KEYWORDS):
        return True
    # Fallback: standalone 6-digit number = very likely OTP
    if re.search(r'(?<!\d)\d{6}(?!\d)', text):
        return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────────────
def escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return html.escape(text)


def truncate_bytes(s: str, max_bytes: int) -> str:
    """Truncate string to fit within max_bytes when UTF-8 encoded."""
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def safe_callback_data(prefix: str, value: str) -> str:
    """Create callback data that fits within Telegram's limit."""
    return prefix + truncate_bytes(value, MAX_CALLBACK_DATA - len(prefix.encode("utf-8")))


def format_time(ts: str) -> str:
    """Format timestamp for display."""
    return ts.replace("T", " ")[:19]


def make_message_key(sms: dict) -> str:
    """Create a dedup key for a message."""
    return f"{sms['timestamp']}|{sms['phone']}|{sms['body'][:40]}"


def is_valid_sms(sms: dict) -> bool:
    """Validate an SMS record."""
    phone = sms.get("phone", "")
    body = sms.get("body", "")
    timestamp = sms.get("timestamp", "")

    if not phone or not re.match(r'^\+?\d{5,}$', phone.strip()):
        return False
    if not body or body.strip() in ("", "0"):
        return False
    if not timestamp or not re.match(r'^\d{4}-\d{2}-\d{2}', timestamp):
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
#  Secondary Dedup: phone + body, expires after 90 seconds
# ──────────────────────────────────────────────────────────────────────────────
recent_body_map: dict[str, float] = {}  # key → sent_at timestamp


def is_body_duplicate(sms: dict) -> bool:
    """Check if same body from same phone was sent recently."""
    key = f"{sms['phone']}|{sms['body'].strip()}"
    now = time.time()
    last = recent_body_map.get(key)

    if last and now - last < DEDUP_BODY_WINDOW:
        return True

    recent_body_map[key] = now

    # Cleanup old entries
    if len(recent_body_map) > 500:
        expired = [k for k, t in recent_body_map.items() if now - t > DEDUP_BODY_WINDOW]
        for k in expired:
            del recent_body_map[k]

    return False


# ──────────────────────────────────────────────────────────────────────────────
#  Message Formatters
# ──────────────────────────────────────────────────────────────────────────────
def format_otp_message(sms: dict) -> str:
    otp = extract_otp(sms["body"])
    return (
        f'{ce("🚨")} <b>OTP INTERCEPTED</b> {ce("🚨")}\n'
        f'{LINE_SEPARATOR}\n\n'
        f'╭─ {ce("✨")} <b>DETAILS</b>\n'
        f'├ {ce("📲")} <b>Phone:</b>   <code>{escape_html(sms["phone"])}</code>\n'
        f'├ {ce("🔔")} <b>Time:</b>    {escape_html(format_time(sms["timestamp"]))}\n'
        f'├ {ce("🃏")} <b>SIM:</b>     {escape_html(sms["sim"])}\n'
        f'├ {ce("💻")} <b>Device:</b>  {escape_html(sms["device"])}\n'
        f'╰ {ce("💵")} <b>Plan:</b>    {escape_html(sms["plan"])}\n\n'
        f'╭─ {ce("💬")} <b>MESSAGE</b>\n'
        f'╰ <i>{escape_html(sms["body"])}</i>\n\n'
        f'╭─ {ce("🔓")} <b>OTP CODE</b>\n'
        f'╰ <code>{escape_html(otp or "N/A")}</code>\n\n'
        f'{ce("⬆️")} <i>Tap the code to copy</i>'
    )


def format_sms_message(sms: dict) -> str:
    return (
        f'{ce("💌")} <b>NEW SMS</b> {ce("💌")}\n'
        f'{LINE_SEPARATOR}\n\n'
        f'╭─ {ce("✨")} <b>DETAILS</b>\n'
        f'├ {ce("📲")} <b>Phone:</b>   <code>{escape_html(sms["phone"])}</code>\n'
        f'├ {ce("🔔")} <b>Time:</b>    {escape_html(format_time(sms["timestamp"]))}\n'
        f'├ {ce("🃏")} <b>SIM:</b>     {escape_html(sms["sim"])}\n'
        f'├ {ce("💻")} <b>Device:</b>  {escape_html(sms["device"])}\n'
        f'╰ {ce("💵")} <b>Plan:</b>    {escape_html(sms["plan"])}\n\n'
        f'╭─ {ce("💬")} <b>MESSAGE</b>\n'
        f'╰ <i>{escape_html(sms["body"])}</i>'
    )


def format_stats_message(total: int, displayed: int, otps: int, session_sms: int, week_otps: int = 0) -> str:
    now = datetime.now()
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    week_label = f"Mon {monday.strftime('%d %b')} → Now"
    return (
        f'{ce("🏆")} <b>LIVE STATISTICS</b> {ce("🏆")}\n'
        f'{LINE_SEPARATOR}\n\n'
        f'╭─ {ce("⚡️")} <b>DATA</b>\n'
        f'├ {ce("💌")} Total SMS        →  <b>{total}</b>\n'
        f'├ {ce("📊")} Displayed        →  <b>{displayed}</b>\n'
        f'├ {ce("🎁")} OTPs detected    →  <b>{otps}</b>\n'
        f'├ {ce("📅")} OTPs this week   →  <b>{week_otps}</b>  <i>({week_label})</i>\n'
        f'╰ {ce("✨")} New this session →  <b>{session_sms}</b>\n\n'
        f'╭─ {ce("💎")} <b>SYSTEM</b>\n'
        f'├ {ce("🟢")} Status   →  <b>ACTIVE</b>\n'
        f'╰ {ce("🔄")} Refresh  →  <b>Every 5 seconds</b>\n'
        f'{LINE_SEPARATOR}'
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Premium Button Builders (with icon_custom_emoji_id)
# ──────────────────────────────────────────────────────────────────────────────
def with_icon(btn: dict, emoji: str) -> dict:
    """Add icon_custom_emoji_id to a button dict if available."""
    eid = ce_id(emoji)
    if eid:
        return {**btn, "icon_custom_emoji_id": eid}
    return btn


def build_otp_rows(sms: dict, store_id: str) -> list[list[dict]]:
    otp = extract_otp(sms["body"]) or ""
    return [
        [
            with_icon({"text": "Copy OTP", "callback_data": safe_callback_data("otp:", otp)}, "🔓"),
            with_icon({"text": "Copy Number", "callback_data": safe_callback_data("num:", sms["phone"])}, "📲"),
        ],
        [
            with_icon({"text": "Copy Message", "callback_data": f"msg:{store_id}"}, "💬"),
        ],
    ]


def build_sms_rows(sms: dict, store_id: str) -> list[list[dict]]:
    return [
        [
            with_icon({"text": "Copy Number", "callback_data": safe_callback_data("num:", sms["phone"])}, "📲"),
            with_icon({"text": "Copy Message", "callback_data": f"msg:{store_id}"}, "💬"),
        ],
    ]


# Legacy SDK keyboard builders (for fallback sends via python-telegram-bot)
def build_otp_keyboard(sms: dict, store_id: str) -> InlineKeyboardMarkup:
    otp = extract_otp(sms["body"]) or ""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔓 Copy OTP", callback_data=safe_callback_data("otp:", otp)),
            InlineKeyboardButton("📲 Copy Number", callback_data=safe_callback_data("num:", sms["phone"])),
        ],
        [
            InlineKeyboardButton("💬 Copy Message", callback_data=f"msg:{store_id}"),
        ],
    ])


def build_sms_keyboard(sms: dict, store_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📲 Copy Number", callback_data=safe_callback_data("num:", sms["phone"])),
            InlineKeyboardButton("💬 Copy Message", callback_data=f"msg:{store_id}"),
        ],
    ])


# ──────────────────────────────────────────────────────────────────────────────
#  Raw Send with Styled Buttons (3-tier fallback)
# ──────────────────────────────────────────────────────────────────────────────
_STYLES = ["primary", "success", "danger"]
_style_idx = 0

TG_EMOJI_RE = re.compile(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', re.DOTALL)


def _next_style() -> str:
    global _style_idx
    s = _STYLES[_style_idx % len(_STYLES)]
    _style_idx += 1
    return s


def _colorize(rows: list[list[dict]]) -> dict:
    out = []
    for row in rows:
        colored = []
        for btn in row:
            b = dict(btn)
            interactive = (
                (b.get("callback_data") and b["callback_data"] != "noop")
                or b.get("url")
                or b.get("copy_text")
            )
            if interactive and not b.get("style"):
                b["style"] = _next_style()
            colored.append(b)
        out.append(colored)
    return {"inline_keyboard": out}


def _strip_styles(kb: dict) -> dict:
    return {
        "inline_keyboard": [
            [{k: v for k, v in btn.items() if k != "style"} for btn in row]
            for row in kb["inline_keyboard"]
        ]
    }


def _strip_icons(kb: dict) -> dict:
    return {
        "inline_keyboard": [
            [{k: v for k, v in btn.items() if k not in ("style", "icon_custom_emoji_id")} for btn in row]
            for row in kb["inline_keyboard"]
        ]
    }


def _strip_tg_emoji(text: str) -> str:
    if "<tg-emoji" not in text:
        return text
    return TG_EMOJI_RE.sub(r'\1', text)


async def _tg_post(token: str, method: str, payload: dict) -> dict:
    """Raw HTTP POST to Telegram Bot API."""
    import aiohttp
    import json

    body = dict(payload)
    if "reply_markup" in body and isinstance(body["reply_markup"], dict):
        body["reply_markup"] = json.dumps(body["reply_markup"])

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.telegram.org/bot{token}/{method}",
                json=body,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                return await resp.json()
    except Exception as e:
        return {"ok": False, "description": str(e)}


async def raw_send(
    token: str,
    chat_id: int | str,
    text: str,
    rows: list[list[dict]],
    parse_mode: str = "HTML",
) -> dict:
    """
    Send a message with styled premium buttons via raw Telegram API.
    4-tier fallback: styled → no-styles → no-icons → strip tg-emoji from text.
    """
    kb = _colorize(rows)
    base = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }

    # Tier 1: full styled keyboard
    resp = await _tg_post(token, "sendMessage", {**base, "reply_markup": kb})
    if resp.get("ok"):
        return resp

    # Tier 2: strip button styles
    no_styles = _strip_styles(kb)
    resp = await _tg_post(token, "sendMessage", {**base, "reply_markup": no_styles})
    if resp.get("ok"):
        return resp

    # Tier 3a: strip premium icons too
    no_icons = _strip_icons(kb)
    resp = await _tg_post(token, "sendMessage", {**base, "reply_markup": no_icons})
    if resp.get("ok"):
        return resp

    # Tier 3b: strip <tg-emoji> tags from text
    if "<tg-emoji" in text:
        resp = await _tg_post(
            token,
            "sendMessage",
            {**base, "text": _strip_tg_emoji(text), "reply_markup": no_icons},
        )
        if resp.get("ok"):
            return resp

    logger.warning(f"rawSend failed after all fallbacks: {resp.get('description')}")
    raise RuntimeError(f"ETELEGRAM rawSend failed: {resp.get('description', 'unknown')}")


# ──────────────────────────────────────────────────────────────────────────────
#  Upstream API Client
# ──────────────────────────────────────────────────────────────────────────────
_sms_cache: dict[str, tuple[float, dict]] = {}
_nums_cache: dict[str, tuple[float, dict]] = {}
_inflight: dict[str, asyncio.Task] = {}

CACHE_TTL = 1.0    # seconds — serve fresh
STALE_TTL = 60.0   # seconds — serve stale on failure


async def _fetch_json(url: str) -> dict:
    """Fetch JSON from a URL with timeout."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(
            url,
            headers={"Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Upstream {url} returned HTTP {resp.status}")
            return await resp.json(content_type=None)


async def fetch_sms() -> dict:
    return await _fetch_json(f"{UPSTREAM_API_URL}/?type=sms")


async def fetch_numbers() -> dict:
    return await _fetch_json(f"{UPSTREAM_API_URL}/?type=numbers")


async def fetch_sms_cached() -> dict:
    return await _cached("sms", _sms_cache, fetch_sms)


async def fetch_numbers_cached() -> dict:
    return await _cached("numbers", _nums_cache, fetch_numbers)


async def _cached(key: str, cache: dict, loader) -> dict:
    now = time.time()
    hit = cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]

    # Check for in-flight request
    if key in _inflight and not _inflight[key].done():
        return await _inflight[key]

    async def _do_fetch():
        try:
            fresh = await loader()
            cache[key] = (time.time(), fresh)
            return fresh
        except Exception as err:
            if hit and now - hit[0] < STALE_TTL:
                logger.warning(f"Serving stale upstream data for {key}: {err}")
                return hit[1]
            raise
        finally:
            _inflight.pop(key, None)

    task = asyncio.create_task(_do_fetch())
    _inflight[key] = task
    return await task


# ──────────────────────────────────────────────────────────────────────────────
#  Access Control
# ──────────────────────────────────────────────────────────────────────────────
def is_allowed(user_id: int) -> bool:
    return user_id == OWNER_CHAT_ID or user_id in approved_users


# ──────────────────────────────────────────────────────────────────────────────
#  Command Handlers
# ──────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    user_id = user.id

    if not is_allowed(user_id):
        # Send access request to owner
        if user_id not in pending_users:
            pending_users[user_id] = {
                "first_name": user.first_name or "",
                "last_name": user.last_name or "",
                "username": user.username or "",
            }

            name = " ".join(filter(None, [user.first_name, user.last_name]))
            username = f"@{user.username}" if user.username else "no username"

            text = (
                f'{ce("🔔")} <b>ACCESS REQUEST</b> {ce("🔔")}\n'
                f'{LINE_SEPARATOR}\n\n'
                f'╭─ {ce("👤")} <b>USER INFO</b>\n'
                f'├ {ce("💫")} <b>Name</b>      {escape_html(name)}\n'
                f'├ {ce("✨")} <b>Username</b>  {escape_html(username)}\n'
                f'╰ {ce("🔢")} <b>User ID</b>   <code>{user_id}</code>\n\n'
                f'<i>This user wants access to Zone SMS Bot.</i>\n'
                f'<i>Approve or decline below.</i>'
            )

            rows = [
                [
                    with_icon({"text": "Approve", "callback_data": f"approve:{user_id}"}, "✅"),
                    with_icon({"text": "Decline", "callback_data": f"decline:{user_id}"}, "❌"),
                ],
            ]

            try:
                await raw_send(BOT_TOKEN, OWNER_CHAT_ID, text, rows)
            except Exception:
                # Fallback to SDK
                kb = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Approve", callback_data=f"approve:{user_id}"),
                        InlineKeyboardButton("❌ Decline", callback_data=f"decline:{user_id}"),
                    ],
                ])
                await context.bot.send_message(
                    OWNER_CHAT_ID, text=_strip_tg_emoji(text),
                    parse_mode=ParseMode.HTML, reply_markup=kb,
                )

        await update.message.reply_text(
            f'{ce("🔒")} <b>ACCESS REQUIRED</b> {ce("🔒")}\n'
            f'{LINE_SEPARATOR}\n\n'
            f'{ce("🔔")} Your request has been sent to the owner.\n'
            f'{ce("⏳")} Please wait for approval.\n\n'
            f'<i>You\'ll be notified here once the owner responds.</i>',
            parse_mode=ParseMode.HTML,
        )
        return

    welcome = (
        f'{ce("🚀")} <b>ZONE SMS MONITOR</b> {ce("🚀")}\n'
        f'{LINE_SEPARATOR}\n\n'
        f'{ce("🟢")} <b>ONLINE</b>  •  {ce("💎")} <b>PREMIUM ACTIVE</b>\n\n'
        f'╭─ {ce("🌟")} <b>FEATURES</b>\n'
        f'├ {ce("🎁")} Auto OTP detection\n'
        f'├ {ce("🔥")} Live SMS monitoring\n'
        f'├ {ce("🔓")} One-tap copy codes\n'
        f'├ {ce("🏆")} Real-time statistics\n'
        f'╰ {ce("🌐")} Multi-country support\n\n'
        f'╭─ {ce("⚡️")} <b>COMMANDS</b>\n'
        f'├ /stats  —  Live statistics\n'
        f'├ /status —  System health\n'
        f'╰ /help   —  Command guide\n\n'
        f'{ce("✨")} <i>Every new SMS arrives here instantly</i>'
    )
    await update.message.reply_text(welcome, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return

    help_text = (
        f'{ce("💎")} <b>HELP & GUIDE</b> {ce("💎")}\n'
        f'{LINE_SEPARATOR}\n\n'
        f'╭─ {ce("⚡️")} <b>COMMANDS</b>\n'
        f'├ /start   →  Welcome screen\n'
        f'├ /stats   →  Live statistics\n'
        f'├ /status  →  System health\n'
        f'╰ /help    →  This guide\n\n'
        f'╭─ {ce("✨")} <b>BUTTON ACTIONS</b>\n'
        f'├ {ce("🔓")}  Copy OTP      →  tappable OTP code\n'
        f'├ {ce("📲")}  Copy Number   →  tappable phone number\n'
        f'╰ {ce("💬")}  Copy Message  →  full message\n\n'
        f'{LINE_SEPARATOR}\n'
        f'{ce("💡")} <i>Tap any</i> <code>highlighted code</code> <i>to copy instantly</i>'
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)


# Stats counters (module-level, accessed by polling loop and command handlers)
otp_count = 0
total_sms_today = 0
otp_timestamps: list[float] = []  # unix timestamps of every OTP detected this session


def count_otps_since_monday() -> int:
    """Return how many OTPs were detected from Monday 00:00 (local time) until now."""
    now = datetime.now()
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    cutoff = monday.timestamp()
    return sum(1 for t in otp_timestamps if t >= cutoff)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return

    api_total = 0
    api_displayed = 0
    api_error = False

    try:
        data = await fetch_sms_cached()
        api_total = data.get("total", 0)
        api_displayed = len(data.get("records", []))
    except Exception as err:
        logger.error(f"cmd_stats API fetch failed: {err}")
        api_error = True

    text = format_stats_message(
        api_total,
        api_displayed,
        otp_count,
        total_sms_today,
        count_otps_since_monday(),
    )
    if api_error:
        text += f'\n\n{ce("⚠️")} <i>API offline — showing session data only</i>'

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="refresh_stats")],
    ])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return

    status_text = (
        f'{ce("🛡")} <b>SYSTEM HEALTH</b> {ce("🛡")}\n'
        f'{LINE_SEPARATOR}\n\n'
        f'╭─ {ce("🔥")} <b>SERVICES</b>\n'
        f'├ {ce("🤖")} Bot         {ce("🟢")}  <b>Online</b>\n'
        f'├ {ce("🌐")} SMS API     {ce("🟢")}  <b>Connected</b>\n'
        f'╰ {ce("⚡️")} Poll rate   {ce("🟢")}  <b>Every 5 sec</b>\n\n'
        f'╭─ {ce("📊")} <b>SESSION</b>\n'
        f'├ {ce("🎁")} OTPs detected    →  <b>{otp_count}</b>\n'
        f'├ {ce("💌")} SMS this session →  <b>{total_sms_today}</b>\n'
        f'├ {ce("👥")} Approved users   →  <b>{len(approved_users)}</b>\n'
        f'╰ {ce("⏳")} Pending requests →  <b>{len(pending_users)}</b>\n'
        f'{LINE_SEPARATOR}\n'
        f'{ce("✅")} <i>All systems operational</i>'
    )
    await update.message.reply_text(status_text, parse_mode=ParseMode.HTML)


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_CHAT_ID:
        return

    approved_list = ", ".join(
        str(uid) for uid in approved_users if uid != OWNER_CHAT_ID
    ) or "none"

    pending_list = ", ".join(
        f'{info.get("first_name", "")} ({uid})'
        for uid, info in pending_users.items()
    ) or "none"

    await update.message.reply_text(
        f'{ce("👥")} <b>USER MANAGEMENT</b> {ce("👥")}\n'
        f'{LINE_SEPARATOR}\n\n'
        f'╭─ {ce("✅")} <b>Approved</b>\n'
        f'╰ {escape_html(approved_list)}\n\n'
        f'╭─ {ce("⏳")} <b>Pending</b>\n'
        f'╰ {escape_html(pending_list)}\n'
        f'{LINE_SEPARATOR}',
        parse_mode=ParseMode.HTML,
    )


async def cmd_testmsg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_CHAT_ID:
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fake_sms: dict = {
        "timestamp": now,
        "sim": "SIM 1 (PK)",
        "phone": "+92 300 1234567",
        "device": "Zone TestDevice",
        "currency": "PKR",
        "plan": "Zong Premium",
        "status": 1,
        "body": "Your OTP code is 847291. Do not share with anyone.",
    }

    # Send OTP test message
    otp_store_id = store_message(fake_sms)
    otp_text = format_otp_message(fake_sms)
    otp_rows = build_otp_rows(fake_sms, otp_store_id)

    try:
        await raw_send(BOT_TOKEN, update.effective_chat.id, otp_text, otp_rows)
    except Exception:
        kb = build_otp_keyboard(fake_sms, otp_store_id)
        await update.message.reply_text(
            _strip_tg_emoji(otp_text), parse_mode=ParseMode.HTML, reply_markup=kb,
        )

    # Send standard SMS test message
    fake_sms2 = {
        **fake_sms,
        "status": 0,
        "body": "Your account statement for March 2026 is ready. Visit portal.bank.com to view.",
    }
    sms_store_id = store_message(fake_sms2)
    sms_text = format_sms_message(fake_sms2)
    sms_rows = build_sms_rows(fake_sms2, sms_store_id)

    try:
        await raw_send(BOT_TOKEN, update.effective_chat.id, sms_text, sms_rows)
    except Exception:
        kb = build_sms_keyboard(fake_sms2, sms_store_id)
        await update.message.reply_text(
            _strip_tg_emoji(sms_text), parse_mode=ParseMode.HTML, reply_markup=kb,
        )


# ──────────────────────────────────────────────────────────────────────────────
#  Callback Query Handler
# ──────────────────────────────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    actor_id = query.from_user.id
    chat_id = str(query.message.chat.id)

    try:
        # ── Owner-only: approve / decline ──
        if data.startswith("approve:") or data.startswith("decline:"):
            if actor_id != OWNER_CHAT_ID:
                await query.answer("❌ Only the owner can do this.", show_alert=True)
                return

            target_id = int(data.split(":")[1])
            target_info = pending_users.get(target_id, {})
            name = " ".join(filter(None, [
                target_info.get("first_name", ""),
                target_info.get("last_name", ""),
            ])) or str(target_id)

            if data.startswith("approve:"):
                approved_users.add(target_id)
                pending_users.pop(target_id, None)

                await query.answer(f"✅ {name} approved", show_alert=False)

                await query.edit_message_text(
                    f'{ce("🟢")} <b>ACCESS APPROVED</b> {ce("🟢")}\n'
                    f'{LINE_SEPARATOR}\n\n'
                    f'╭─ {ce("👤")} <b>{escape_html(name)}</b>\n'
                    f'├ {ce("🔢")} ID: <code>{target_id}</code>\n'
                    f'╰ {ce("✅")} Status: <b>Approved</b>',
                    parse_mode=ParseMode.HTML,
                )

                try:
                    await context.bot.send_message(
                        target_id,
                        f'{ce("🎉")} <b>ACCESS GRANTED</b> {ce("🎉")}\n'
                        f'{LINE_SEPARATOR}\n\n'
                        f'{ce("✅")} The owner has approved your request.\n'
                        f'{ce("💎")} You now have full access to Zone SMS Bot.\n\n'
                        f'Use /start to begin.',
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass

            else:  # decline
                pending_users.pop(target_id, None)

                await query.answer(f"❌ {name} declined", show_alert=False)

                await query.edit_message_text(
                    f'{ce("🔴")} <b>ACCESS DECLINED</b> {ce("🔴")}\n'
                    f'{LINE_SEPARATOR}\n\n'
                    f'╭─ {ce("👤")} <b>{escape_html(name)}</b>\n'
                    f'├ {ce("🔢")} ID: <code>{target_id}</code>\n'
                    f'╰ {ce("❌")} Status: <b>Declined</b>',
                    parse_mode=ParseMode.HTML,
                )

                try:
                    await context.bot.send_message(
                        target_id,
                        f'{ce("🚫")} <b>ACCESS DENIED</b> {ce("🚫")}\n'
                        f'{LINE_SEPARATOR}\n\n'
                        f'<i>The owner has declined your access request.</i>',
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
            return

        # ── All other callbacks require access ──
        if not is_allowed(actor_id):
            await query.answer("🚫 No access. Send /start to request.", show_alert=True)
            return

        if data.startswith("otp:"):
            otp = data[4:]
            await query.answer("🔓 OTP copied — tap to use!", show_alert=False)
            await context.bot.send_message(
                actor_id,
                f'{ce("🔓")} <b>OTP CODE</b> {ce("🔓")}\n'
                f'{LINE_SEPARATOR}\n\n'
                f'<code>{escape_html(otp)}</code>\n\n'
                f'{ce("⬆️")} <i>Tap the code above to copy instantly</i>',
                parse_mode=ParseMode.HTML,
            )

        elif data.startswith("num:"):
            num = data[4:]
            await query.answer("📲 Number copied!", show_alert=False)
            await context.bot.send_message(
                actor_id,
                f'{ce("📲")} <b>PHONE NUMBER</b> {ce("📲")}\n'
                f'{LINE_SEPARATOR}\n\n'
                f'<code>{escape_html(num)}</code>\n\n'
                f'{ce("⬆️")} <i>Tap the number above to copy instantly</i>',
                parse_mode=ParseMode.HTML,
            )

        elif data.startswith("msg:"):
            store_id = data[4:]
            sms = message_store.get(store_id)
            if sms:
                await query.answer("💬 Message copied!", show_alert=False)
                await context.bot.send_message(
                    actor_id,
                    f'{ce("💬")} <b>FULL MESSAGE</b> {ce("💬")}\n'
                    f'{LINE_SEPARATOR}\n\n'
                    f'<code>{escape_html(sms["body"])}</code>\n\n'
                    f'{ce("⬆️")} <i>Tap the text above to copy instantly</i>',
                    parse_mode=ParseMode.HTML,
                )
            else:
                await query.answer("⚠️ Message expired from cache", show_alert=True)

        elif data == "refresh_stats":
            await query.answer("⚡️ Refreshing...", show_alert=False)
            api_total = 0
            api_displayed = 0
            api_err = False
            try:
                d = await fetch_sms_cached()
                api_total = d.get("total", 0)
                api_displayed = len(d.get("records", []))
            except Exception as err:
                logger.error(f"refresh_stats API fetch failed: {err}")
                api_err = True
            stats_text = format_stats_message(
                api_total,
                api_displayed,
                otp_count,
                total_sms_today,
                count_otps_since_monday(),
            )
            if api_err:
                stats_text += f'\n\n{ce("⚠️")} <i>API offline — showing session data only</i>'
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="refresh_stats")],
            ])
            try:
                await query.edit_message_text(
                    stats_text, parse_mode=ParseMode.HTML, reply_markup=kb,
                )
            except Exception as err:
                logger.error(f"refresh_stats edit failed: {err}")

    except Exception as err:
        logger.error(f"Callback query error: {err}")


# ──────────────────────────────────────────────────────────────────────────────
#  SMS Polling Loop
# ──────────────────────────────────────────────────────────────────────────────
latest_seen_timestamp = ""
seen_keys: set[str] = set()
is_first_run = True
poll_in_progress = False


async def poll_sms(context: ContextTypes.DEFAULT_TYPE) -> None:
    global latest_seen_timestamp, is_first_run, otp_count, total_sms_today, poll_in_progress, otp_timestamps

    if poll_in_progress:
        return
    poll_in_progress = True

    try:
        data = await fetch_sms_cached()
        records = data.get("records", [])

        if is_first_run:
            for rec in records:
                sms = parse_sms_record(rec)
                if not is_valid_sms(sms):
                    continue
                if sms["timestamp"] > latest_seen_timestamp:
                    latest_seen_timestamp = sms["timestamp"]
                seen_keys.add(make_message_key(sms))
            is_first_run = False
            logger.info(
                f"SMS cache initialized: {len(records)} records, "
                f"latest={latest_seen_timestamp}"
            )
            return

        new_messages: list[dict] = []
        for rec in records:
            sms = parse_sms_record(rec)
            if not is_valid_sms(sms):
                continue
            if sms["timestamp"] <= latest_seen_timestamp:
                continue
            key = make_message_key(sms)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            new_messages.append(sms)

        # Advance timestamp baseline
        for sms in new_messages:
            if sms["timestamp"] > latest_seen_timestamp:
                latest_seen_timestamp = sms["timestamp"]

        if not FORWARD_SMS:
            for sms in new_messages:
                if is_otp_message(sms["body"]):
                    otp_count += 1
                    otp_timestamps.append(time.time())
                total_sms_today += 1
                logger.info(f"Dev mode: SMS tracked (not forwarded) — {sms['phone']}")
            return

        # Process new messages (oldest first)
        for sms in reversed(new_messages):
            # Skip body duplicates
            if is_body_duplicate(sms):
                logger.info(f"Skipped duplicate SMS body: {sms['phone']}")
                continue

            total_sms_today += 1
            store_id = store_message(sms)
            has_otp = is_otp_message(sms["body"])
            otp = extract_otp(sms["body"])

            try:
                if has_otp and otp:
                    otp_count += 1
                    otp_timestamps.append(time.time())
                    text = format_otp_message(sms)
                    rows = build_otp_rows(sms, store_id)
                    try:
                        await raw_send(BOT_TOKEN, OWNER_CHAT_ID, text, rows)
                    except Exception:
                        # Fallback to SDK
                        kb = build_otp_keyboard(sms, store_id)
                        await context.bot.send_message(
                            OWNER_CHAT_ID,
                            text=_strip_tg_emoji(text),
                            parse_mode=ParseMode.HTML,
                            reply_markup=kb,
                        )
                    logger.info(f"OTP SMS sent: {sms['phone']} → {otp}")
                else:
                    text = format_sms_message(sms)
                    rows = build_sms_rows(sms, store_id)
                    try:
                        await raw_send(BOT_TOKEN, OWNER_CHAT_ID, text, rows)
                    except Exception:
                        kb = build_sms_keyboard(sms, store_id)
                        await context.bot.send_message(
                            OWNER_CHAT_ID,
                            text=_strip_tg_emoji(text),
                            parse_mode=ParseMode.HTML,
                            reply_markup=kb,
                        )
                    logger.info(f"SMS sent: {sms['phone']}")

            except Exception as err:
                err_msg = str(err)
                is_markup_error = (
                    "ETELEGRAM" in err_msg
                    and ("Bad Request" in err_msg or "BUTTON_DATA_INVALID" in err_msg or "can't parse" in err_msg)
                )
                if is_markup_error:
                    logger.warning(f"Markup error, retrying plain: {err_msg}")
                    try:
                        plain_text = format_otp_message(sms) if (has_otp and otp) else format_sms_message(sms)
                        await context.bot.send_message(
                            OWNER_CHAT_ID,
                            text=_strip_tg_emoji(plain_text),
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception as fallback_err:
                        logger.error(f"Plain-text fallback also failed: {fallback_err}")
                else:
                    logger.error(f"Send failed (not retrying): {err_msg}")

    except Exception as err:
        logger.error(f"Error polling SMS API: {err}")
    finally:
        poll_in_progress = False


# ──────────────────────────────────────────────────────────────────────────────
#  Numbers Polling Loop
# ──────────────────────────────────────────────────────────────────────────────
seen_num_phones: set[str] = set()
seen_ranges: set[str] = set()
is_first_num_run = True


async def poll_numbers(context: ContextTypes.DEFAULT_TYPE) -> None:
    global is_first_num_run

    try:
        data = await fetch_numbers_cached()
        records = data.get("records", [])

        if is_first_num_run:
            for rec in records:
                phone = rec.get("termination", "")
                range_name = rec.get("number", "")
                if phone and phone != "0":
                    seen_num_phones.add(phone)
                if range_name and range_name != "0":
                    seen_ranges.add(range_name)
            is_first_num_run = False
            logger.info(
                f"Numbers cache initialized: {len(seen_num_phones)} phones, "
                f"{len(seen_ranges)} ranges"
            )
            return

        if not FORWARD_SMS:
            return

        new_range_map: dict[str, list[str]] = {}
        new_phones_by_range: dict[str, list[str]] = {}

        for rec in records:
            range_name = rec.get("number", "")
            phone = rec.get("termination", "")
            if not phone or phone == "0":
                continue

            is_new_phone = phone not in seen_num_phones
            is_new_range = range_name and range_name != "0" and range_name not in seen_ranges

            if is_new_range:
                seen_ranges.add(range_name)
                if range_name not in new_range_map:
                    new_range_map[range_name] = []
            if is_new_phone:
                seen_num_phones.add(phone)
                if is_new_range:
                    new_range_map[range_name].append(phone)
                else:
                    if range_name not in new_phones_by_range:
                        new_phones_by_range[range_name] = []
                    new_phones_by_range[range_name].append(phone)

        # Notify: new ranges
        for range_name, phones in new_range_map.items():
            preview = phones[:8]
            extra = len(phones) - len(preview)
            lines_list = []
            for i, p in enumerate(preview):
                prefix = "╰" if (i == len(preview) - 1 and extra == 0) else "├"
                lines_list.append(f'{prefix} <code>{escape_html(p)}</code>')
            lines = "\n".join(lines_list)

            text = (
                f'{ce("📡")} <b>NEW RANGE ADDED</b> {ce("📡")}\n'
                f'{LINE_SEPARATOR}\n\n'
                f'╭─ {ce("✨")} <b>DETAILS</b>\n'
                f'├ {ce("🃏")} <b>Range:</b>   {escape_html(range_name)}\n'
                f'╰ {ce("💌")} <b>Numbers:</b> <b>{len(phones)}</b> added\n\n'
            )
            if phones:
                text += f'╭─ {ce("📲")} <b>NUMBERS</b>\n{lines}'
                if extra > 0:
                    text += f'\n╰ <i>+{extra} more...</i>'

            try:
                await context.bot.send_message(
                    OWNER_CHAT_ID, text=text, parse_mode=ParseMode.HTML,
                )
            except Exception:
                await context.bot.send_message(
                    OWNER_CHAT_ID, text=_strip_tg_emoji(text), parse_mode=ParseMode.HTML,
                )
            logger.info(f"New range notification sent: {range_name} ({len(phones)} numbers)")

        # Notify: new phones in existing ranges
        for range_name, phones in new_phones_by_range.items():
            preview = phones[:8]
            extra = len(phones) - len(preview)
            lines_list = []
            for i, p in enumerate(preview):
                prefix = "╰" if (i == len(preview) - 1 and extra == 0) else "├"
                lines_list.append(f'{prefix} <code>{escape_html(p)}</code>')
            lines = "\n".join(lines_list)

            text = (
                f'{ce("💌")} <b>NEW NUMBERS ADDED</b> {ce("💌")}\n'
                f'{LINE_SEPARATOR}\n\n'
                f'╭─ {ce("✨")} <b>DETAILS</b>\n'
                f'├ {ce("🃏")} <b>Range:</b>  {escape_html(range_name)}\n'
                f'╰ {ce("⚡️")} <b>Added:</b>  <b>{len(phones)}</b> new numbers\n\n'
                f'╭─ {ce("📲")} <b>NUMBERS</b>\n{lines}'
            )
            if extra > 0:
                text += f'\n╰ <i>+{extra} more...</i>'

            try:
                await context.bot.send_message(
                    OWNER_CHAT_ID, text=text, parse_mode=ParseMode.HTML,
                )
            except Exception:
                await context.bot.send_message(
                    OWNER_CHAT_ID, text=_strip_tg_emoji(text), parse_mode=ParseMode.HTML,
                )
            logger.info(f"New numbers notification sent: {range_name} ({len(phones)} numbers)")

    except Exception as err:
        logger.error(f"Error polling Numbers API: {err}")


# ──────────────────────────────────────────────────────────────────────────────
#  Main
# ──────────────────────────────────────────────────────────────────────────────
def main() -> None:
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set — exiting")
        return
    if not OWNER_CHAT_ID:
        logger.error("OWNER_CHAT_ID not set — exiting")
        return

    # Pre-approve the owner
    approved_users.add(OWNER_CHAT_ID)

    logger.info(f"Starting Zone SMS Monitor Bot (owner={OWNER_CHAT_ID})")

    # Build application
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("testmsg", cmd_testmsg))

    # Register callback query handler
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Register bot commands with Telegram
    async def post_init(application) -> None:
        await application.bot.delete_webhook(drop_pending_updates=True)
        await application.bot.set_my_commands([
            ("start", "Welcome & activate access"),
            ("stats", "Live SMS & OTP statistics"),
            ("status", "System health check"),
            ("help", "Command reference & guide"),
            ("users", "Manage users (owner only)"),
            ("testmsg", "Preview OTP & SMS message format (owner)"),
        ])
        # Load custom emoji packs
        await load_custom_emoji_packs(application.bot)
        logger.info("Bot initialized, emoji packs loaded")

    app.post_init = post_init

    # Schedule polling jobs
    app.job_queue.run_repeating(poll_sms, interval=SMS_POLL_INTERVAL, first=2)
    app.job_queue.run_repeating(poll_numbers, interval=NUMS_POLL_INTERVAL, first=5)

    logger.info("Bot is running... (polling mode)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
