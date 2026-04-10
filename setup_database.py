#!/usr/bin/env python3
"""
Database setup script for Expense Tracker.
Safe to run multiple times -- fully idempotent.
Run this after initial install and after any schema migration.
"""

import sqlite3
import os
import sys

from config import DATABASE_PATH, DEFAULT_MONTHLY_BUDGET


def setup_database() -> None:
    db_dir = os.path.dirname(DATABASE_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
        print(f"  Created directory: {db_dir}")

    conn = sqlite3.connect(DATABASE_PATH)
    try:
        cursor = conn.cursor()

        # Enable WAL mode for better read/write concurrency.
        # WAL mode persists in the DB file -- subsequent connections inherit it.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        print(f"  Connected: {DATABASE_PATH} (WAL mode enabled)")

        # --- transactions ---
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                person      TEXT    NOT NULL CHECK(person IN ('Princess', 'Jay')),
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
        print("  transactions table: ready")

        # Migration: add columns if upgrading from an older schema
        for col, definition in [
            ("payer",       "TEXT NOT NULL DEFAULT 'Princess'"),
            ("split_ratio", "TEXT DEFAULT '50/50'"),
            ("category",    "TEXT"),
        ]:
            try:
                cursor.execute(f"SELECT {col} FROM transactions LIMIT 1")
            except sqlite3.OperationalError:
                cursor.execute(
                    f"ALTER TABLE transactions ADD COLUMN {col} {definition}"
                )
                if col == "payer":
                    cursor.execute(
                        "UPDATE transactions SET payer = person "
                        "WHERE payer IS NULL OR payer = ''"
                    )
                elif col == "split_ratio":
                    cursor.execute(
                        "UPDATE transactions SET split_ratio = '50/50' "
                        "WHERE split_ratio IS NULL"
                    )
                print(f"  Migration: added '{col}' column")

        # --- periods ---
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS periods (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                start_date           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                end_date             DATETIME,
                is_active            INTEGER  DEFAULT 1 CHECK(is_active IN (0, 1)),
                princess_total       REAL     DEFAULT 0,
                jay_total            REAL     DEFAULT 0,
                settlement_description TEXT
            )
        """)
        print("  periods table: ready")

        # --- settings ---
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        print("  settings table: ready")

        # --- indexes ---
        for idx, table, col in [
            ("idx_transactions_period",    "transactions", "period_id"),
            ("idx_transactions_person",    "transactions", "person"),
            ("idx_transactions_payer",     "transactions", "payer"),
            ("idx_transactions_timestamp", "transactions", "timestamp"),
            ("idx_periods_active",         "periods",      "is_active"),
        ]:
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS {idx} ON {table}({col})"
            )
        print("  indexes: ready")

        # Ensure at least one active period exists
        cursor.execute("SELECT COUNT(*) FROM periods WHERE is_active = 1")
        if cursor.fetchone()[0] == 0:
            cursor.execute(
                "INSERT INTO periods (start_date, is_active) VALUES (datetime('now'), 1)"
            )
            print("  Created initial active period")
        else:
            print("  Active period: already exists")

        # Default settings (INSERT OR IGNORE preserves existing values)
        for key, value in [
            ("monthly_budget",   str(DEFAULT_MONTHLY_BUDGET)),
            ("princess_chat_id", ""),
            ("jay_chat_id",      ""),
            ("reminder_days",    "3"),
            ("default_split",    "50/50"),
        ]:
            cursor.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        print("  settings: initialized")

        conn.commit()
        print(f"\nDatabase setup complete: {DATABASE_PATH}")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        setup_database()
    except Exception as exc:
        print(f"\nDatabase setup failed: {exc}", file=sys.stderr)
        sys.exit(1)
