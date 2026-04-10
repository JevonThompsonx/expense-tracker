#!/usr/bin/env python3
"""
Configuration file for Expense Tracker Bot
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

if not TELEGRAM_BOT_TOKEN:
    raise ValueError(
        "TELEGRAM_BOT_TOKEN is not set! "
        "Please create a .env file (see .env.example) and set your bot token."
    )

# Chat IDs (will be auto-discovered if not set)
PRINCESS_CHAT_ID = os.getenv('PRINCESS_CHAT_ID', '')
JAY_CHAT_ID = os.getenv('JAY_CHAT_ID', '')

# Database Configuration
_BASE_DIR = Path(__file__).resolve().parent
_RAW_DB_PATH = os.getenv('DATABASE_PATH', './data/expenses.db')
_RAW_DB_PATH = os.path.expanduser(_RAW_DB_PATH)

def _resolve_database_path(raw_path: str) -> str:
    if not raw_path or raw_path == ":memory:" or raw_path.startswith("file:"):
        return raw_path
    path = Path(raw_path)
    if not path.is_absolute():
        path = (_BASE_DIR / path).resolve()
    return str(path)


def _ensure_database_dir(path: str) -> None:
    if not path or path == ":memory:" or path.startswith("file:"):
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)


DATABASE_PATH = _resolve_database_path(_RAW_DB_PATH)
_ensure_database_dir(DATABASE_PATH)

# Budget Configuration
DEFAULT_MONTHLY_BUDGET = float(os.getenv('DEFAULT_BUDGET', '600.00'))

# Person Names (case-insensitive matching)
VALID_PERSONS = ['Princess', 'Jay']
