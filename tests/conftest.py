"""
Pytest fixtures for expense-tracker test suite.

Key design decisions:
- expense_bot.py imports `config.py` at module load time, which in turn reads
  TELEGRAM_BOT_TOKEN from the .env file.  The real .env already contains a
  valid token, so the module import succeeds without any mocking.
- calculate_period_settlement() and the DB helpers all open *fresh* SQLite
  connections using `expense_bot.DATABASE_PATH`.  Because every connection is
  independent, `:memory:` would give each call an empty, schema-less database.
  Instead, we create a real temporary SQLite *file* for each test that needs DB
  access, set it up with the full schema, and monkey-patch
  `expense_bot.DATABASE_PATH` for the duration of the test.
"""

import os
import sqlite3
import tempfile

import pytest

import expense_bot as bot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_schema(db_path: str) -> None:
    """Create the minimum schema required by calculate_period_settlement."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS periods (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                start_date           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                end_date             DATETIME,
                is_active            INTEGER  DEFAULT 1
                                     CHECK(is_active IN (0, 1)),
                princess_total       REAL     DEFAULT 0,
                jay_total            REAL     DEFAULT 0,
                settlement_description TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                person      TEXT    NOT NULL
                            CHECK(person IN ('Princess', 'Jay')),
                amount      REAL    NOT NULL CHECK(amount > 0),
                description TEXT    NOT NULL,
                timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP,
                period_id   INTEGER NOT NULL,
                payer       TEXT    NOT NULL DEFAULT 'Princess'
                            CHECK(payer IN ('Princess', 'Jay')),
                split_ratio TEXT    DEFAULT '50/50',
                category    TEXT,
                FOREIGN KEY (period_id) REFERENCES periods(id)
            )
        """)
        # Seed one active period so get_active_period_id() returns a row.
        conn.execute(
            "INSERT INTO periods (start_date, is_active) VALUES (datetime('now'), 1)"
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_db(monkeypatch):
    """
    Fixture that:
      1. Creates a temporary SQLite file with the full schema + one active period.
      2. Patches expense_bot.DATABASE_PATH to point at that file.
      3. Yields the file path so tests can insert rows directly.
      4. Removes the file on teardown.

    Uses monkeypatch so the patch is automatically undone after each test.
    """
    fd, path = tempfile.mkstemp(suffix=".db", prefix="expense_test_")
    os.close(fd)
    _create_schema(path)
    monkeypatch.setattr(bot, "DATABASE_PATH", path)
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture(autouse=True)
def reset_rate_tracker():
    """
    Clear the in-process rate-limiter dictionary before every test so that
    rate-limiter tests are fully independent of each other.
    """
    bot._rate_tracker.clear()
    yield
    bot._rate_tracker.clear()
