#!/usr/bin/env python3
"""
Expense Tracker Telegram Bot

Tracks shared expenses between Princess and Jay with custom split ratios,
period-based settlement, and budget alerts.

Security: only authorized chat IDs can use the bot once configured.
Performance: SQLite WAL mode, single context manager for multi-statement
             transactions, consolidated queries.
"""

import csv
import hashlib
import io
import logging
import math
import os
import re
import secrets as _secrets
import sqlite3
import tempfile
from contextlib import contextmanager
from collections import defaultdict
from datetime import datetime
from time import monotonic
from typing import Generator

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode

from config import (
    TELEGRAM_BOT_TOKEN, DATABASE_PATH,
    VALID_PERSONS, PRINCESS_CHAT_ID, JAY_CHAT_ID,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_DESCRIPTION_LEN = 200
MIN_AMOUNT = 0.01
MAX_AMOUNT = 99_999.99

# Simple in-memory sliding-window rate limiter per chat ID
RATE_LIMIT_WINDOW = 60    # seconds
RATE_LIMIT_MAX = 30       # requests per window
_rate_tracker: dict[int, list[float]] = {}

# Category picker options: (slug, emoji)
CATEGORIES = [
    ("groceries", "🛒"),
    ("dining",    "🍽"),
    ("transport", "⛽"),
    ("home",      "🏠"),
    ("fun",       "🎉"),
    ("health",    "💊"),
    ("travel",    "✈️"),
    ("recurring", "🔁"),
    ("other",     "📦"),
]

_CATEGORY_EMOJI: dict[str, str] = {slug: emoji for slug, emoji in CATEGORIES}

def _category_emoji(category: str | None) -> str:
    """Return the emoji for a category slug, or empty string if unknown/None."""
    if not category:
        return ""
    return _CATEGORY_EMOJI.get(category.lower(), "🏷")

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Apply performance and safety pragmas. WAL persists in the file."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """
    Yield a SQLite connection with WAL mode enabled.
    Commits on clean exit, rolls back on exception, always closes.
    Use this for multi-statement transactions (e.g. bulk inserts).
    """
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    try:
        _apply_pragmas(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def execute_query(query: str, params: tuple = (), *,
                  fetchone: bool = False, fetchall: bool = False):
    """Execute a read query and return results. Opens and closes its own connection."""
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        _apply_pragmas(conn)
        cursor = conn.execute(query, params)
        if fetchone:
            return cursor.fetchone()
        if fetchall:
            return cursor.fetchall()
        return None
    finally:
        conn.close()


def execute_write(query: str, params: tuple = ()) -> int:
    """Execute a single write query, commit, and return lastrowid."""
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        _apply_pragmas(conn)
        cursor = conn.execute(query, params)
        conn.commit()
        return cursor.lastrowid or 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_active_period_id() -> int | None:
    row = execute_query(
        "SELECT id FROM periods WHERE is_active = 1 LIMIT 1", fetchone=True
    )
    return row[0] if row else None


def build_category_keyboard(pre_selected: str | None = None) -> InlineKeyboardMarkup:
    """
    Build a 2-column InlineKeyboardMarkup for category selection.

    Rows:
      - 5 category rows covering all 9 CATEGORIES, callback_data=cat_pick_{slug}
      - 1 final row: [✏️ Custom (cat_custom), ❌ Cancel (cancel)]

    If pre_selected matches a slug, that button's label is prefixed with '✅ '.
    """
    rows = []
    for i in range(0, len(CATEGORIES), 2):
        row = []
        for slug, emoji in CATEGORIES[i:i + 2]:
            label = f"{emoji} {slug}"
            if pre_selected and slug == pre_selected:
                label = f"✅ {label}"
            row.append(InlineKeyboardButton(label, callback_data=f"cat_pick_{slug}"))
        rows.append(row)
    rows.append([
        InlineKeyboardButton("✏️ Custom", callback_data="cat_custom"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def group_recurring_rows(
    rows: list[tuple[str, float, str]],
) -> list[dict]:
    """
    Collapse raw recurring transaction rows into unique items by description.

    Input rows: list of (payer, amount, description) — expected newest-first.

    Returns a list of dicts (order preserved, first-seen order):
        {"description": str, "amount": float, "payer": str, "count": int}

    The amount and payer are taken from the *first* (most-recent) occurrence.
    """
    seen: dict[str, dict] = {}
    for payer, amount, description in rows:
        key = description.lower().strip()
        if key not in seen:
            seen[key] = {"description": description, "amount": amount,
                         "payer": payer, "count": 1}
        else:
            seen[key]["count"] += 1
    return list(seen.values())


def toggle_recurring_category(
    current: str | None,
    previous: str | None = None,
) -> tuple[str, str | None]:
    """
    Toggle a transaction's category between 'recurring' and its prior category.

    Returns (new_category, displaced_category):
    - If current != 'recurring'  → new='recurring', displaced=current
    - If current == 'recurring'  → new=previous (fallback 'other'), displaced='recurring'
    - If current is None         → treated as non-recurring, new='recurring', displaced=None
    """
    if current == "recurring":
        return (previous or "other", "recurring")
    return ("recurring", current)


# ---------------------------------------------------------------------------
# Recurring carry-over helpers  (post-reset prompt feature)
# ---------------------------------------------------------------------------

def normalize_recurring_description(description: str) -> str:
    """Stable grouping key: strip whitespace and lowercase."""
    return description.strip().lower()


def amount_to_cents(amount: float) -> int:
    """Convert a float amount to integer cents (rounded)."""
    return int(round(amount * 100))


def make_recurring_item_token(description: str, amount: float, person: str, payer: str) -> str:
    """
    Produce a short deterministic 12-hex-char token for a (description, amount, person, payer) tuple.
    Description is normalised before hashing so 'Spotify' and 'spotify' share a token for the same
    person/payer combination. Uses sha256 for collision resistance.
    """
    key = f"{normalize_recurring_description(description)}|{amount_to_cents(amount)}|{person}|{payer}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def build_recurring_carryover_item(row: tuple) -> dict:
    """
    Shape a DB row into a carry-over item dict.
    Expected row: (person, payer, amount, description, split_ratio)
    """
    person, payer, amount, description, split_ratio = row
    return {
        "token": make_recurring_item_token(description, amount, person, payer),
        "description": description,
        "normalized_description": normalize_recurring_description(description),
        "amount": amount,
        "amount_cents": amount_to_cents(amount),
        "person": person,
        "payer": payer,
        "split_ratio": split_ratio,
    }


def build_recurring_carryover_items(rows: list) -> list[dict]:
    """Map build_recurring_carryover_item over a list of DB rows."""
    return [build_recurring_carryover_item(r) for r in rows]


def toggle_selected_token(selected_tokens: set, token: str) -> set:
    """Return a new set with token added if missing, or removed if present."""
    new = set(selected_tokens)
    if token in new:
        new.discard(token)
    else:
        new.add(token)
    return new


def get_selected_recurring_items(
    items_by_token: dict,
    selected_tokens: set,
    ordered_tokens: list,
) -> list[dict]:
    """Return item dicts for selected tokens, preserving ordered_tokens display order."""
    return [
        items_by_token[tok]
        for tok in ordered_tokens
        if tok in selected_tokens and tok in items_by_token
    ]


def make_recurring_transaction_signature(item: dict) -> tuple:
    """Return a tuple that uniquely identifies a recurring transaction for idempotency."""
    return (
        item["normalized_description"],
        item["amount_cents"],
        item["person"],
        item["payer"],
        item["split_ratio"],
    )


def filter_new_recurring_items(
    items: list[dict],
    existing_signatures: set,
) -> list[dict]:
    """Return only items whose signature is not already in existing_signatures."""
    return [
        item for item in items
        if make_recurring_transaction_signature(item) not in existing_signatures
    ]


def parse_recurring_carryover_callback(data: str):
    """
    Parse a recurring carry-over callback_data string.

    Formats:
      rc:t:{session_id}:{token}  → ('toggle', session_id, token)
      rc:a:{session_id}          → ('add',    session_id, None)
      rc:s:{session_id}          → ('skip',   session_id, None)

    Returns None for unrecognised formats.
    """
    if not data.startswith("rc:"):
        return None
    parts = data.split(":", 3)
    if len(parts) < 3:
        return None
    action_code = parts[1]
    session_id = parts[2]
    if action_code == "t":
        if len(parts) < 4:
            return None
        return ("toggle", session_id, parts[3])
    if action_code == "a":
        return ("add", session_id, None)
    if action_code == "s":
        return ("skip", session_id, None)
    return None


def get_canonical_recurring_items_from_history() -> list[tuple]:
    """
    Return one canonical row per (normalised description, amount) pair across all periods.
    Row shape: (person, payer, amount, description, split_ratio)
    The most-recent occurrence (by timestamp DESC, id DESC) wins.
    """
    return execute_query(
        """
        WITH ranked AS (
            SELECT
                person,
                payer,
                ROUND(amount, 2)        AS amount,
                description,
                COALESCE(split_ratio, '50/50') AS split_ratio,
                LOWER(TRIM(description)) AS norm_desc,
                ROW_NUMBER() OVER (
                    PARTITION BY LOWER(TRIM(description)), ROUND(amount, 2)
                    ORDER BY timestamp DESC, id DESC
                ) AS rn
            FROM transactions
            WHERE category = 'recurring'
        )
        SELECT person, payer, amount, description, split_ratio
          FROM ranked
         WHERE rn = 1
         ORDER BY norm_desc ASC, amount ASC
        """,
        fetchall=True,
    ) or []


def get_existing_recurring_signatures_for_period(period_id: int) -> set:
    """
    Return a set of (norm_desc, cents, person, payer, split_ratio) for all
    recurring transactions already in the given period.
    Used for idempotency checking before inserting carry-over items.
    """
    rows = execute_query(
        """SELECT LOWER(TRIM(description)), ROUND(amount, 2), person, payer,
                  COALESCE(split_ratio, '50/50')
             FROM transactions
            WHERE period_id = ? AND category = 'recurring'
        """,
        (period_id,),
        fetchall=True,
    ) or []
    return {
        (norm_desc, amount_to_cents(float(amt)), person, payer, split_ratio)
        for norm_desc, amt, person, payer, split_ratio in rows
    }


def build_recurring_carryover_session(items: list[dict], period_id: int):
    """
    Build the user_data session dict for the carry-over prompt.
    Returns None if items is empty (no prompt should be shown).
    All items are pre-selected by default.
    """
    if not items:
        return None
    session_id = _secrets.token_hex(4)
    ordered_tokens = [item["token"] for item in items]
    items_by_token = {item["token"]: item for item in items}
    return {
        "session_id": session_id,
        "period_id": period_id,
        "ordered_tokens": ordered_tokens,
        "items_by_token": items_by_token,
        "selected_tokens": set(ordered_tokens),
        "completed": False,
    }


def build_recurring_carryover_text(items_by_token: dict, selected_tokens: set, ordered_tokens: list) -> str:
    """Render the carry-over prompt message text."""
    lines = ["🔁 **Recurring Expenses — Carry Over?**\n",
             "Tap items to toggle selection, then tap **Add Selected**.\n"]
    for tok in ordered_tokens:
        item = items_by_token[tok]
        check = "✅" if tok in selected_tokens else "⬜"
        lines.append(
            f"{check} {item['payer']} ${item['amount']:.2f} — {item['description']}"
        )
    return "\n".join(lines)


def build_recurring_carryover_keyboard(
    items_by_token: dict,
    selected_tokens: set,
    ordered_tokens: list,
    session_id: str,
) -> InlineKeyboardMarkup:
    """Build the InlineKeyboardMarkup for the carry-over prompt."""
    rows = []
    for tok in ordered_tokens:
        item = items_by_token[tok]
        check = "✅" if tok in selected_tokens else "⬜"
        label = f"{check} {item['payer']} ${item['amount']:.2f} — {item['description']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"rc:t:{session_id}:{tok}")])
    rows.append([
        InlineKeyboardButton("➕ Add Selected", callback_data=f"rc:a:{session_id}"),
        InlineKeyboardButton("⏭ Skip", callback_data=f"rc:s:{session_id}"),
    ])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Security / rate-limiting helpers
# ---------------------------------------------------------------------------

def is_rate_limited(chat_id: int) -> bool:
    """Sliding-window rate limiter. Returns True if the user should be throttled."""
    now = monotonic()
    window_start = now - RATE_LIMIT_WINDOW
    timestamps = _rate_tracker.setdefault(chat_id, [])
    _rate_tracker[chat_id] = [t for t in timestamps if t > window_start]
    if len(_rate_tracker[chat_id]) >= RATE_LIMIT_MAX:
        return True
    _rate_tracker[chat_id].append(now)
    return False


def get_authorized_chat_ids() -> tuple[str, str]:
    """Return (princess_id, jay_id) from DB, falling back to config values."""
    rows = execute_query(
        "SELECT key, value FROM settings WHERE key IN ('princess_chat_id', 'jay_chat_id')",
        fetchall=True,
    )
    stored = {r[0]: r[1] for r in rows} if rows else {}
    princess = stored.get("princess_chat_id") or str(PRINCESS_CHAT_ID)
    jay = stored.get("jay_chat_id") or str(JAY_CHAT_ID)
    return princess, jay


def is_authorized(chat_id: int) -> bool:
    princess_id, jay_id = get_authorized_chat_ids()
    # Fail closed: if neither ID is configured, deny everyone.
    # Chat IDs are written to DB at startup when users first message the bot,
    # and fall back to PRINCESS_CHAT_ID / JAY_CHAT_ID from the .env, so a
    # truly unconfigured state should not occur in production.
    if not princess_id and not jay_id:
        return False
    return str(chat_id) in (princess_id, jay_id)


async def _auth_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Gate for message-based handlers. Sends rejection and returns False
    if the user is rate-limited or unauthorized. Returns True on pass.
    """
    chat_id = update.effective_chat.id
    if is_rate_limited(chat_id):
        await update.message.reply_text("\u23f3 Too many requests \u2014 slow down.")
        return False
    if not is_authorized(chat_id):
        logger.warning("Unauthorized access attempt: chat_id=%s", chat_id)
        await update.message.reply_text("\u26d4 Unauthorized.")
        return False
    return True


async def _auth_check_callback(query) -> bool:
    """Gate for callback query handlers."""
    chat_id = query.message.chat.id if query.message else 0
    if is_rate_limited(chat_id):
        await query.answer("Too many requests.", show_alert=True)
        return False
    if not is_authorized(chat_id):
        logger.warning("Unauthorized callback: chat_id=%s", chat_id)
        await query.answer("Unauthorized.", show_alert=True)
        return False
    return True

# ---------------------------------------------------------------------------
# Expense parsing
# ---------------------------------------------------------------------------

def parse_split_ratio(split_text: str | None) -> tuple[float, float]:
    """Parse 'XX/YY' -> (princess_pct, jay_pct). Returns (50, 50) on any error."""
    if not split_text or split_text == "50/50":
        return (50.0, 50.0)
    try:
        parts = split_text.strip().split("/")
        if len(parts) == 2:
            p, j = float(parts[0]), float(parts[1])
            if p >= 0 and j >= 0 and abs((p + j) - 100) < 0.01:
                return (p, j)
    except (ValueError, IndexError):
        pass
    return (50.0, 50.0)


def calculate_split_amounts(amount: float, split_ratio: str | None) -> tuple[float, float]:
    """Return (princess_owes, jay_owes) for an amount with the given split."""
    p_pct, j_pct = parse_split_ratio(split_ratio)
    return (amount * p_pct / 100.0, amount * j_pct / 100.0)


def parse_expense(text: str, default_split: str = "50/50") -> dict | None:
    """
    Parse expense input. Supports:
      Single (amount-second):  'Princess 50 groceries'
                               'Princess 100 dinner -split 60/40'
                               'Princess 50 groceries #food'
      Single (amount-last):    'Princess groceries 50'
                               'Jay ice cream 54.33 -split 60/40'
                               'Jay lunch 30 #dining'
      Bulk:    'Princess\\n- 50 groceries\\n- 25 coffee\\n\\nJay\\n- 30 lunch'

    A #tag anywhere in the message is extracted as the category (lowercased)
    and removed from the description.

    Returns None if the text does not match any valid format.
    Enforces MIN_AMOUNT, MAX_AMOUNT, and MAX_DESCRIPTION_LEN.
    Uses default_split when no -split override is provided.
    """
    text = text.strip()

    def _extract_category(s: str) -> tuple[str, str | None]:
        """Return (cleaned_text, category_or_None) stripping first #tag."""
        tag_match = re.search(r"#(\w+)", s)
        if tag_match:
            category = tag_match.group(1).lower()
            cleaned = re.sub(r"#\w+", "", s, count=1).strip()
            # Collapse multiple spaces left by removal
            cleaned = re.sub(r" {2,}", " ", cleaned)
            return cleaned, category
        return s, None

    # ---- Bulk (multi-line) format ----
    if "\n" in text:
        lines = text.split("\n")
        expenses = []
        current_person = None

        for line in lines:
            line = line.strip()
            if not line:
                continue
            person_match = re.match(r"^(Princess|Jay)$", line, re.IGNORECASE)
            if person_match:
                current_person = person_match.group(1).capitalize()
                continue
            exp_match = re.match(
                r"^\s*-\s*\$?\s*(\d+\.?\d*)\s+(.+?)\s*(?:-split\s+(\d+)/(\d+))?\s*$",
                line,
            )
            if exp_match and current_person:
                amt = float(exp_match.group(1))
                raw_desc = exp_match.group(2).strip()
                split = (
                    f"{exp_match.group(3)}/{exp_match.group(4)}"
                    if exp_match.group(3) else default_split
                )
                raw_desc, category = _extract_category(raw_desc)
                if MIN_AMOUNT <= amt <= MAX_AMOUNT:
                    expenses.append({
                        "person": current_person,
                        "amount": amt,
                        "description": raw_desc[:MAX_DESCRIPTION_LEN],
                        "split_ratio": split,
                        "category": category,
                    })

        if expenses:
            return {"bulk": True, "expenses": expenses}
        return None

    # Strip #tag from the whole text before running single-line regexes
    # so that 'Jay lunch 30.00 #dining' parses correctly as amount-last.
    text_no_tag, category = _extract_category(text)

    # ---- Single-line: try amount-second format first ----
    # Pattern: Name Amount Description [-split XX/YY]
    pattern_amount_second = r"^\s*(Princess|Jay)\s+(\d+\.?\d*)\s+(.+?)\s*(?:-split\s+(\d+)/(\d+))?\s*$"
    match = re.match(pattern_amount_second, text_no_tag, re.IGNORECASE)
    if match:
        person = match.group(1).capitalize()
        amount = float(match.group(2))
        raw_desc = match.group(3).strip()
        split = (
            f"{match.group(4)}/{match.group(5)}"
            if match.group(4) else default_split
        )
        # Re-extract in case description had its own tag (edge case)
        raw_desc, cat2 = _extract_category(raw_desc)
        final_category = category or cat2
        description = raw_desc[:MAX_DESCRIPTION_LEN]
        if person in VALID_PERSONS and MIN_AMOUNT <= amount <= MAX_AMOUNT:
            return {
                "bulk": False,
                "person": person,
                "amount": amount,
                "description": description,
                "split_ratio": split,
                "category": final_category,
            }

    # ---- Single-line: try amount-last format ----
    # Pattern: Name Description Amount [-split XX/YY]
    # The amount is the last numeric token before the optional -split suffix.
    pattern_amount_last = r"^\s*(Princess|Jay)\s+(.+?)\s+(\d+\.?\d*)\s*(?:-split\s+(\d+)/(\d+))?\s*$"
    match = re.match(pattern_amount_last, text_no_tag, re.IGNORECASE)
    if match:
        person = match.group(1).capitalize()
        raw_desc = match.group(2).strip()
        amount = float(match.group(3))
        split = (
            f"{match.group(4)}/{match.group(5)}"
            if match.group(4) else default_split
        )
        raw_desc, cat2 = _extract_category(raw_desc)
        final_category = category or cat2
        description = raw_desc[:MAX_DESCRIPTION_LEN]
        if person in VALID_PERSONS and MIN_AMOUNT <= amount <= MAX_AMOUNT:
            return {
                "bulk": False,
                "person": person,
                "amount": amount,
                "description": description,
                "split_ratio": split,
                "category": final_category,
            }

    return None

# ---------------------------------------------------------------------------
# Settlement calculation
# ---------------------------------------------------------------------------

def calculate_period_settlement() -> dict:
    """
    Calculate settlement for the active period honouring custom split ratios.

    Returns a dict with:
      princess_paid / jay_paid      -- what each person actually paid out
      princess_owes / jay_owes      -- what each person owes per split ratios
      total_spent                   -- combined total paid
      princess_net                  -- princess_paid - princess_owes
                                       > 0 -> Jay owes Princess
                                       < 0 -> Princess owes Jay
    """
    rows = execute_query(
        """SELECT payer, amount, split_ratio
             FROM transactions
            WHERE period_id = (SELECT id FROM periods WHERE is_active = 1)""",
        fetchall=True,
    ) or []

    princess_paid = sum(r[1] for r in rows if r[0] == "Princess")
    jay_paid = sum(r[1] for r in rows if r[0] == "Jay")
    princess_owes_total = 0.0
    jay_owes_total = 0.0

    for _, amount, split_ratio in rows:
        p_owes, j_owes = calculate_split_amounts(amount, split_ratio or "50/50")
        princess_owes_total += p_owes
        jay_owes_total += j_owes

    princess_net = princess_paid - princess_owes_total
    return {
        "princess_paid": princess_paid,
        "jay_paid": jay_paid,
        "princess_owes": princess_owes_total,
        "jay_owes": jay_owes_total,
        "total_spent": princess_paid + jay_paid,
        "princess_net": princess_net,
        "jay_net": jay_paid - jay_owes_total,
    }


def settlement_line(princess_net: float) -> str:
    """Return a one-line human-readable settlement string."""
    if abs(princess_net) < 0.01:
        return "\u2705 All settled up!"
    if princess_net > 0:
        return f"\U0001f4b8 Jay owes Princess: ${abs(princess_net):.2f}"
    return f"\U0001f4b8 Princess owes Jay: ${abs(princess_net):.2f}"


def build_confirmation_footer(settlement: dict) -> str:
    """
    Return a short 2-line footer appended to expense confirmations.

    Shows running period total and current settlement so users never
    need to run /status just to see where things stand.
    """
    total = settlement["total_spent"]
    settle = settlement_line(settlement["princess_net"])
    return f"\U0001f4ca Period total: ${total:.2f}  |  {settle}"


def budget_bar(spent: float, budget: float, days_left: int) -> str:
    """
    Return a text-art budget progress bar.

    Example:
      [████████░░░░░░░░░░░░] 40% of $600 — $360 left, 18 days
    """
    if budget <= 0:
        return f"Budget: ${spent:.2f} spent (no budget set)"
    percent = min(spent / budget * 100, 999.9)
    filled = min(int(percent / 5), 20)   # 20-cell bar, each cell = 5%
    empty = 20 - filled
    bar = "█" * filled + "░" * empty
    remaining = budget - spent
    remaining_str = f"${remaining:.2f} left" if remaining >= 0 else f"${abs(remaining):.2f} over"
    days_str = f", {days_left} day{'s' if days_left != 1 else ''} left" if days_left > 0 else ""
    return f"[{bar}] {percent:.1f}% of ${budget:.0f} — {remaining_str}{days_str}"


def build_summary_message(rows: list, budget: float) -> str:
    """
    Build a /summary report string.

    rows: list of (payer, amount, category) tuples for the active period.
    budget: monthly budget as float.

    Returns a human-readable Markdown string.
    """
    if not rows:
        return "\U0001f4ca *Period Summary*\n\nNo transactions yet."

    princess_total = sum(amt for payer, amt, _ in rows if payer == "Princess")
    jay_total = sum(amt for payer, amt, _ in rows if payer == "Jay")
    grand_total = princess_total + jay_total

    princess_pct = princess_total / grand_total * 100 if grand_total > 0 else 0
    jay_pct = jay_total / grand_total * 100 if grand_total > 0 else 0
    budget_pct = grand_total / budget * 100 if budget > 0 else 0

    # Category breakdown
    cat_totals: dict[str, float] = {}
    for _payer, amt, cat in rows:
        label = cat if cat else "uncategorized"
        cat_totals[label] = cat_totals.get(label, 0.0) + amt

    msg = (
        "\U0001f4ca *Period Summary*\n\n"
        f"*Who paid:*\n"
        f"Princess: ${princess_total:.2f} ({princess_pct:.0f}%)\n"
        f"Jay: ${jay_total:.2f} ({jay_pct:.0f}%)\n"
        f"Total: ${grand_total:.2f}\n\n"
        f"*Budget:* ${grand_total:.2f} / ${budget:.0f} ({budget_pct:.1f}%)\n"
    )

    if cat_totals:
        msg += "\n*By category:*\n"
        for cat, total in sorted(cat_totals.items(), key=lambda x: -x[1]):
            cat_pct = total / grand_total * 100 if grand_total > 0 else 0
            msg += f"\u2022 {cat}: ${total:.2f} ({cat_pct:.0f}%)\n"

    return msg


def find_transactions(query: str) -> list:
    """
    Search transactions in the active period by description (case-insensitive).

    Returns list of (id, payer, description, amount, split_ratio, category) tuples.
    """
    rows = execute_query(
        """SELECT id, payer, description, amount, split_ratio, category
             FROM transactions
            WHERE period_id = (SELECT id FROM periods WHERE is_active = 1)
              AND description LIKE ?
            ORDER BY id DESC""",
        (f"%{query}%",),
        fetchall=True,
    )
    return rows or []

# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    # Rate-limit /start even though it doesn't require authorization
    if is_rate_limited(chat_id):
        await update.message.reply_text("\u23f3 Too many requests \u2014 slow down.")
        return
    princess_id, jay_id = get_authorized_chat_ids()

    # Auto-register if chat ID matches a configured env var and DB slot is empty
    if str(chat_id) == str(PRINCESS_CHAT_ID) and not princess_id:
        execute_write(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("princess_chat_id", str(chat_id)),
        )
        logger.info("Auto-registered Princess: chat_id=%s", chat_id)
    elif str(chat_id) == str(JAY_CHAT_ID) and not jay_id:
        execute_write(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("jay_chat_id", str(chat_id)),
        )
        logger.info("Auto-registered Jay: chat_id=%s", chat_id)

    if not princess_id or not jay_id:
        await update.message.reply_text(
            "\U0001f44b **Welcome to Expense Tracker!**\n\n"
            "Track shared expenses between Princess and Jay.\n\n"
            "**Add expenses like this:**\n"
            "`Princess 50.00 groceries`\n"
            "`Jay 25.50 coffee`\n"
            "`Princess 100 dinner -split 60/40`\n\n"
            "**Commands:**\n"
            "/status \u2014 Current totals & settlement\n"
            "/history \u2014 Recent transactions\n"
            "/periods \u2014 Period history\n"
            "/edit \u2014 Edit a transaction\n"
            "/undo \u2014 Remove last transaction\n"
            "/reset \u2014 Close period & settle up\n"
            "/export \u2014 Download CSV\n"
            "/setbudget \u2014 Change monthly budget\n"
            "/setreminder \u2014 Change reminder frequency\n"
            "/setsplit \u2014 Change default split ratio\n\n"
            f"Your chat ID: `{chat_id}`\n"
            "_Ask the admin to register your chat ID._",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            "\U0001f44b **Welcome back!**\n\n"
            "`Princess 50.00 groceries` \u2014 add an expense\n"
            "/status \u2014 view totals",
            parse_mode=ParseMode.MARKDOWN,
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_check(update, context):
        return

    s = calculate_period_settlement()
    settle_msg = settlement_line(s["princess_net"])

    # Feature 4: fetch budget and period start for progress bar
    budget_row = execute_query(
        "SELECT value FROM settings WHERE key = 'monthly_budget'", fetchone=True
    )
    budget = float(budget_row[0]) if budget_row else 0.0

    period_row = execute_query(
        "SELECT start_date FROM periods WHERE is_active = 1", fetchone=True
    )
    days_left = 0
    if period_row:
        try:
            period_start = datetime.fromisoformat(period_row[0])
            days_left = max(0, 30 - (datetime.utcnow() - period_start).days)
        except (ValueError, TypeError):
            days_left = 0

    rows = execute_query(
        """SELECT payer, amount, description, split_ratio, category,
                  datetime(timestamp, 'localtime') AS ts
             FROM transactions
            WHERE period_id = (SELECT id FROM periods WHERE is_active = 1)
            ORDER BY payer, timestamp ASC""",
        fetchall=True,
    ) or []

    msg = (
        "\U0001f4ca **Current Period Status**\n\n"
        "**Paid:**\n"
        f"Princess: ${s['princess_paid']:.2f}\n"
        f"Jay: ${s['jay_paid']:.2f}\n\n"
        "**Owes (based on splits):**\n"
        f"Princess owes: ${s['princess_owes']:.2f}\n"
        f"Jay owes: ${s['jay_owes']:.2f}\n\n"
        f"**Total Spent:** ${s['total_spent']:.2f}\n\n"
    )

    if budget > 0:
        bar_line = budget_bar(s["total_spent"], budget, days_left)
        msg += f"{bar_line}\n\n"

    msg += f"{settle_msg}\n"

    if rows:
        grouped: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for payer, amount, description, split_ratio, category, ts in rows:
            dt = datetime.fromisoformat(ts)
            date_str = dt.strftime("%A, %b %d, %Y")
            grouped[payer][date_str].append({
                "amount": amount,
                "description": description,
                "split_ratio": split_ratio,
                "category": category,
            })

        msg += "\n**Itemized Breakdown:**\n"
        for person_name in ("Princess", "Jay"):
            if person_name not in grouped:
                continue
            msg += f"\n**{person_name} paid:**\n"
            for date_str in sorted(
                grouped[person_name],
                key=lambda d: datetime.strptime(d, "%A, %b %d, %Y"),
            ):
                msg += f"\n**{date_str}:**\n"
                for item in grouped[person_name][date_str]:
                    split_note = (
                        f" (split {item['split_ratio']})"
                        if item["split_ratio"] not in (None, "50/50") else ""
                    )
                    cat_tag = _category_emoji(item["category"])
                    cat_str = f" {cat_tag}" if cat_tag else ""
                    msg += f"\u2022 ${item['amount']:.2f} \u2014 {item['description']}{split_note}{cat_str}\n"
    else:
        msg += "\n_No transactions yet._"

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_check(update, context):
        return
    rows = execute_query(
        """SELECT payer, amount, category FROM transactions
            WHERE period_id = (SELECT id FROM periods WHERE is_active = 1)""",
        fetchall=True,
    ) or []
    budget_row = execute_query(
        "SELECT value FROM settings WHERE key = 'monthly_budget'", fetchone=True
    )
    budget = float(budget_row[0]) if budget_row else 0.0
    msg = build_summary_message(rows, budget)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_check(update, context):
        return

    rows = execute_query(
        """SELECT payer, amount, description, split_ratio, category,
                  datetime(timestamp, 'localtime') AS ts
             FROM transactions
            WHERE period_id = (SELECT id FROM periods WHERE is_active = 1)
            ORDER BY timestamp DESC
            LIMIT 15""",
        fetchall=True,
    ) or []

    if not rows:
        await update.message.reply_text(
            "\U0001f4cb **Recent Transactions**\n\nNo transactions yet.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    msg = "\U0001f4cb **Recent Transactions**\n\n"
    for i, (payer, amount, description, split_ratio, category, ts) in enumerate(rows, 1):
        split_note = (
            f" (split {split_ratio})" if split_ratio and split_ratio != "50/50" else ""
        )
        cat_tag = _category_emoji(category)
        cat_str = f" {cat_tag}" if cat_tag else ""
        msg += (
            f"{i}. **{payer} paid** \u2014 ${amount:.2f}{split_note}{cat_str}\n"
            f"   {description}\n"
            f"   _{ts}_\n\n"
        )

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_check(update, context):
        return

    row = execute_query(
        """SELECT id, person, amount, description
             FROM transactions
            WHERE period_id = (SELECT id FROM periods WHERE is_active = 1)
            ORDER BY timestamp DESC
            LIMIT 1""",
        fetchone=True,
    )
    if not row:
        await update.message.reply_text("\u274c No transactions to undo.")
        return

    trans_id, person, amount, description = row
    execute_write("DELETE FROM transactions WHERE id = ?", (trans_id,))
    logger.info("Instant undo: deleted transaction id=%s", trans_id)
    footer = build_confirmation_footer(calculate_period_settlement())
    await update.message.reply_text(
        f"\u2705 Removed: **{person}** ${amount:.2f} \u2014 {description}\n\n{footer}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_check(update, context):
        return

    # Use split-aware settlement (the old code used naive 50/50 here -- fixed)
    s = calculate_period_settlement()
    settle_msg = settlement_line(s["princess_net"])

    keyboard = [[
        InlineKeyboardButton("\u2705 Yes, Reset", callback_data="reset_confirm"),
        InlineKeyboardButton("\u274c Cancel", callback_data="cancel"),
    ]]
    msg = (
        "\U0001f504 **Reset Period?**\n\n"
        "**Current Totals:**\n"
        f"Princess paid: ${s['princess_paid']:.2f}\n"
        f"Jay paid: ${s['jay_paid']:.2f}\n"
        f"Total: ${s['total_spent']:.2f}\n\n"
        f"{settle_msg}\n\n"
        "This closes the current period and starts fresh. Continue?"
    )
    await update.message.reply_text(
        msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN
    )


def generate_csv_content(rows: list) -> str:
    """Generate CSV string from transaction rows.

    rows: list of (id, payer, amount, description, timestamp, period_id, split_ratio, category)
    Returns CSV string with header.
    Column order: ID, Date, Payer, Amount, Description, Category, Split Ratio, Period ID
    """
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    writer.writerow(["ID", "Date", "Payer", "Amount", "Description", "Category", "Split Ratio", "Period ID"])
    for row_id, payer, amount, desc, ts, period_id, split_ratio, category in rows:
        writer.writerow([
            row_id, ts, payer, f"{amount:.2f}", desc or "",
            category or "", split_ratio or "50/50", period_id,
        ])
    return output.getvalue()


def save_period_csv(period_id: int, rows: list) -> str:
    """Save CSV for a completed period to data/exports/. Returns path to saved file."""
    exports_dir = os.path.join(os.path.dirname(DATABASE_PATH), "exports")
    os.makedirs(exports_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"period_{period_id}_{timestamp}.csv"
    path = os.path.join(exports_dir, filename)
    content = generate_csv_content(rows)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info("Saved period CSV: %s", path)
    return path


async def export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_check(update, context):
        return

    exports_dir = os.path.join(os.path.dirname(DATABASE_PATH), "exports")
    os.makedirs(exports_dir, exist_ok=True)
    saved_files = sorted(
        [f for f in os.listdir(exports_dir) if f.endswith(".csv")],
        reverse=True,  # newest first
    )

    # Always offer current period as first option
    keyboard = [[InlineKeyboardButton(
        "\U0001f4ca Current Period", callback_data="export_current"
    )]]

    # Add saved period exports (up to 10)
    for fname in saved_files[:10]:
        parts = fname.replace(".csv", "").split("_")
        if len(parts) >= 3:
            label = f"\U0001f4c1 Period {parts[1]} \u2014 {parts[2][:8]}"
        else:
            label = f"\U0001f4c1 {fname}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"export_file_{fname}")])

    keyboard.append([InlineKeyboardButton("\u274c Cancel", callback_data="cancel")])

    await update.message.reply_text(
        "\U0001f4ca **Export Transactions**\n\nSelect which period to export:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
    )


async def setbudget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_check(update, context):
        return

    if not context.args:
        row = execute_query(
            "SELECT value FROM settings WHERE key = 'monthly_budget'", fetchone=True
        )
        current = float(row[0]) if row else 600.0
        await update.message.reply_text(
            "\U0001f4b0 **Budget Configuration**\n\n"
            f"Current budget: **${current:.2f}**\n\n"
            "Usage: `/setbudget 800`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        amount = float(context.args[0])
        if not 0.01 <= amount <= 1_000_000:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "\u274c Invalid amount. Example: `/setbudget 800`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    keyboard = [[
        InlineKeyboardButton(
            "\u2705 Confirm", callback_data=f"setbudget_confirm_{amount:.2f}"
        ),
        InlineKeyboardButton("\u274c Cancel", callback_data="cancel"),
    ]]
    await update.message.reply_text(
        f"\U0001f4b0 Set monthly budget to **${amount:.2f}**?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
    )


async def setreminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_check(update, context):
        return

    if not context.args:
        row = execute_query(
            "SELECT value FROM settings WHERE key = 'reminder_days'", fetchone=True
        )
        current = int(row[0]) if row else 3
        await update.message.reply_text(
            "\U0001f514 **Reminder Configuration**\n\n"
            f"Current frequency: every **{current} days**\n\n"
            "Usage: `/setreminder 7`\n"
            "Note: update the cron job after changing \u2014 see `/reminder_help`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        days = int(context.args[0])
        if not 1 <= days <= 30:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "\u274c Must be 1\u201330. Example: `/setreminder 7`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    keyboard = [[
        InlineKeyboardButton(
            "\u2705 Confirm", callback_data=f"setreminder_confirm_{days}"
        ),
        InlineKeyboardButton("\u274c Cancel", callback_data="cancel"),
    ]]
    await update.message.reply_text(
        f"\U0001f514 Set reminder to every **{days} days**?\n\n"
        "\u26a0\ufe0f Remember to update the cron job \u2014 see `/reminder_help`.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
    )


async def reminder_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_check(update, context):
        return

    row = execute_query(
        "SELECT value FROM settings WHERE key = 'reminder_days'", fetchone=True
    )
    days = int(row[0]) if row else 3
    await update.message.reply_text(
        "\U0001f527 **Cron Job Update Instructions**\n\n"
        f"Current frequency: every **{days} days**\n\n"
        "**On your device, run:**\n"
        "```\ncrontab -e```\n\n"
        "**Replace the budget alert line with:**\n"
        f"```\n0 9 */{days} * * /opt/telegram-bots/expense-tracker/venv/bin/python3 "
        "/opt/telegram-bots/expense-tracker/budget_alert.py >> "
        "/var/log/expense-tracker-budget.log 2>&1```\n\n"
        "**Common schedules:**\n"
        "\u2022 Daily: `0 9 * * *`\n"
        "\u2022 Weekly (Mon): `0 9 * * 1`\n"
        f"\u2022 Every {days} days: `0 9 */{days} * *`\n\n"
        "Verify with: `crontab -l`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def setsplit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_check(update, context):
        return

    if not context.args:
        row = execute_query(
            "SELECT value FROM settings WHERE key = 'default_split'", fetchone=True
        )
        current = row[0] if row else "50/50"
        p_pct, j_pct = parse_split_ratio(current)
        await update.message.reply_text(
            "\u2696\ufe0f **Default Split Configuration**\n\n"
            f"Current default: **{current}** "
            f"(Princess {p_pct:.4g}% / Jay {j_pct:.4g}%)\n\n"
            "Usage: `/setsplit Princess%/Jay%`\n\n"
            "**Examples:**\n"
            "\u2022 `/setsplit 50/50` \u2014 even split\n"
            "\u2022 `/setsplit 60/40` \u2014 Princess owes 60%, Jay owes 40%\n"
            "\u2022 `/setsplit 0/100` \u2014 Jay pays for everything\n"
            "\u2022 `/setsplit 100/0` \u2014 Princess pays for everything\n\n"
            "_Override per-expense any time with_ `-split XX/YY`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    raw = context.args[0]
    parts = raw.split("/")
    try:
        if len(parts) != 2:
            raise ValueError
        p_pct = float(parts[0])
        j_pct = float(parts[1])
        if p_pct < 0 or j_pct < 0 or abs((p_pct + j_pct) - 100) > 0.01:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "\u274c Invalid format. Percentages must sum to 100.\n\n"
            "Format: `/setsplit Princess%/Jay%`\n"
            "Examples: `/setsplit 50/50`, `/setsplit 60/40`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    split_str = f"{parts[0].strip()}/{parts[1].strip()}"
    keyboard = [[
        InlineKeyboardButton(
            "\u2705 Confirm", callback_data=f"setsplit_confirm_{split_str}"
        ),
        InlineKeyboardButton("\u274c Cancel", callback_data="cancel"),
    ]]
    await update.message.reply_text(
        f"\u2696\ufe0f Set default split to **{split_str}**?\n\n"
        f"\u2022 Princess owes **{p_pct:.4g}%** of each expense\n"
        f"\u2022 Jay owes **{j_pct:.4g}%** of each expense\n\n"
        "_Override per-expense any time with_ `-split XX/YY`",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
    )


async def periods_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_check(update, context):
        return

    rows = execute_query(
        """SELECT id,
                  datetime(start_date, 'localtime'),
                  datetime(end_date, 'localtime'),
                  is_active, princess_total, jay_total, settlement_description
             FROM periods
            ORDER BY start_date DESC
            LIMIT 10""",
        fetchall=True,
    ) or []

    if not rows:
        await update.message.reply_text("\u274c No period history found.")
        return

    msg = "\U0001f4c5 **Period History** (last 10)\n\n"
    for period_id, start, end, is_active, p_total, j_total, settle in rows:
        p_total = p_total or 0.0
        j_total = j_total or 0.0
        if is_active:
            msg += (
                f"**\U0001f7e2 Current Period** (#{period_id})\n"
                f"Started: {start}\n_Active_\n\n"
            )
        else:
            msg += (
                f"**Period #{period_id}**\n"
                f"\U0001f4c5 {start} \u2192 {end}\n"
                f"Princess: ${p_total:.2f} | Jay: ${j_total:.2f}\n"
                f"Total: ${p_total + j_total:.2f}\n"
            )
            if settle:
                msg += f"\U0001f4b8 _{settle}_\n"
            msg += "\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_check(update, context):
        return

    rows = execute_query(
        """SELECT id, person, amount, description,
                  datetime(timestamp, 'localtime')
             FROM transactions
            WHERE period_id = (SELECT id FROM periods WHERE is_active = 1)
            ORDER BY timestamp DESC
            LIMIT 10""",
        fetchall=True,
    ) or []

    if not rows:
        await update.message.reply_text("\u274c No transactions to edit.")
        return

    msg = "\u270f\ufe0f **Select a transaction to edit:**\n\n"
    keyboard = []
    for i, (trans_id, person, amount, description, ts) in enumerate(rows, 1):
        msg += f"{i}. **{person}** \u2014 ${amount:.2f}\n   {description}\n   _{ts}_\n\n"
        keyboard.append([InlineKeyboardButton(
            f"{i}. {person} \u2014 ${amount:.2f}",
            callback_data=f"edit_select_{trans_id}",
        )])

    keyboard.append([InlineKeyboardButton("\u274c Cancel", callback_data="cancel")])
    await update.message.reply_text(
        msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN
    )

# ---------------------------------------------------------------------------
# Message handler (expense entry + in-place editing state)
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_check(update, context):
        return

    text = update.message.text

    # If we're mid-edit, route to the edit handler instead of parsing as expense
    if context.user_data.get("editing_transaction_id"):
        await _process_edit(update, context, text)
        return

    # ---- Custom-category reply ----
    if context.user_data.get("awaiting_custom_category"):
        context.user_data.pop("awaiting_custom_category")
        category = text.strip().lower()[:50]
        p = context.user_data.pop("pending_single", None)
        if not p:
            await update.message.reply_text("❌ Session expired. Please try again.")
            return
        period_id = get_active_period_id()
        if period_id is None:
            await update.message.reply_text(
                "❌ No active period. Run /start or contact admin."
            )
            return
        execute_write(
            "INSERT INTO transactions "
            "(person, amount, description, period_id, payer, split_ratio, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (p["person"], p["amount"], p["description"], period_id,
             p["person"], p["split_ratio"], category),
        )
        logger.info(
            "Saved (custom cat): %s $%.2f %s [%s]",
            p["person"], p["amount"], p["description"], category,
        )
        footer = build_confirmation_footer(calculate_period_settlement())
        await update.message.reply_text(
            f"✅ Saved: **{p['person']}** ${p['amount']:.2f} — "
            f"{p['description']} [{category}]\n\n{footer}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    split_row = execute_query(
        "SELECT value FROM settings WHERE key = 'default_split'", fetchone=True
    )
    default_split = split_row[0] if split_row else "50/50"

    parsed = parse_expense(text, default_split=default_split)
    if not parsed:
        await update.message.reply_text(
            "\u274c **Format not recognized**\n\n"
            "Use: `Name Amount Description`\n\n"
            "**Examples:**\n"
            "\u2022 `Princess 50 groceries`\n"
            "\u2022 `Jay 25.50 coffee`\n"
            "\u2022 `Princess 100 dinner -split 60/40`\n\n"
            "**Bulk format:**\n"
            "```\nPrincess\n- 50 groceries\n- 25 coffee\n\nJay\n- 30 lunch```",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if parsed.get("bulk"):
        expenses = parsed["expenses"]
        context.user_data["pending_bulk"] = expenses

        total_by_person: dict[str, float] = defaultdict(float)
        lines = [f"\U0001f4dd **Confirm {len(expenses)} expense(s):**\n"]
        for i, e in enumerate(expenses, 1):
            split_note = (
                f" (split {e['split_ratio']})" if e["split_ratio"] != "50/50" else ""
            )
            lines.append(
                f"{i}. **{e['person']}** ${e['amount']:.2f} \u2014 {e['description']}{split_note}"
            )
            total_by_person[e["person"]] += e["amount"]

        lines.append("\n**Totals:**")
        for name, total in total_by_person.items():
            lines.append(f"{name}: ${total:.2f}")
        lines.append("\nAdd all?")

        keyboard = [[
            InlineKeyboardButton("\u2705 Add All", callback_data="conf_bulk"),
            InlineKeyboardButton("\u274c Cancel", callback_data="cancel"),
        ]]
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        context.user_data["pending_single"] = parsed
        split_note = (
            f"\n\U0001f4b1 Split: {parsed['split_ratio']}"
            if parsed["split_ratio"] != "50/50" else ""
        )
        # Fast-path: #recurring tag was explicit — skip the picker and save immediately
        if parsed.get("category") == "recurring":
            period_id = get_active_period_id()
            if period_id is None:
                await update.message.reply_text(
                    "\u274c No active period. Run /start or contact admin."
                )
                return
            context.user_data.pop("pending_single", None)
            execute_write(
                "INSERT INTO transactions "
                "(person, amount, description, period_id, payer, split_ratio, category) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (parsed["person"], parsed["amount"], parsed["description"], period_id,
                 parsed["person"], parsed["split_ratio"], "recurring"),
            )
            logger.info(
                "Auto-saved recurring: %s $%.2f %s",
                parsed["person"], parsed["amount"], parsed["description"],
            )
            footer = build_confirmation_footer(calculate_period_settlement())
            await update.message.reply_text(
                f"\u2705 Saved as recurring: **{parsed['person']}** "
                f"${parsed['amount']:.2f} \u2014 {parsed['description']} \U0001f501\n\n{footer}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        await update.message.reply_text(
            f"Confirm: **{parsed['person']}** paid **${parsed['amount']:.2f}** "
            f"for **{parsed['description']}**{split_note}\n"
            "Pick a category to save:",
            reply_markup=build_category_keyboard(pre_selected=parsed.get("category")),
            parse_mode=ParseMode.MARKDOWN,
        )


async def _process_edit(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    """Handle free-text input when the user is in an active edit flow."""
    transaction_id = context.user_data["editing_transaction_id"]
    field = context.user_data["editing_field"]

    if field == "amount":
        try:
            new_amount = float(text.replace("$", "").strip())
            if not MIN_AMOUNT <= new_amount <= MAX_AMOUNT:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                f"\u274c Enter a number between ${MIN_AMOUNT} and ${MAX_AMOUNT:.0f}:"
            )
            return
        execute_write(
            "UPDATE transactions SET amount = ? WHERE id = ?",
            (new_amount, transaction_id),
        )
        await update.message.reply_text(
            f"\u2705 Amount updated to **${new_amount:.2f}**.", parse_mode=ParseMode.MARKDOWN
        )

    elif field == "description":
        new_desc = text.strip()[:MAX_DESCRIPTION_LEN]
        if not new_desc:
            await update.message.reply_text("\u274c Description cannot be empty.")
            return
        execute_write(
            "UPDATE transactions SET description = ? WHERE id = ?",
            (new_desc, transaction_id),
        )
        await update.message.reply_text(
            f"\u2705 Description updated: **{new_desc}**.", parse_mode=ParseMode.MARKDOWN
        )

    context.user_data.pop("editing_transaction_id", None)
    context.user_data.pop("editing_field", None)

# ---------------------------------------------------------------------------
# Recurring carry-over prompt  (sent after a period reset)
# ---------------------------------------------------------------------------

async def send_recurring_carryover_prompt(
    query, context: ContextTypes.DEFAULT_TYPE, new_period_id: int
) -> None:
    """
    Send a follow-up message after a period reset asking which recurring items to carry over.
    Builds the session in context.user_data and sends the interactive prompt.
    If no recurring history exists, sends a brief informational message instead.
    """
    rows = get_canonical_recurring_items_from_history()
    if not rows:
        await query.message.reply_text(
            "🔁 No recurring items in history — nothing to carry over.",
        )
        return
    items = build_recurring_carryover_items(rows)
    session = build_recurring_carryover_session(items, period_id=new_period_id)
    if session is None:
        return
    context.user_data["recurring_carryover_session"] = session
    text = build_recurring_carryover_text(
        session["items_by_token"],
        session["selected_tokens"],
        session["ordered_tokens"],
    )
    keyboard = build_recurring_carryover_keyboard(
        session["items_by_token"],
        session["selected_tokens"],
        session["ordered_tokens"],
        session["session_id"],
    )
    await query.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Callback query handler
# ---------------------------------------------------------------------------

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not await _auth_check_callback(query):
        return

    data = query.data
    period_id = get_active_period_id()

    # ---- Cancel ----
    if data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("\u274c Cancelled.")
        return

    # ---- Category pick ----
    if data.startswith("cat_pick_"):
        category = data.removeprefix("cat_pick_")
        # Check if this is an edit-category flow
        if context.user_data.get("editing_field") == "category":
            trans_id = context.user_data.pop("editing_transaction_id", None)
            context.user_data.pop("editing_field", None)
            if not trans_id:
                await query.edit_message_text("\u274c Session expired.")
                return
            execute_write(
                "UPDATE transactions SET category = ? WHERE id = ?", (category, trans_id)
            )
            # Clear any stale prev_cat that was set by a prior edit_toggle_recurring_ action,
            # so an un-toggle after this direct category change doesn't restore a stale value.
            context.user_data.pop(f"prev_cat_{trans_id}", None)
            logger.info("Category updated: id=%s → %s", trans_id, category)
            await query.edit_message_text(
                f"\u2705 Category updated to {_category_emoji(category)} **{category}**.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        # Otherwise: save pending new expense
        p = context.user_data.pop("pending_single", None)
        if not p:
            await query.edit_message_text("\u274c Session expired. Please try again.")
            return
        if period_id is None:
            await query.edit_message_text(
                "\u274c No active period. Run /start or contact admin."
            )
            return
        execute_write(
            "INSERT INTO transactions "
            "(person, amount, description, period_id, payer, split_ratio, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (p["person"], p["amount"], p["description"], period_id,
             p["person"], p["split_ratio"], category),
        )
        logger.info(
            "Saved: %s $%.2f %s [%s]",
            p["person"], p["amount"], p["description"], category,
        )
        footer = build_confirmation_footer(calculate_period_settlement())
        await query.edit_message_text(
            f"\u2705 Saved: **{p['person']}** ${p['amount']:.2f} \u2014 "
            f"{p['description']} [{category}]\n\n{footer}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ---- Custom category prompt ----
    if data == "cat_custom":
        context.user_data["awaiting_custom_category"] = True
        await query.edit_message_text("\u270f\ufe0f Type your category name:")
        return

    # ---- Confirm bulk expenses ----
    if data == "conf_bulk":
        expenses = context.user_data.pop("pending_bulk", None)
        if not expenses:
            await query.edit_message_text("\u274c Session expired. Please try again.")
            return
        if period_id is None:
            await query.edit_message_text(
                "\u274c No active period. Run /start or contact admin."
            )
            return
        with get_db() as conn:
            for e in expenses:
                conn.execute(
                    """INSERT INTO transactions
                       (person, amount, description, period_id, payer, split_ratio, category)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (e["person"], e["amount"], e["description"], period_id,
                     e["person"], e["split_ratio"], e.get("category")),
                )
        total = sum(e["amount"] for e in expenses)
        logger.info("Saved %d bulk expenses, total $%.2f", len(expenses), total)
        await query.edit_message_text(
            f"\u2705 Saved **{len(expenses)} expenses** \u2014 total ${total:.2f}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ---- Undo ----
    if data.startswith("undo_"):
        trans_id = data.removeprefix("undo_")
        execute_write("DELETE FROM transactions WHERE id = ?", (trans_id,))
        logger.info("Deleted transaction id=%s", trans_id)
        await query.edit_message_text("\u2705 Transaction removed.")
        return

    # ---- Reset period ----
    if data == "reset_confirm":
        # Guard: no active period → nothing to reset
        closing_period_row = execute_query(
            "SELECT id FROM periods WHERE is_active = 1", fetchone=True
        )
        closing_period_id = closing_period_row[0] if closing_period_row else None
        if closing_period_id is None:
            await query.answer("No active period to reset.", show_alert=True)
            return

        s = calculate_period_settlement()
        # Strip the emoji prefix before storing in DB for cleaner history display
        settle_desc = settlement_line(s["princess_net"])
        settle_desc_plain = settle_desc.replace("\u2705 ", "").replace("\U0001f4b8 ", "")
        # Auto-save CSV before resetting
        csv_rows = execute_query(
            """SELECT id, payer, amount, description, datetime(timestamp, 'localtime'),
                      period_id, split_ratio, category
                 FROM transactions WHERE period_id = ?
                ORDER BY timestamp ASC""",
            (closing_period_id,), fetchall=True,
        ) or []
        saved_path = save_period_csv(closing_period_id, csv_rows)
        logger.info("Auto-saved CSV on reset: %s", saved_path)

        new_period_id = None
        try:
            with get_db() as conn:
                conn.execute(
                    """UPDATE periods
                          SET end_date = datetime('now'),
                              is_active = 0,
                              princess_total = ?,
                              jay_total = ?,
                              settlement_description = ?
                        WHERE is_active = 1""",
                    (s["princess_paid"], s["jay_paid"], settle_desc_plain),
                )
                cursor = conn.execute(
                    "INSERT INTO periods (start_date, is_active) VALUES (datetime('now'), 1)"
                )
                new_period_id = cursor.lastrowid
        except Exception as exc:
            logger.error("DB error during period reset: %s", exc)
            await query.message.reply_text(
                "\u274c Period reset failed due to a database error. Please try again."
            )
            return

        logger.info("Period reset. Settlement: %s", settle_desc_plain)
        try:
            await query.edit_message_text(
                "\u2705 **Period Reset!**\n\n"
                f"Princess: ${s['princess_paid']:.2f} | Jay: ${s['jay_paid']:.2f}\n"
                f"Total: ${s['total_spent']:.2f}\n\n"
                f"\U0001f4b8 {settle_desc_plain}\n\n"
                "New period started. CSV saved locally. Use /periods to see history.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            logger.warning("edit_message_text failed after reset: %s", exc)
            await query.message.reply_text(
                "\u2705 **Period Reset!**\n\n"
                f"Princess: ${s['princess_paid']:.2f} | Jay: ${s['jay_paid']:.2f}\n"
                f"Total: ${s['total_spent']:.2f}\n\n"
                f"\U0001f4b8 {settle_desc_plain}\n\n"
                "New period started. CSV saved locally. Use /periods to see history.",
                parse_mode=ParseMode.MARKDOWN,
            )
        if new_period_id:
            await send_recurring_carryover_prompt(query, context, new_period_id)
        return

    # ---- Edit: select transaction ----
    if data.startswith("edit_select_"):
        trans_id = data.removeprefix("edit_select_")
        row = execute_query(
            "SELECT id, person, amount, description, category FROM transactions WHERE id = ?",
            (trans_id,), fetchone=True,
        )
        if not row:
            await query.edit_message_text("\u274c Transaction not found.")
            return
        _, person, amount, description, category = row
        cat_display = _category_emoji(category) + " " + (category or "none")
        toggle_label = "🔁 Unset Recurring" if category == "recurring" else "🔁 Mark as Recurring"
        keyboard = [
            [InlineKeyboardButton(
                "\U0001f464 Change Person",
                callback_data=f"edit_person_{trans_id}",
            )],
            [InlineKeyboardButton(
                "\U0001f4b5 Change Amount",
                callback_data=f"edit_amount_{trans_id}",
            )],
            [InlineKeyboardButton(
                "\U0001f4dd Change Description",
                callback_data=f"edit_desc_{trans_id}",
            )],
            [InlineKeyboardButton(
                "\U0001f3f7 Change Category",
                callback_data=f"edit_cat_{trans_id}",
            )],
            [InlineKeyboardButton(
                toggle_label,
                callback_data=f"edit_toggle_recurring_{trans_id}",
            )],
            [InlineKeyboardButton("\u274c Cancel", callback_data="cancel")],
        ]
        await query.edit_message_text(
            "\u270f\ufe0f **Edit Transaction**\n\n"
            f"Person: {person}\nAmount: ${amount:.2f}\nDescription: {description}\nCategory: {cat_display}\n\n"
            "What would you like to change?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ---- Edit: confirm person change ----
    if data.startswith("edit_person_confirm_"):
        remainder = data.removeprefix("edit_person_confirm_")
        # Format stored as {trans_id}_{new_person} where new_person is Princess or Jay
        underscore_idx = remainder.index("_")
        trans_id = remainder[:underscore_idx]
        new_person = remainder[underscore_idx + 1:]
        if not trans_id.isdigit():
            await query.edit_message_text("\u274c Invalid transaction ID.")
            return
        if new_person not in VALID_PERSONS:
            await query.edit_message_text("\u274c Invalid person value.")
            return
        execute_write(
            "UPDATE transactions SET person = ?, payer = ? WHERE id = ?",
            (new_person, new_person, trans_id),
        )
        await query.edit_message_text(
            f"\u2705 Person changed to **{new_person}**.", parse_mode=ParseMode.MARKDOWN
        )
        return

    # ---- Edit: initiate person change ----
    if data.startswith("edit_person_"):
        trans_id = data.removeprefix("edit_person_")
        row = execute_query(
            "SELECT person FROM transactions WHERE id = ?", (trans_id,), fetchone=True
        )
        if not row:
            await query.edit_message_text("\u274c Transaction not found.")
            return
        current = row[0]
        new_person = "Jay" if current == "Princess" else "Princess"
        keyboard = [[
            InlineKeyboardButton(
                "\u2705 Confirm",
                callback_data=f"edit_person_confirm_{trans_id}_{new_person}",
            ),
            InlineKeyboardButton(
                "\u274c Cancel", callback_data=f"edit_select_{trans_id}"
            ),
        ]]
        await query.edit_message_text(
            f"Change person from **{current}** \u2192 **{new_person}**?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ---- Edit: change category ----
    if data.startswith("edit_cat_"):
        trans_id = data.removeprefix("edit_cat_")
        row = execute_query(
            "SELECT category FROM transactions WHERE id = ?", (trans_id,), fetchone=True
        )
        if not row:
            await query.edit_message_text("\u274c Transaction not found.")
            return
        current_cat = row[0]
        context.user_data["editing_transaction_id"] = trans_id
        context.user_data["editing_field"] = "category"
        await query.edit_message_text(
            f"\U0001f3f7 **Change Category**\n\nCurrent: {_category_emoji(current_cat)} {current_cat or 'none'}\n\nPick new category:",
            reply_markup=build_category_keyboard(pre_selected=current_cat),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ---- Edit: toggle recurring ----
    if data.startswith("edit_toggle_recurring_"):
        trans_id = data.removeprefix("edit_toggle_recurring_")
        row = execute_query(
            "SELECT category FROM transactions WHERE id = ?", (trans_id,), fetchone=True
        )
        if not row:
            await query.edit_message_text("\u274c Transaction not found.")
            return
        current_cat = row[0]
        # Retrieve previously stored non-recurring category (if any)
        previous_cat = context.user_data.pop(f"prev_cat_{trans_id}", None)
        new_cat, displaced = toggle_recurring_category(current_cat, previous=previous_cat)
        # If we just set to recurring, remember the old category for a future un-toggle
        if new_cat == "recurring" and displaced:
            context.user_data[f"prev_cat_{trans_id}"] = displaced
        execute_write(
            "UPDATE transactions SET category = ? WHERE id = ?", (new_cat, trans_id)
        )
        logger.info("Toggle recurring: id=%s %s → %s", trans_id, current_cat, new_cat)
        emoji = _category_emoji(new_cat)
        keyboard = [[
            InlineKeyboardButton("◀️ Back to Edit", callback_data=f"edit_select_{trans_id}"),
        ]]
        await query.edit_message_text(
            f"\u2705 Category toggled to {emoji} **{new_cat}**.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ---- Edit: change amount ----
    if data.startswith("edit_amount_"):
        trans_id = data.removeprefix("edit_amount_")
        context.user_data["editing_transaction_id"] = trans_id
        context.user_data["editing_field"] = "amount"
        await query.edit_message_text("\U0001f4b5 Enter the new amount (e.g. 50.00):")
        return

    # ---- Edit: change description ----
    if data.startswith("edit_desc_"):
        trans_id = data.removeprefix("edit_desc_")
        context.user_data["editing_transaction_id"] = trans_id
        context.user_data["editing_field"] = "description"
        await query.edit_message_text("\U0001f4dd Enter the new description:")
        return

    # ---- Set budget confirm ----
    if data.startswith("setbudget_confirm_"):
        try:
            amount = float(data.removeprefix("setbudget_confirm_"))
            if not math.isfinite(amount) or not 0.01 <= amount <= 1_000_000:
                raise ValueError
        except ValueError:
            await query.edit_message_text("\u274c Invalid budget value.")
            return
        execute_write(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("monthly_budget", f"{amount:.2f}"),
        )
        logger.info("Budget updated to $%.2f", amount)
        await query.edit_message_text(
            f"\u2705 Monthly budget set to **${amount:.2f}**.", parse_mode=ParseMode.MARKDOWN
        )
        return

    # ---- Set reminder confirm ----
    if data.startswith("setreminder_confirm_"):
        try:
            days = int(data.removeprefix("setreminder_confirm_"))
            if not 1 <= days <= 30:
                raise ValueError
        except ValueError:
            await query.edit_message_text("\u274c Invalid reminder value.")
            return
        execute_write(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("reminder_days", str(days)),
        )
        logger.info("Reminder days updated to %d", days)
        await query.edit_message_text(
            f"\u2705 Reminder set to every **{days} days**.\n\n"
            "\u26a0\ufe0f Update your cron job \u2014 see `/reminder_help`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ---- Set split confirm ----
    if data.startswith("setsplit_confirm_"):
        split_str = data.removeprefix("setsplit_confirm_")
        p_pct, j_pct = parse_split_ratio(split_str)
        execute_write(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("default_split", split_str),
        )
        logger.info("Default split updated to %s", split_str)
        await query.edit_message_text(
            f"\u2705 Default split set to **{split_str}** "
            f"(Princess {p_pct:.4g}% / Jay {j_pct:.4g}%).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ---- Export: current period ----
    if data == "export_current":
        rows = execute_query(
            """SELECT id, payer, amount, description, datetime(timestamp, 'localtime'),
                      period_id, split_ratio, category
                 FROM transactions
                WHERE period_id = (SELECT id FROM periods WHERE is_active = 1)
                ORDER BY timestamp ASC""",
            fetchall=True,
        ) or []
        if not rows:
            await query.edit_message_text("\u274c No transactions in current period.")
            return
        csv_content = generate_csv_content(rows)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        fd, tmp_path = tempfile.mkstemp(suffix=".csv", prefix=f"expenses_current_{timestamp}_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(csv_content)
            with open(tmp_path, "rb") as f:
                await query.message.reply_document(
                    document=f,
                    filename=f"expenses_current_{timestamp}.csv",
                    caption="\U0001f4ca **Current Period Export**",
                    parse_mode=ParseMode.MARKDOWN,
                )
            await query.edit_message_text("\u2705 Current period CSV sent.")
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        return

    # ---- Export: saved period file ----
    if data.startswith("export_file_"):
        fname = data.removeprefix("export_file_")
        if not re.match(r'^[\w\-\.]+\.csv$', fname):
            await query.edit_message_text("\u274c Invalid filename.")
            return
        exports_dir = os.path.join(os.path.dirname(DATABASE_PATH), "exports")
        fpath = os.path.join(exports_dir, fname)
        if not os.path.exists(fpath):
            await query.edit_message_text("\u274c File not found.")
            return
        with open(fpath, "rb") as f:
            await query.message.reply_document(
                document=f,
                filename=fname,
                caption=f"\U0001f4c1 **Period Export:** {fname}",
                parse_mode=ParseMode.MARKDOWN,
            )
        await query.edit_message_text(f"\u2705 Sent: {fname}")
        return

    # ---- Recurring carry-over prompt (rc:t/a/s) ----
    if data.startswith("rc:"):
        parsed_cb = parse_recurring_carryover_callback(data)
        if parsed_cb is None:
            await query.edit_message_text("\u274c Unknown action.")
            return

        action, cb_session_id, token = parsed_cb
        session = context.user_data.get("recurring_carryover_session")

        # Session missing, already completed, or stale session_id
        if not session or session.get("completed") or session.get("session_id") != cb_session_id:
            await query.answer("This prompt has expired.")
            return

        if action == "toggle":
            if token not in session["items_by_token"]:
                await query.answer("Unknown item.")
                return
            session["selected_tokens"] = toggle_selected_token(
                session["selected_tokens"], token
            )
            text = build_recurring_carryover_text(
                session["items_by_token"],
                session["selected_tokens"],
                session["ordered_tokens"],
            )
            keyboard = build_recurring_carryover_keyboard(
                session["items_by_token"],
                session["selected_tokens"],
                session["ordered_tokens"],
                session["session_id"],
            )
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
            return

        if action == "skip":
            session["completed"] = True
            await query.edit_message_text("⏭ Recurring carry-over skipped.")
            return

        if action == "add":
            if not session["selected_tokens"]:
                await query.answer("No items selected.")
                return
            target_period_id = session["period_id"]
            # Verify the target period still exists (safety check)
            period_exists = execute_query(
                "SELECT id FROM periods WHERE id = ?", (target_period_id,), fetchone=True
            )
            if not period_exists:
                session["completed"] = True
                await query.edit_message_text("\u274c Target period no longer exists.")
                return
            # Resolve selected items in stable order
            selected_items = get_selected_recurring_items(
                session["items_by_token"],
                session["selected_tokens"],
                session["ordered_tokens"],
            )
            # Idempotency: skip items already in this period
            existing_sigs = get_existing_recurring_signatures_for_period(target_period_id)
            new_items = filter_new_recurring_items(selected_items, existing_sigs)
            if new_items:
                with get_db() as conn:
                    for item in new_items:
                        conn.execute(
                            "INSERT INTO transactions "
                            "(person, amount, description, period_id, payer, split_ratio, category) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (item["person"], item["amount"], item["description"],
                             target_period_id, item["payer"], item["split_ratio"], "recurring"),
                        )
                logger.info(
                    "Carry-over: added %d recurring items to period %d",
                    len(new_items), target_period_id,
                )
            session["completed"] = True
            if new_items:
                names = ", ".join(i["description"] for i in new_items)
                await query.edit_message_text(
                    f"\u2705 Added **{len(new_items)} recurring item(s)** to the new period:\n{names}",
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                await query.edit_message_text(
                    "\u2705 All selected items were already in this period — nothing added."
                )
            return

    logger.warning("Unhandled callback_data: %s", data)
    await query.edit_message_text("\u274c Unknown action.")

# ---------------------------------------------------------------------------
# Recurring expenses command  (Feature 6)
# ---------------------------------------------------------------------------

async def recurring_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_check(update, context):
        return
    rows = execute_query(
        """SELECT payer, amount, description
             FROM transactions
            WHERE category = 'recurring'
            ORDER BY timestamp DESC""",
        fetchall=True,
    ) or []
    if not rows:
        await update.message.reply_text(
            "📋 No recurring expenses logged yet.\n\nTag an expense with `#recurring` to track it.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    grouped = group_recurring_rows(rows)
    lines = ["🔁 **Recurring Expenses** _(unique items)_\n"]
    for item in grouped:
        count_note = f" ×{item['count']}" if item["count"] > 1 else ""
        lines.append(
            f"• **{item['payer']}** ${item['amount']:.2f} \u2014 {item['description']}{count_note}"
        )
    monthly_total = sum(item["amount"] for item in grouped)
    lines.append(f"\n**Est. monthly total:** ${monthly_total:.2f}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Find / search command  (Feature 7)
# ---------------------------------------------------------------------------

async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_check(update, context):
        return
    if not context.args:
        await update.message.reply_text(
            "🔍 **Find Expenses**\n\nUsage: `/find <keyword>`\nExample: `/find groceries`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    query_str = " ".join(context.args)
    results = find_transactions(query_str)
    if not results:
        await update.message.reply_text(
            f"🔍 No expenses found matching **{query_str}**.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    lines = [f"🔍 **Results for \"{query_str}\":**\n"]
    for row in results:
        # row = (id, payer, description, amount, split_ratio, category)
        lines.append(f"• **{row[1]}** ${row[3]:.2f} \u2014 {row[2]}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set -- cannot start")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("periods", periods_history))
    app.add_handler(CommandHandler("undo", undo))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler("edit", edit_command))
    app.add_handler(CommandHandler("setbudget", setbudget))
    app.add_handler(CommandHandler("setreminder", setreminder))
    app.add_handler(CommandHandler("reminder_help", reminder_help))
    app.add_handler(CommandHandler("setsplit", setsplit))
    app.add_handler(CommandHandler("recurring", recurring_command))
    app.add_handler(CommandHandler("find", find_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Starting Expense Tracker Bot")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=-1,  # Retry indefinitely on startup until network is ready
    )


if __name__ == "__main__":
    main()
