"""
Instagram Downloader Telegram Bot — Professional Edition

Features:
- Downloads Instagram posts/reels via instagrapi (session-based, no repeated logins)
- Mandatory channel membership gate before use
- Freemium model: N free downloads total, then Telegram Stars subscription for unlimited
- Clean inline-button UX
- SQLite persistence
- Admin stats

Environment variables (set these as Secrets — never hardcode credentials):
    BOT_TOKEN            (required) Telegram bot token from @BotFather
    ADMIN_ID             (required) numeric Telegram ID of the bot owner
    IG_USERNAME          (required) Instagram account username used for fetching
    IG_PASSWORD          (required) Instagram account password
    FORCE_SUB_CHANNEL    (required) channel username the user must join, e.g. @diaco_game1
    FREE_DOWNLOAD_LIMIT  (optional, default 5)  total free downloads per user (lifetime)
    STARS_PRICE          (optional, default 100) subscription price in Telegram Stars
"""

import os
import json
import logging
import sqlite3
import asyncio
import concurrent.futures
from datetime import datetime, timedelta

from instagrapi import Client
from instagrapi.exceptions import ClientError

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------
# ------------------------------------------------------------------
# Configuration — fill in your own values below
# ------------------------------------------------------------------
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"          # from @BotFather
ADMIN_ID = 0                                # your numeric Telegram ID, e.g. 123456789
IG_USERNAME = "YOUR_INSTAGRAM_USERNAME"     # Instagram account used to fetch content
IG_PASSWORD = "YOUR_INSTAGRAM_PASSWORD"     # its password
FORCE_SUB_CHANNEL = "@your_channel_here"    # channel users must join, e.g. "@diaco_game1"

FREE_DOWNLOAD_LIMIT = 5
STARS_PRICE = 100
SUBSCRIPTION_DAYS = 30

DB_PATH = "instabot.db"
SESSION_FILE = "ig_session.json"

executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)


# ------------------------------------------------------------------
# Instagram client (single shared session, thread-safe enough for this use case)
# ------------------------------------------------------------------
cl = Client()


def instagram_login() -> None:
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r") as f:
                settings = json.load(f)
            cl.set_settings(settings)
            cl.login(IG_USERNAME, IG_PASSWORD)
            logger.info("Logged into Instagram using saved session.")
            return
        except Exception:
            logger.warning("Saved session invalid, logging in fresh.")

    cl.login(IG_USERNAME, IG_PASSWORD)
    with open(SESSION_FILE, "w") as f:
        json.dump(cl.get_settings(), f)
    logger.info("Logged into Instagram with fresh session.")


def fetch_instagram_media(url: str):
    """Blocking call — always run inside the thread pool executor."""
    media_pk = cl.media_pk_from_url(url)
    info = cl.media_info(media_pk)
    caption = info.caption_text or "No caption."
    username = info.user.username if info.user else "unknown"
    is_video = info.media_type == 2
    media_url = info.video_url if is_video else info.thumbnail_url
    return is_video, str(media_url), caption, username


