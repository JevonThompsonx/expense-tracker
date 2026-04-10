"""
Tests for budget_alert.py

Covers:
- build_alert_message() correctly includes settlement line
- build_alert_message() uses single-asterisk Markdown bold (not **)
- build_alert_message() shows correct totals
- get_budget_data() returns None when no active period exists
- Days-in-period calculation is non-negative
"""

import sqlite3
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import budget_alert


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(period_start: str | None = None, transactions: list | None = None) -> str:
    """
    Create a minimal temp SQLite DB with the schema used by budget_alert.
    Returns the file path.
    """
    fd, path = tempfile.mkstemp(suffix=".db", prefix="alert_test_")
    os.close(fd)
    conn = sqlite3.connect(path)
    try:
        conn.execute("""
            CREATE TABLE periods (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                start_date DATETIME NOT NULL,
                is_active  INTEGER DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                person      TEXT,
                amount      REAL,
                description TEXT,
                timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP,
                period_id   INTEGER,
                payer       TEXT,
                split_ratio TEXT DEFAULT '50/50'
            )
        """)
        conn.execute("""
            CREATE TABLE settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        if period_start is None:
            period_start = datetime.now().isoformat()

        conn.execute(
            "INSERT INTO periods (start_date, is_active) VALUES (?, 1)",
            (period_start,),
        )
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('monthly_budget', '600.0')"
        )
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('princess_chat_id', '111')"
        )
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('jay_chat_id', '222')"
        )

        if transactions:
            for payer, amount, split_ratio in transactions:
                conn.execute(
                    "INSERT INTO transactions (person, amount, description, period_id, payer, split_ratio) "
                    "VALUES (?, ?, 'test', 1, ?, ?)",
                    (payer, amount, payer, split_ratio),
                )
        conn.commit()
    finally:
        conn.close()
    return path


# ---------------------------------------------------------------------------
# Tests for build_alert_message()
# ---------------------------------------------------------------------------

class TestBuildAlertMessage:
    """Tests for the budget_alert.build_alert_message() helper."""

    def test_returns_string(self):
        msg = budget_alert.build_alert_message(
            princess_total=100.0,
            jay_total=200.0,
            budget=600.0,
            days_in_period=5,
            princess_net=-10.0,
        )
        assert isinstance(msg, str)

    def test_shows_total_spent(self):
        msg = budget_alert.build_alert_message(
            princess_total=100.0,
            jay_total=200.0,
            budget=600.0,
            days_in_period=5,
            princess_net=0.0,
        )
        assert "300.00" in msg  # total_spent = 100 + 200

    def test_shows_remaining_budget(self):
        msg = budget_alert.build_alert_message(
            princess_total=100.0,
            jay_total=200.0,
            budget=600.0,
            days_in_period=5,
            princess_net=0.0,
        )
        assert "300.00" in msg  # remaining = 600 - 300

    def test_shows_percent_used(self):
        msg = budget_alert.build_alert_message(
            princess_total=150.0,
            jay_total=150.0,
            budget=600.0,
            days_in_period=5,
            princess_net=0.0,
        )
        assert "50.0%" in msg  # 300/600 = 50%

    def test_settlement_princess_owes_jay_shown(self):
        """When princess_net < 0, message must say Princess owes Jay."""
        msg = budget_alert.build_alert_message(
            princess_total=100.0,
            jay_total=200.0,
            budget=600.0,
            days_in_period=5,
            princess_net=-13.26,  # negative = Princess owes Jay
        )
        assert "Princess owes Jay" in msg
        assert "13.26" in msg

    def test_settlement_jay_owes_princess_shown(self):
        """When princess_net > 0, message must say Jay owes Princess."""
        msg = budget_alert.build_alert_message(
            princess_total=300.0,
            jay_total=100.0,
            budget=600.0,
            days_in_period=5,
            princess_net=50.0,  # positive = Jay owes Princess
        )
        assert "Jay owes Princess" in msg
        assert "50.00" in msg

    def test_settlement_all_settled(self):
        """When princess_net is ~0, message must say settled up."""
        msg = budget_alert.build_alert_message(
            princess_total=200.0,
            jay_total=200.0,
            budget=600.0,
            days_in_period=5,
            princess_net=0.0,
        )
        assert "settled" in msg.lower()

    def test_no_double_asterisk_bold(self):
        """Message must use *single* asterisk for bold (Telegram MarkdownV1), not **."""
        msg = budget_alert.build_alert_message(
            princess_total=100.0,
            jay_total=200.0,
            budget=600.0,
            days_in_period=5,
            princess_net=0.0,
        )
        assert "**" not in msg

    def test_over_budget_icon(self):
        """When over budget, message should contain warning indicator."""
        msg = budget_alert.build_alert_message(
            princess_total=350.0,
            jay_total=350.0,
            budget=600.0,
            days_in_period=5,
            princess_net=0.0,
        )
        assert "Over budget" in msg or "⚠" in msg

    def test_days_in_period_shown(self):
        msg = budget_alert.build_alert_message(
            princess_total=100.0,
            jay_total=100.0,
            budget=600.0,
            days_in_period=7,
            princess_net=0.0,
        )
        assert "7" in msg


# ---------------------------------------------------------------------------
# Tests for get_budget_data()
# ---------------------------------------------------------------------------

class TestGetBudgetData:
    """Tests for budget_alert.get_budget_data() using real temp SQLite files."""

    def test_returns_none_when_no_active_period(self):
        fd, path = tempfile.mkstemp(suffix=".db", prefix="alert_test_")
        os.close(fd)
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE periods (id INTEGER PRIMARY KEY, start_date DATETIME, is_active INTEGER)"
            )
            conn.execute(
                "CREATE TABLE transactions (id INTEGER PRIMARY KEY, payer TEXT, amount REAL, "
                "period_id INTEGER, split_ratio TEXT)"
            )
            conn.execute(
                "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)"
            )
            conn.execute("INSERT INTO settings VALUES ('princess_chat_id', '111')")
            conn.execute("INSERT INTO settings VALUES ('jay_chat_id', '222')")
            conn.execute("INSERT INTO settings VALUES ('monthly_budget', '600')")
            # No active period row
            conn.commit()
        finally:
            conn.close()
        try:
            with patch.object(budget_alert, "DATABASE_PATH", path):
                result = budget_alert.get_budget_data()
            assert result is None
        finally:
            os.unlink(path)

    def test_returns_dict_with_expected_keys(self):
        path = _make_db(transactions=[("Princess", 50.0, "40/60"), ("Jay", 100.0, "40/60")])
        try:
            with patch.object(budget_alert, "DATABASE_PATH", path):
                result = budget_alert.get_budget_data()
            assert result is not None
            for key in ("princess_chat", "jay_chat", "budget",
                        "princess_total", "jay_total", "period_start", "princess_net"):
                assert key in result, f"Missing key: {key}"
        finally:
            os.unlink(path)

    def test_princess_net_calculation_correct(self):
        """
        Princess paid 50, Jay paid 100. Both 40/60 split.
        Total = 150; princess_owes = 150*0.4 = 60; jay_owes = 150*0.6 = 90
        princess_net = 50 - 60 = -10 (Princess owes Jay $10)
        """
        path = _make_db(transactions=[("Princess", 50.0, "40/60"), ("Jay", 100.0, "40/60")])
        try:
            with patch.object(budget_alert, "DATABASE_PATH", path):
                result = budget_alert.get_budget_data()
            assert result is not None
            assert abs(result["princess_net"] - (-10.0)) < 0.01
        finally:
            os.unlink(path)

    def test_zero_transactions_returns_zero_totals(self):
        path = _make_db(transactions=[])
        try:
            with patch.object(budget_alert, "DATABASE_PATH", path):
                result = budget_alert.get_budget_data()
            assert result is not None
            assert result["princess_total"] == 0.0
            assert result["jay_total"] == 0.0
            assert result["princess_net"] == 0.0
        finally:
            os.unlink(path)

    def test_days_in_period_non_negative(self):
        # Start date in the past
        past = (datetime.now() - timedelta(days=5)).isoformat()
        path = _make_db(period_start=past, transactions=[])
        try:
            with patch.object(budget_alert, "DATABASE_PATH", path):
                result = budget_alert.get_budget_data()
            assert result is not None
            assert result["days_in_period"] >= 0
        finally:
            os.unlink(path)
