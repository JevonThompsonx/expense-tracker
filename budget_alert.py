#!/usr/bin/env python3
"""
Budget Alert Script

Run via cron or systemd timer to send spending summaries to both users.
Uses COALESCE to handle periods with no transactions gracefully.
Retries failed sends up to MAX_RETRIES times.
"""

import asyncio
import logging
import sqlite3
import sys
from datetime import datetime

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from config import TELEGRAM_BOT_TOKEN, DATABASE_PATH, PRINCESS_CHAT_ID, JAY_CHAT_ID


def _parse_split_ratio(split_ratio: str | None) -> tuple[float, float]:
    """Return (princess_pct, jay_pct) from a 'P/J' string, defaulting to 50/50."""
    if not split_ratio:
        return 50.0, 50.0
    try:
        p, j = split_ratio.split("/")
        return float(p), float(j)
    except (ValueError, AttributeError):
        return 50.0, 50.0

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds between retries


def get_budget_data() -> dict | None:
    """
    Fetch settings and period spending in a single DB round-trip.
    Returns None if chat IDs aren't configured or no active period exists.
    COALESCE ensures zero totals (not NULL) when there are no transactions.

    Returned dict includes:
      princess_chat, jay_chat, budget,
      princess_total, jay_total, period_start,
      days_in_period, princess_net
    """
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")

        cursor.execute(
            "SELECT key, value FROM settings "
            "WHERE key IN ('princess_chat_id', 'jay_chat_id', 'monthly_budget')"
        )
        settings = dict(cursor.fetchall())

        princess_chat = settings.get("princess_chat_id", "").strip() or PRINCESS_CHAT_ID
        jay_chat = settings.get("jay_chat_id", "").strip() or JAY_CHAT_ID
        budget = float(settings.get("monthly_budget", "600.0"))

        if not princess_chat or not jay_chat:
            logger.warning("Chat IDs not configured in DB or .env -- skipping alert")
            return None

        # Fetch period start date
        cursor.execute("SELECT start_date FROM periods WHERE is_active = 1")
        period_row = cursor.fetchone()
        if not period_row:
            logger.warning("No active period found -- skipping alert")
            return None
        period_start = period_row[0]

        # Fetch all transactions for the active period (need split_ratio for settlement)
        cursor.execute(
            """SELECT payer, amount, split_ratio
               FROM transactions
               WHERE period_id = (SELECT id FROM periods WHERE is_active = 1)"""
        )
        tx_rows = cursor.fetchall()

        princess_total = sum(amt for payer, amt, _ in tx_rows if payer == "Princess")
        jay_total = sum(amt for payer, amt, _ in tx_rows if payer == "Jay")

        # Settlement: what each person owes based on split ratios
        princess_owes = 0.0
        for _payer, amt, split_ratio in tx_rows:
            p_pct, _j_pct = _parse_split_ratio(split_ratio)
            princess_owes += amt * p_pct / 100.0

        princess_net = princess_total - princess_owes  # >0 Jay owes P, <0 P owes Jay

        days_in_period = (datetime.now() - datetime.fromisoformat(period_start)).days

        return {
            "princess_chat": princess_chat,
            "jay_chat": jay_chat,
            "budget": budget,
            "princess_total": princess_total,
            "jay_total": jay_total,
            "period_start": period_start,
            "days_in_period": days_in_period,
            "princess_net": princess_net,
        }
    finally:
        conn.close()


async def send_with_retry(bot: Bot, chat_id: str, message: str) -> bool:
    """Send a Telegram message with linear-backoff retry. Returns True on success."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode=ParseMode.MARKDOWN,
            )
            logger.info("Sent alert to chat_id=%s", chat_id)
            return True
        except TelegramError as exc:
            logger.warning(
                "Send attempt %d/%d failed for chat_id=%s: %s",
                attempt, MAX_RETRIES, chat_id, exc,
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * attempt)

    logger.error("All %d attempts failed for chat_id=%s", MAX_RETRIES, chat_id)
    return False


def build_alert_message(
    princess_total: float,
    jay_total: float,
    budget: float,
    days_in_period: int,
    princess_net: float,
) -> str:
    """
    Build the budget-alert message string.

    Separated from send_budget_alert() so it can be unit-tested without
    touching the DB or Telegram API.

    Uses Telegram MarkdownV1 bold (*text*, not **text**).
    Includes a settlement line so recipients know who owes whom.
    """
    total_spent = princess_total + jay_total
    remaining = budget - total_spent
    percent = (total_spent / budget * 100) if budget > 0 else 0.0

    if percent >= 100:
        status_icon, status_text = "\u26a0\ufe0f", "Over budget!"
    elif percent >= 80:
        status_icon, status_text = "\u26a1", "Approaching limit!"
    elif percent >= 50:
        status_icon, status_text = "\U0001f4c8", "On track"
    else:
        status_icon, status_text = "\U0001f3af", "Keep it up!"

    # Settlement line
    if abs(princess_net) < 0.01:
        settle_line = "\u2705 All settled up!"
    elif princess_net > 0:
        settle_line = f"\U0001f4b8 Jay owes Princess: ${abs(princess_net):.2f}"
    else:
        settle_line = f"\U0001f4b8 Princess owes Jay: ${abs(princess_net):.2f}"

    return (
        f"\U0001f4b0 *Budget Update* (Day {days_in_period})\n\n"
        f"*Spending:*\n"
        f"Princess: ${princess_total:.2f}\n"
        f"Jay: ${jay_total:.2f}\n"
        f"Total: ${total_spent:.2f}\n\n"
        f"*Budget:*\n"
        f"Monthly: ${budget:.2f}\n"
        f"Remaining: ${remaining:.2f}\n"
        f"Used: {percent:.1f}%\n\n"
        f"*Settlement:*\n"
        f"{settle_line}\n\n"
        f"{status_icon} {status_text}"
    )


async def send_budget_alert() -> None:
    data = get_budget_data()
    if not data:
        return

    message = build_alert_message(
        princess_total=data["princess_total"],
        jay_total=data["jay_total"],
        budget=data["budget"],
        days_in_period=data["days_in_period"],
        princess_net=data["princess_net"],
    )

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    # Send to both users concurrently
    results = await asyncio.gather(
        send_with_retry(bot, data["princess_chat"], message),
        send_with_retry(bot, data["jay_chat"], message),
    )

    if all(results):
        logger.info("Budget alert delivered to both users")
    elif any(results):
        logger.warning("Budget alert delivered to only one user")
    else:
        logger.error("Budget alert failed for both users")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(send_budget_alert())
    except Exception as exc:
        logger.error("Unexpected error in budget_alert: %s", exc, exc_info=True)
        sys.exit(1)