# ------------------------------------------------------------------
# Database layer
# ------------------------------------------------------------------
def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_seen TEXT,
            downloads_used INTEGER DEFAULT 0,
            subscribed_until TEXT
        )"""
    )
    conn.commit()
    conn.close()


def ensure_user(user_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, first_seen, downloads_used, subscribed_until) "
        "VALUES (?, ?, 0, NULL)",
        (user_id, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_downloads_used(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT downloads_used FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0


def increment_downloads(user_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET downloads_used = downloads_used + 1 WHERE user_id = ?", (user_id,)
    )
    conn.commit()
    conn.close()


def is_subscribed(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT subscribed_until FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row or not row[0]:
        return False
    return datetime.now() < datetime.fromisoformat(row[0])


def subscription_expiry(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT subscribed_until FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def activate_subscription(user_id: int, days: int = SUBSCRIPTION_DAYS):
    now = datetime.now()
    current = subscription_expiry(user_id)
    base = current if current and current > now else now
    new_expiry = base + timedelta(days=days)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET subscribed_until = ? WHERE user_id = ?", (new_expiry.isoformat(), user_id))
    conn.commit()
    conn.close()
    return new_expiry


def get_stats() -> dict:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    cur.execute("SELECT SUM(downloads_used) FROM users")
    total_downloads = cur.fetchone()[0] or 0
    cur.execute(
        "SELECT COUNT(*) FROM users WHERE subscribed_until IS NOT NULL AND subscribed_until > ?",
        (datetime.now().isoformat(),),
    )
    active_subs = cur.fetchone()[0]
    conn.close()
    return {"total_users": total_users, "total_downloads": total_downloads, "active_subs": active_subs}


# ------------------------------------------------------------------
# Channel membership gate
# ------------------------------------------------------------------
async def is_channel_member(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if not FORCE_SUB_CHANNEL:
        return True
    try:
        member = await context.bot.get_chat_member(chat_id=FORCE_SUB_CHANNEL, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        logger.exception("Failed to check channel membership")
        return False


def join_channel_keyboard() -> InlineKeyboardMarkup:
    channel_url = f"https://t.me/{FORCE_SUB_CHANNEL.lstrip('@')}"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Join Channel", url=channel_url)],
            [InlineKeyboardButton("✅ I've Joined", callback_data="check_join")],
        ]
    )


# ------------------------------------------------------------------
# Command handlers
# ------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)

    if not await is_channel_member(context, user_id):
        await update.message.reply_text(
            "🔒 To use this bot, please join our channel first.",
            reply_markup=join_channel_keyboard(),
        )
        return

    await update.message.reply_text(
        "👋 <b>Welcome to Instagram Downloader Bot!</b>\n\n"
        "Send me any Instagram post or reel link and I'll fetch it for you — video, "
        "caption, and page name included.\n\n"
        f"🎁 You get <b>{FREE_DOWNLOAD_LIMIT} free downloads</b>. "
        f"After that, unlock unlimited downloads for just <b>{STARS_PRICE} ⭐</b>/month.\n\n"
        "<b>Commands</b>\n"
        "/status — check your remaining downloads\n"
        "/subscribe — get unlimited access",
        parse_mode="HTML",
    )


async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    if await is_channel_member(context, user_id):
        await query.answer("✅ Verified! You're all set.")
        await query.edit_message_text(
            "✅ Thanks for joining! Send me an Instagram link to get started."
        )
    else:
        await query.answer("❌ You haven't joined the channel yet.", show_alert=True)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)

    if is_subscribed(user_id):
        expiry = subscription_expiry(user_id)
        text = f"🟢 <b>Unlimited plan active</b> — until {expiry.strftime('%Y-%m-%d')}"
    else:
        used = get_downloads_used(user_id)
        remaining = max(0, FREE_DOWNLOAD_LIMIT - used)
        text = f"🎁 Free downloads remaining: <b>{remaining}/{FREE_DOWNLOAD_LIMIT}</b>"

    await update.message.reply_text(text, parse_mode="HTML")


async def send_stars_invoice(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_invoice(
        chat_id=chat_id,
        title="Unlimited Monthly Access",
        description=f"Unlimited Instagram downloads for {SUBSCRIPTION_DAYS} days.",
        payload="monthly_subscription",
        provider_token="",  # required empty for Telegram Stars
        currency="XTR",
        prices=[LabeledPrice("1-month subscription", STARS_PRICE)],
    )


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_stars_invoice(update.effective_chat.id, context)


async def buy_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Generating your payment invoice... 💫")
    await send_stars_invoice(update.effective_chat.id, context)


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    new_expiry = activate_subscription(user_id, SUBSCRIPTION_DAYS)
    await update.message.reply_text(
        f"✅ <b>Payment successful!</b>\nUnlimited downloads active until "
        f"{new_expiry.strftime('%Y-%m-%d')}.",
        parse_mode="HTML",
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    s = get_stats()
    await update.message.reply_text(
        "📊 <b>Bot Statistics</b>\n\n"
        f"👥 Total users: {s['total_users']}\n"
        f"⬇️ Total downloads: {s['total_downloads']}\n"
        f"💳 Active subscriptions: {s['active_subs']}",
        parse_mode="HTML",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)

    if not await is_channel_member(context, user_id):
        await update.message.reply_text(
            "🔒 Please join our channel first to use this bot.",
            reply_markup=join_channel_keyboard(),
        )
        return

    url = update.message.text.strip()
    if "instagram.com" not in url:
        await update.message.reply_text("Please send a valid Instagram post or reel link.")
        return

    if not is_subscribed(user_id):
        used = get_downloads_used(user_id)
        if used >= FREE_DOWNLOAD_LIMIT:
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"💎 Unlock Unlimited — {STARS_PRICE} ⭐", callback_data="buy_sub")]]
            )
            await update.message.reply_text(
                "You've used all your free downloads.\nUpgrade for unlimited access:",
                reply_markup=keyboard,
            )
            return

    progress_msg = await update.message.reply_text("⏳ Fetching your media...")

    try:
        loop = asyncio.get_event_loop()
        is_video, media_url, caption, username = await loop.run_in_executor(
            executor, fetch_instagram_media, url
        )

        short_caption = (caption[:200] + "…") if len(caption) > 200 else caption
        final_caption = f"📝 {short_caption}\n👤 @{username}"

        await progress_msg.delete()
        if is_video:
            await update.message.reply_video(media_url, caption=final_caption)
        else:
            await update.message.reply_photo(media_url, caption=final_caption)

        increment_downloads(user_id)

        if not is_subscribed(user_id):
            used = get_downloads_used(user_id)
            remaining = max(0, FREE_DOWNLOAD_LIMIT - used)
            if remaining > 0:
                await update.message.reply_text(f"✅ Done! {remaining} free download(s) left.")
            else:
                keyboard = InlineKeyboardMarkup(
                    [[InlineKeyboardButton(f"💎 Unlock Unlimited — {STARS_PRICE} ⭐", callback_data="buy_sub")]]
                )
                await update.message.reply_text(
                    "✅ Done! That was your last free download.",
                    reply_markup=keyboard,
                )

    except ClientError:
        logger.exception("Instagram client error")
        await progress_msg.edit_text(
            "❌ Couldn't fetch this post. It might be private, deleted, or the link is invalid."
        )
    except Exception:
        logger.exception("Unexpected error while fetching media")
        await progress_msg.edit_text("❌ Something went wrong. Please try again in a moment.")


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------
def main() -> None:
    placeholders = {
        "BOT_TOKEN": "YOUR_BOT_TOKEN_HERE",
        "IG_USERNAME": "YOUR_INSTAGRAM_USERNAME",
        "IG_PASSWORD": "YOUR_INSTAGRAM_PASSWORD",
    }
    not_filled_in = [
        name for name, placeholder in placeholders.items()
        if globals()[name] == placeholder
    ]
    if not_filled_in:
        raise SystemExit(
            f"❌ Please fill in these values at the top of the file: {', '.join(not_filled_in)}"
        )

    init_db()
    instagram_login()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("subscribe", subscribe_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CallbackQueryHandler(check_join_callback, pattern=r"^check_join$"))
    app.add_handler(CallbackQueryHandler(buy_sub_callback, pattern=r"^buy_sub$"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
