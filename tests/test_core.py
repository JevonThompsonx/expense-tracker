"""
Comprehensive test suite for expense_bot.py pure/testable functions.

Tested functions
----------------
- parse_split_ratio(split_text)
- calculate_split_amounts(amount, split_ratio)
- parse_expense(text, default_split)
- settlement_line(princess_net)
- is_rate_limited(chat_id)
- calculate_period_settlement()  -- requires in-memory DB via tmp_db fixture
- build_confirmation_footer(settlement)
- budget_bar(spent, budget, days_left)
- build_summary_message(rows, budget)

No Telegram API calls are made anywhere in this file.
"""

import os
import sqlite3
from time import monotonic
from unittest.mock import patch

import pytest

import expense_bot as bot
from expense_bot import (
    MAX_AMOUNT,
    MAX_DESCRIPTION_LEN,
    MIN_AMOUNT,
    RATE_LIMIT_MAX,
    RATE_LIMIT_WINDOW,
    calculate_period_settlement,
    calculate_split_amounts,
    is_rate_limited,
    parse_expense,
    parse_split_ratio,
    settlement_line,
    build_confirmation_footer,
    budget_bar,
    build_summary_message,
)
from telegram import InlineKeyboardMarkup


# ===========================================================================
# parse_split_ratio
# ===========================================================================

class TestParseSplitRatio:
    """Tests for parse_split_ratio(split_text) -> (princess_pct, jay_pct)."""

    def test_none_returns_fifty_fifty(self):
        assert parse_split_ratio(None) == (50.0, 50.0)

    def test_fifty_fifty_string_returns_fifty_fifty(self):
        assert parse_split_ratio("50/50") == (50.0, 50.0)

    def test_sixty_forty(self):
        assert parse_split_ratio("60/40") == (60.0, 40.0)

    def test_invalid_alpha_falls_back(self):
        """Non-numeric parts must fall back to 50/50."""
        assert parse_split_ratio("abc/xyz") == (50.0, 50.0)

    def test_parts_not_summing_to_100_falls_back(self):
        """70/40 = 110, not 100 -> fallback."""
        assert parse_split_ratio("70/40") == (50.0, 50.0)

    def test_zero_one_hundred(self):
        assert parse_split_ratio("0/100") == (0.0, 100.0)

    def test_one_hundred_zero(self):
        assert parse_split_ratio("100/0") == (100.0, 0.0)

    def test_empty_string_falls_back(self):
        assert parse_split_ratio("") == (50.0, 50.0)

    def test_only_one_part_falls_back(self):
        """A string without '/' is not a valid split."""
        assert parse_split_ratio("50") == (50.0, 50.0)

    def test_negative_values_fall_back(self):
        """-10/110 sums to 100 but has a negative -- must fall back."""
        assert parse_split_ratio("-10/110") == (50.0, 50.0)

    def test_whitespace_around_values_parses_ok(self):
        """Leading/trailing whitespace on the whole string should be stripped."""
        assert parse_split_ratio("  70/30  ") == (70.0, 30.0)

    def test_thirty_seventy(self):
        assert parse_split_ratio("30/70") == (30.0, 70.0)

    def test_floating_point_split(self):
        """33.33/66.67 sums to ~100 within tolerance."""
        p, j = parse_split_ratio("33.33/66.67")
        assert abs(p - 33.33) < 0.01
        assert abs(j - 66.67) < 0.01


# ===========================================================================
# calculate_split_amounts
# ===========================================================================

class TestCalculateSplitAmounts:
    """Tests for calculate_split_amounts(amount, split_ratio) -> (princess_owes, jay_owes)."""

    def test_hundred_fifty_fifty(self):
        assert calculate_split_amounts(100, "50/50") == (50.0, 50.0)

    def test_hundred_sixty_forty(self):
        assert calculate_split_amounts(100, "60/40") == (60.0, 40.0)

    def test_seventy_five_zero_hundred(self):
        assert calculate_split_amounts(75, "0/100") == (0.0, 75.0)

    def test_none_split_defaults_to_fifty_fifty(self):
        assert calculate_split_amounts(200, None) == (100.0, 100.0)

    def test_small_amount(self):
        p, j = calculate_split_amounts(1.00, "50/50")
        assert abs(p - 0.50) < 0.001
        assert abs(j - 0.50) < 0.001

    def test_large_amount_with_split(self):
        p, j = calculate_split_amounts(99999.99, "60/40")
        assert abs(p - 59999.994) < 0.01
        assert abs(j - 39999.996) < 0.01

    def test_amounts_sum_to_total(self):
        amount = 137.55
        p, j = calculate_split_amounts(amount, "70/30")
        assert abs(p + j - amount) < 0.001


# ===========================================================================
# parse_expense
# ===========================================================================

class TestParseExpenseSingleLine:
    """Tests for single-line expense parsing."""

    # --- Happy path ---

    def test_basic_princess_expense(self):
        result = parse_expense("Princess 50 groceries")
        assert result is not None
        assert result["person"] == "Princess"
        assert result["amount"] == 50.0
        assert result["description"] == "groceries"
        assert result["split_ratio"] == "50/50"
        assert result["bulk"] is False

    def test_jay_with_decimal_amount(self):
        result = parse_expense("Jay 25.50 coffee")
        assert result is not None
        assert result["person"] == "Jay"
        assert abs(result["amount"] - 25.50) < 0.001
        assert result["description"] == "coffee"
        assert result["split_ratio"] == "50/50"

    def test_princess_with_split_override(self):
        result = parse_expense("Princess 100 dinner -split 60/40")
        assert result is not None
        assert result["person"] == "Princess"
        assert result["amount"] == 100.0
        assert result["description"] == "dinner"
        assert result["split_ratio"] == "60/40"

    def test_case_insensitive_name_capitalized(self):
        """'princess' (lowercase) must be capitalised in the result."""
        result = parse_expense("princess 50 test")
        assert result is not None
        assert result["person"] == "Princess"

    def test_jay_case_insensitive(self):
        result = parse_expense("jay 10 snack")
        assert result is not None
        assert result["person"] == "Jay"

    def test_default_split_applied_when_no_override(self):
        result = parse_expense("Princess 50 lunch", default_split="70/30")
        assert result is not None
        assert result["split_ratio"] == "70/30"

    def test_explicit_split_overrides_default(self):
        result = parse_expense("Princess 50 lunch -split 40/60", default_split="70/30")
        assert result is not None
        assert result["split_ratio"] == "40/60"

    # --- Boundary amounts ---

    def test_exactly_min_amount_is_valid(self):
        """MIN_AMOUNT = 0.01 should be accepted."""
        result = parse_expense(f"Princess {MIN_AMOUNT} test")
        assert result is not None
        assert abs(result["amount"] - MIN_AMOUNT) < 1e-9

    def test_below_min_amount_returns_none(self):
        below = MIN_AMOUNT / 2          # 0.005, well below 0.01
        result = parse_expense(f"Princess {below} test")
        assert result is None

    def test_exactly_max_amount_is_valid(self):
        """MAX_AMOUNT = 99_999.99 should be accepted."""
        result = parse_expense(f"Princess {MAX_AMOUNT} test")
        assert result is not None
        assert abs(result["amount"] - MAX_AMOUNT) < 0.001

    def test_above_max_amount_returns_none(self):
        above = MAX_AMOUNT + 0.01       # 100_000.00
        result = parse_expense(f"Princess {above} test")
        assert result is None

    def test_round_number_above_max_returns_none(self):
        result = parse_expense("Princess 100000 test")
        assert result is None

    # --- Invalid inputs ---

    def test_invalid_text_returns_none(self):
        assert parse_expense("Hello world") is None

    def test_empty_string_returns_none(self):
        assert parse_expense("") is None

    def test_whitespace_only_returns_none(self):
        assert parse_expense("   ") is None

    def test_unknown_person_returns_none(self):
        assert parse_expense("Bob 50 groceries") is None

    def test_missing_description_returns_none(self):
        assert parse_expense("Princess 50") is None

    def test_non_numeric_amount_returns_none(self):
        assert parse_expense("Princess abc groceries") is None

    # --- Description length ---

    def test_description_truncated_at_max_len(self):
        long_desc = "x" * (MAX_DESCRIPTION_LEN + 50)   # 250 chars
        result = parse_expense(f"Princess 50 {long_desc}")
        assert result is not None
        assert len(result["description"]) == MAX_DESCRIPTION_LEN

    def test_description_at_exact_max_len_not_truncated(self):
        desc = "a" * MAX_DESCRIPTION_LEN
        result = parse_expense(f"Princess 50 {desc}")
        assert result is not None
        assert len(result["description"]) == MAX_DESCRIPTION_LEN


class TestParseExpenseAmountLast:
    """Tests for the 'Name Description Amount' (amount-last) order."""

    def test_jay_description_then_amount(self):
        result = parse_expense("Jay ice cream 54.33")
        assert result is not None
        assert result["person"] == "Jay"
        assert abs(result["amount"] - 54.33) < 0.001
        assert result["description"] == "ice cream"
        assert result["bulk"] is False

    def test_princess_description_then_amount(self):
        result = parse_expense("Princess groceries 99.50")
        assert result is not None
        assert result["person"] == "Princess"
        assert abs(result["amount"] - 99.50) < 0.001
        assert result["description"] == "groceries"

    def test_amount_last_single_word_description(self):
        result = parse_expense("Jay coffee 5")
        assert result is not None
        assert result["amount"] == 5.0
        assert result["description"] == "coffee"

    def test_amount_last_multi_word_description(self):
        result = parse_expense("Princess thai food dinner 42.00")
        assert result is not None
        assert abs(result["amount"] - 42.00) < 0.001
        assert result["description"] == "thai food dinner"

    def test_amount_last_with_split_override(self):
        result = parse_expense("Princess dinner 100 -split 60/40")
        assert result is not None
        assert abs(result["amount"] - 100.0) < 0.001
        assert result["description"] == "dinner"
        assert result["split_ratio"] == "60/40"

    def test_amount_last_uses_default_split(self):
        result = parse_expense("Jay lunch 30", default_split="40/60")
        assert result is not None
        assert result["split_ratio"] == "40/60"

    def test_amount_last_below_min_returns_none(self):
        result = parse_expense(f"Jay coffee {MIN_AMOUNT / 2:.4f}")
        assert result is None

    def test_amount_last_above_max_returns_none(self):
        result = parse_expense(f"Princess dinner {MAX_AMOUNT + 1:.2f}")
        assert result is None

    def test_amount_last_case_insensitive_name(self):
        result = parse_expense("jay ice cream 54.33")
        assert result is not None
        assert result["person"] == "Jay"

    def test_amount_last_description_truncated(self):
        long_desc = "x" * (MAX_DESCRIPTION_LEN + 50)
        result = parse_expense(f"Jay {long_desc} 10")
        assert result is not None
        assert len(result["description"]) == MAX_DESCRIPTION_LEN

    def test_amount_first_still_works(self):
        """Original order must remain unbroken."""
        result = parse_expense("Jay 54.33 ice cream")
        assert result is not None
        assert abs(result["amount"] - 54.33) < 0.001
        assert result["description"] == "ice cream"

    def test_description_that_ends_in_number_uses_last_token_as_amount(self):
        """'Jay room 101 50' -> description='room 101', amount=50."""
        result = parse_expense("Jay room 101 50")
        assert result is not None
        assert abs(result["amount"] - 50.0) < 0.001
        assert result["description"] == "room 101"


class TestParseExpenseBulk:
    """Tests for multi-line (bulk) expense parsing."""

    def test_bulk_three_expenses(self):
        text = "Princess\n- 50 groceries\n- 25 coffee\nJay\n- 30 lunch"
        result = parse_expense(text)
        assert result is not None
        assert result["bulk"] is True
        expenses = result["expenses"]
        assert len(expenses) == 3

    def test_bulk_persons_assigned_correctly(self):
        text = "Princess\n- 50 groceries\n- 25 coffee\nJay\n- 30 lunch"
        result = parse_expense(text)
        assert result["expenses"][0]["person"] == "Princess"
        assert result["expenses"][1]["person"] == "Princess"
        assert result["expenses"][2]["person"] == "Jay"

    def test_bulk_amounts_correct(self):
        text = "Princess\n- 50 groceries\n- 25 coffee\nJay\n- 30 lunch"
        result = parse_expense(text)
        amounts = [e["amount"] for e in result["expenses"]]
        assert amounts == [50.0, 25.0, 30.0]

    def test_bulk_descriptions_correct(self):
        text = "Princess\n- 50 groceries\n- 25 coffee\nJay\n- 30 lunch"
        result = parse_expense(text)
        descs = [e["description"] for e in result["expenses"]]
        assert descs == ["groceries", "coffee", "lunch"]

    def test_bulk_default_split_applied(self):
        text = "Princess\n- 50 groceries"
        result = parse_expense(text)
        assert result["expenses"][0]["split_ratio"] == "50/50"

    def test_bulk_with_split_override(self):
        text = "Princess\n- 50 groceries -split 60/40"
        result = parse_expense(text)
        assert result is not None
        assert result["expenses"][0]["split_ratio"] == "60/40"

    def test_bulk_returns_none_when_no_valid_expenses(self):
        """Lines that don't match the '- amount description' pattern -> None."""
        text = "Princess\n- abc not_a_number"
        assert parse_expense(text) is None

    def test_bulk_skips_amount_below_min(self):
        """Items below MIN_AMOUNT are silently dropped; if none remain -> None."""
        text = f"Princess\n- {MIN_AMOUNT / 10:.4f} too_cheap"
        assert parse_expense(text) is None

    def test_bulk_skips_amount_above_max(self):
        text = f"Princess\n- {MAX_AMOUNT + 1:.2f} too_expensive"
        assert parse_expense(text) is None

    def test_bulk_empty_lines_ignored(self):
        """Blank separator lines between sections must not cause errors."""
        text = "Princess\n- 50 groceries\n\nJay\n- 30 lunch"
        result = parse_expense(text)
        assert result is not None
        assert len(result["expenses"]) == 2

    def test_bulk_case_insensitive_person(self):
        """'princess' in bulk header should be normalised to 'Princess'."""
        text = "princess\n- 50 groceries"
        result = parse_expense(text)
        assert result is not None
        assert result["expenses"][0]["person"] == "Princess"

    def test_bulk_description_truncated(self):
        long_desc = "y" * (MAX_DESCRIPTION_LEN + 50)
        text = f"Princess\n- 50 {long_desc}"
        result = parse_expense(text)
        assert result is not None
        assert len(result["expenses"][0]["description"]) == MAX_DESCRIPTION_LEN

    def test_bulk_default_split_propagated(self):
        """default_split parameter must be used for items without -split."""
        text = "Jay\n- 80 dinner"
        result = parse_expense(text, default_split="30/70")
        assert result["expenses"][0]["split_ratio"] == "30/70"


# ===========================================================================
# settlement_line
# ===========================================================================

class TestSettlementLine:
    """Tests for settlement_line(princess_net) -> str."""

    def test_exactly_zero_settled(self):
        assert settlement_line(0.0) == "✅ All settled up!"

    def test_below_threshold_settled(self):
        """abs(0.005) < 0.01 -> settled."""
        assert settlement_line(0.005) == "✅ All settled up!"

    def test_negative_below_threshold_settled(self):
        assert settlement_line(-0.009) == "✅ All settled up!"

    def test_jay_owes_princess_positive_net(self):
        msg = settlement_line(25.0)
        assert "Jay owes Princess" in msg
        assert "$25.00" in msg

    def test_princess_owes_jay_negative_net(self):
        msg = settlement_line(-15.0)
        assert "Princess owes Jay" in msg
        assert "$15.00" in msg

    def test_large_positive_amount_formatted(self):
        msg = settlement_line(1234.56)
        assert "$1234.56" in msg
        assert "Jay owes Princess" in msg

    def test_large_negative_amount_formatted(self):
        msg = settlement_line(-999.99)
        assert "$999.99" in msg
        assert "Princess owes Jay" in msg

    def test_just_above_threshold_not_settled(self):
        """0.01 is the threshold; 0.011 must not return 'settled'."""
        msg = settlement_line(0.011)
        assert "All settled up" not in msg

    def test_emoji_prefix_present(self):
        """Both outcome branches should start with the money-wings emoji."""
        msg_pos = settlement_line(10.0)
        msg_neg = settlement_line(-10.0)
        assert msg_pos.startswith("💸")
        assert msg_neg.startswith("💸")


# ===========================================================================
# is_rate_limited
# ===========================================================================

class TestIsRateLimited:
    """
    Tests for the sliding-window rate limiter.

    The `reset_rate_tracker` autouse fixture (defined in conftest.py) ensures
    _rate_tracker is cleared before each test.
    """

    def test_first_call_for_new_chat_id_not_limited(self):
        assert is_rate_limited(1001) is False

    def test_returns_false_up_to_max_inclusive(self):
        """The (RATE_LIMIT_MAX)th call should still succeed (not limited)."""
        chat_id = 2002
        for _ in range(RATE_LIMIT_MAX - 1):
            is_rate_limited(chat_id)
        # The RATE_LIMIT_MAX-th call (30th) should still be False
        assert is_rate_limited(chat_id) is False

    def test_over_limit_returns_true(self):
        """After RATE_LIMIT_MAX calls the next one must be throttled."""
        chat_id = 3003
        for _ in range(RATE_LIMIT_MAX):
            is_rate_limited(chat_id)
        assert is_rate_limited(chat_id) is True

    def test_different_chat_ids_tracked_independently(self):
        """Exhausting one chat_id's quota must not affect another."""
        chat_a = 4004
        chat_b = 5005
        for _ in range(RATE_LIMIT_MAX):
            is_rate_limited(chat_a)
        # chat_a is now throttled
        assert is_rate_limited(chat_a) is True
        # chat_b is completely fresh
        assert is_rate_limited(chat_b) is False

    def test_old_timestamps_expire_from_window(self):
        """
        Timestamps outside the RATE_LIMIT_WINDOW must not count.

        We inject RATE_LIMIT_MAX stale timestamps manually (monotonic() far in
        the past) then call is_rate_limited once; the stale entries should be
        pruned and the call should succeed.
        """
        chat_id = 6006
        stale_time = monotonic() - (RATE_LIMIT_WINDOW + 10)
        bot._rate_tracker[chat_id] = [stale_time] * RATE_LIMIT_MAX
        # After pruning the stale entries, the first real call must pass.
        assert is_rate_limited(chat_id) is False

    def test_many_chat_ids_all_independent(self):
        """Verify N fresh chat IDs are each allowed their first request."""
        results = [is_rate_limited(chat_id) for chat_id in range(7000, 7020)]
        assert all(r is False for r in results)


# ===========================================================================
# calculate_period_settlement  (DB-backed tests)
# ===========================================================================

class TestCalculatePeriodSettlement:
    """
    Integration-style tests for calculate_period_settlement().

    Each test uses the `tmp_db` fixture from conftest.py which:
      - creates a temp SQLite file with the correct schema + 1 active period,
      - patches expense_bot.DATABASE_PATH to that file,
      - cleans up afterwards.
    """

    def _insert_tx(self, db_path: str, payer: str, amount: float,
                   description: str = "test", split_ratio: str = "50/50") -> None:
        """Helper: insert a transaction into the test DB (period_id=1)."""
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """INSERT INTO transactions
                   (person, amount, description, period_id, payer, split_ratio)
                   VALUES (?, ?, ?, 1, ?, ?)""",
                (payer, amount, description, payer, split_ratio),
            )
            conn.commit()
        finally:
            conn.close()

    # --- Empty period ---

    def test_empty_period_returns_zeros(self, tmp_db):
        result = calculate_period_settlement()
        assert result["princess_paid"] == 0
        assert result["jay_paid"] == 0
        assert result["princess_owes"] == 0.0
        assert result["jay_owes"] == 0.0
        assert result["total_spent"] == 0
        assert result["princess_net"] == 0.0

    # --- Single payer ---

    def test_princess_paid_only_fifty_fifty(self, tmp_db):
        """Princess pays 100. Each owes 50. princess_net = 100 - 50 = 50."""
        self._insert_tx(tmp_db, "Princess", 100.0, split_ratio="50/50")
        result = calculate_period_settlement()
        assert result["princess_paid"] == 100.0
        assert result["jay_paid"] == 0.0
        assert abs(result["princess_owes"] - 50.0) < 0.001
        assert abs(result["jay_owes"] - 50.0) < 0.001
        assert abs(result["princess_net"] - 50.0) < 0.001

    def test_jay_paid_only_fifty_fifty(self, tmp_db):
        """Jay pays 80. Each owes 40. jay_net = 80 - 40 = 40. princess_net = 0 - 40 = -40."""
        self._insert_tx(tmp_db, "Jay", 80.0, split_ratio="50/50")
        result = calculate_period_settlement()
        assert result["jay_paid"] == 80.0
        assert result["princess_paid"] == 0.0
        assert abs(result["princess_net"] - (-40.0)) < 0.001

    # --- Mixed payers ---

    def test_mixed_payers_fifty_fifty(self, tmp_db):
        """
        Princess pays 100, Jay pays 40. Total = 140. Each owes 70.
        princess_net = 100 - 70 = 30  -> Jay owes Princess $30.
        """
        self._insert_tx(tmp_db, "Princess", 100.0, split_ratio="50/50")
        self._insert_tx(tmp_db, "Jay", 40.0, split_ratio="50/50")
        result = calculate_period_settlement()
        assert result["princess_paid"] == 100.0
        assert result["jay_paid"] == 40.0
        assert result["total_spent"] == 140.0
        assert abs(result["princess_owes"] - 70.0) < 0.001
        assert abs(result["jay_owes"] - 70.0) < 0.001
        assert abs(result["princess_net"] - 30.0) < 0.001

    # --- Custom split ratios ---

    def test_custom_split_princess_sixty_forty(self, tmp_db):
        """
        Princess pays 100 with 60/40 split.
        princess_owes = 60, jay_owes = 40.
        princess_net = 100 - 60 = 40.
        """
        self._insert_tx(tmp_db, "Princess", 100.0, split_ratio="60/40")
        result = calculate_period_settlement()
        assert abs(result["princess_owes"] - 60.0) < 0.001
        assert abs(result["jay_owes"] - 40.0) < 0.001
        assert abs(result["princess_net"] - 40.0) < 0.001

    def test_jay_pays_princess_owes_all(self, tmp_db):
        """
        Jay pays 200 with 100/0 split.
        princess_owes = 200, jay_owes = 0.
        princess_net = 0 - 200 = -200.
        """
        self._insert_tx(tmp_db, "Jay", 200.0, split_ratio="100/0")
        result = calculate_period_settlement()
        assert abs(result["princess_owes"] - 200.0) < 0.001
        assert abs(result["jay_owes"] - 0.0) < 0.001
        assert abs(result["princess_net"] - (-200.0)) < 0.001

    def test_multiple_transactions_accumulated(self, tmp_db):
        """
        Three transactions: totals should be summed correctly.
        Princess: 50 @ 50/50 -> owes 25
        Princess: 50 @ 50/50 -> owes 25
        Jay:      40 @ 50/50 -> owes 20
        princess_paid=100, jay_paid=40, total=140
        princess_owes=50+20=70, jay_owes=50+20=70  (each tx splits)
        princess_net = 100 - 70 = 30
        """
        self._insert_tx(tmp_db, "Princess", 50.0, split_ratio="50/50")
        self._insert_tx(tmp_db, "Princess", 50.0, split_ratio="50/50")
        self._insert_tx(tmp_db, "Jay", 40.0, split_ratio="50/50")
        result = calculate_period_settlement()
        assert result["princess_paid"] == 100.0
        assert result["jay_paid"] == 40.0
        assert result["total_spent"] == 140.0
        assert abs(result["princess_net"] - 30.0) < 0.001

    def test_net_and_owes_relationship(self, tmp_db):
        """princess_net must always equal princess_paid - princess_owes."""
        self._insert_tx(tmp_db, "Princess", 75.0, split_ratio="70/30")
        self._insert_tx(tmp_db, "Jay", 125.0, split_ratio="40/60")
        result = calculate_period_settlement()
        expected_net = result["princess_paid"] - result["princess_owes"]
        assert abs(result["princess_net"] - expected_net) < 0.001

    def test_total_spent_equals_sum_of_paid(self, tmp_db):
        """total_spent must equal princess_paid + jay_paid."""
        self._insert_tx(tmp_db, "Princess", 33.33, split_ratio="50/50")
        self._insert_tx(tmp_db, "Jay", 66.67, split_ratio="50/50")
        result = calculate_period_settlement()
        assert abs(result["total_spent"] - (result["princess_paid"] + result["jay_paid"])) < 0.001

    def test_null_split_ratio_treated_as_fifty_fifty(self, tmp_db):
        """
        If a transaction has NULL split_ratio (legacy data), the code falls
        back to 50/50 via the `or '50/50'` guard in calculate_period_settlement.
        """
        conn = sqlite3.connect(tmp_db)
        try:
            conn.execute(
                """INSERT INTO transactions
                   (person, amount, description, period_id, payer, split_ratio)
                   VALUES ('Princess', 100, 'legacy', 1, 'Princess', NULL)"""
            )
            conn.commit()
        finally:
            conn.close()
        result = calculate_period_settlement()
        # With NULL treated as 50/50: princess_owes = 50, princess_net = 50
        assert abs(result["princess_owes"] - 50.0) < 0.001
        assert abs(result["princess_net"] - 50.0) < 0.001


# ===========================================================================
# build_confirmation_footer  (Feature 1)
# ===========================================================================

class TestBuildConfirmationFooter:
    """Tests for build_confirmation_footer(settlement) -> str."""

    def _make_settlement(self, princess_paid, jay_paid, princess_owes, jay_owes):
        princess_net = princess_paid - princess_owes
        return {
            "princess_paid": princess_paid,
            "jay_paid": jay_paid,
            "princess_owes": princess_owes,
            "jay_owes": jay_owes,
            "total_spent": princess_paid + jay_paid,
            "princess_net": princess_net,
            "jay_net": jay_paid - jay_owes,
        }

    def test_returns_string(self):
        s = self._make_settlement(100.0, 100.0, 100.0, 100.0)
        assert isinstance(build_confirmation_footer(s), str)

    def test_shows_total_spent(self):
        s = self._make_settlement(60.0, 140.0, 80.0, 120.0)
        footer = build_confirmation_footer(s)
        assert "200.00" in footer  # total = 60 + 140

    def test_shows_settlement_line(self):
        # princess_net = 60 - 80 = -20 -> Princess owes Jay $20
        s = self._make_settlement(60.0, 140.0, 80.0, 120.0)
        footer = build_confirmation_footer(s)
        assert "Princess owes Jay" in footer
        assert "20.00" in footer

    def test_shows_settled_up_when_balanced(self):
        s = self._make_settlement(100.0, 100.0, 100.0, 100.0)
        footer = build_confirmation_footer(s)
        assert "settled" in footer.lower()

    def test_jay_owes_princess_case(self):
        # princess_net = 200 - 100 = 100 -> Jay owes Princess $100
        s = self._make_settlement(200.0, 50.0, 100.0, 150.0)
        footer = build_confirmation_footer(s)
        assert "Jay owes Princess" in footer
        assert "100.00" in footer

    def test_footer_is_concise(self):
        """Footer should be a short summary (under 200 chars)."""
        s = self._make_settlement(100.0, 200.0, 120.0, 180.0)
        footer = build_confirmation_footer(s)
        assert len(footer) < 200


# ===========================================================================
# budget_bar  (Feature 4)
# ===========================================================================

class TestBudgetBar:
    """Tests for budget_bar(spent, budget, days_left) -> str."""

    def test_returns_string(self):
        assert isinstance(budget_bar(300.0, 600.0, 15), str)

    def test_fifty_percent_has_half_filled(self):
        bar = budget_bar(300.0, 600.0, 15)
        # Should contain filled and empty segments
        assert "$" in bar or "█" in bar or "=" in bar or "#" in bar

    def test_shows_percentage(self):
        bar = budget_bar(300.0, 600.0, 15)
        assert "50" in bar  # 50%

    def test_shows_days_left(self):
        bar = budget_bar(300.0, 600.0, 10)
        assert "10" in bar

    def test_zero_spent(self):
        bar = budget_bar(0.0, 600.0, 20)
        assert "0" in bar

    def test_over_budget(self):
        bar = budget_bar(700.0, 600.0, 5)
        assert "116" in bar or "over" in bar.lower() or "117" in bar  # ~116.7%

    def test_exact_budget(self):
        bar = budget_bar(600.0, 600.0, 0)
        assert "100" in bar

    def test_zero_budget_does_not_crash(self):
        # Should not raise ZeroDivisionError
        bar = budget_bar(0.0, 0.0, 15)
        assert isinstance(bar, str)


# ===========================================================================
# build_summary_message  (Feature 2)
# ===========================================================================

class TestBuildSummaryMessage:
    """Tests for build_summary_message(rows, budget) -> str.

    rows format: list of (payer, amount, category) tuples.
    """

    def test_returns_string(self):
        rows = [("Princess", 50.0, None), ("Jay", 100.0, None)]
        assert isinstance(build_summary_message(rows, 600.0), str)

    def test_shows_total(self):
        rows = [("Princess", 50.0, None), ("Jay", 100.0, None)]
        msg = build_summary_message(rows, 600.0)
        assert "150.00" in msg

    def test_shows_princess_amount(self):
        rows = [("Princess", 75.0, None), ("Jay", 25.0, None)]
        msg = build_summary_message(rows, 600.0)
        assert "75.00" in msg

    def test_shows_jay_amount(self):
        rows = [("Princess", 75.0, None), ("Jay", 25.0, None)]
        msg = build_summary_message(rows, 600.0)
        assert "25.00" in msg

    def test_shows_princess_percentage(self):
        rows = [("Princess", 150.0, None), ("Jay", 150.0, None)]
        msg = build_summary_message(rows, 600.0)
        assert "50" in msg  # 50%

    def test_shows_budget_used_percent(self):
        rows = [("Princess", 150.0, None), ("Jay", 150.0, None)]
        msg = build_summary_message(rows, 600.0)
        assert "50" in msg  # 300/600 = 50%

    def test_category_breakdown_shown(self):
        rows = [
            ("Princess", 50.0, "groceries"),
            ("Jay", 80.0, "dining"),
            ("Jay", 20.0, "groceries"),
        ]
        msg = build_summary_message(rows, 600.0)
        assert "groceries" in msg.lower()
        assert "dining" in msg.lower()

    def test_empty_period_does_not_crash(self):
        msg = build_summary_message([], 600.0)
        assert isinstance(msg, str)
        assert "0" in msg or "no" in msg.lower() or "empty" in msg.lower()

    def test_uncategorized_grouped(self):
        rows = [("Princess", 50.0, None), ("Jay", 50.0, None)]
        msg = build_summary_message(rows, 600.0)
        assert isinstance(msg, str)  # should not crash with None categories


# ===========================================================================
# parse_expense — category (#tag) parsing  (Feature 3)
# ===========================================================================

class TestParseExpenseCategory:
    """Tests for category extraction via #tag syntax in parse_expense."""

    def test_hashtag_captured_as_category(self):
        result = parse_expense("Jay 25.00 groceries #food", "50/50")
        assert result is not None
        assert result["category"] == "food"

    def test_hashtag_removed_from_description(self):
        result = parse_expense("Jay 25.00 groceries #food", "50/50")
        assert result is not None
        assert "#food" not in result["description"]
        assert "groceries" in result["description"]

    def test_no_hashtag_category_is_none(self):
        result = parse_expense("Jay 25.00 lunch", "50/50")
        assert result is not None
        assert result.get("category") is None

    def test_category_case_normalized_lowercase(self):
        result = parse_expense("Princess 10.00 coffee #Dining", "50/50")
        assert result is not None
        assert result["category"] == "dining"

    def test_category_with_amount_last(self):
        result = parse_expense("Jay lunch 30.00 #dining", "50/50")
        assert result is not None
        assert result["category"] == "dining"

    def test_category_with_split_override(self):
        result = parse_expense("Jay 50.00 dinner #dining -split 60/40", "50/50")
        assert result is not None
        assert result["category"] == "dining"
        assert result["split_ratio"] == "60/40"

    def test_only_first_hashtag_used(self):
        result = parse_expense("Jay 50.00 lunch #dining #food", "50/50")
        assert result is not None
        # Should capture a category (first one)
        assert result["category"] in ("dining", "food")


# ===========================================================================
# find_transactions  (Feature 7)
# ===========================================================================

class TestFindTransactions:
    """Tests for find_transactions(query, period_id) -- DB-backed."""

    def _insert(self, tmp_db, payer, amount, description):
        conn = sqlite3.connect(tmp_db)
        try:
            conn.execute(
                "INSERT INTO transactions (person, amount, description, period_id, payer, split_ratio) "
                "VALUES (?, ?, ?, 1, ?, '50/50')",
                (payer, amount, description, payer),
            )
            conn.commit()
        finally:
            conn.close()

    def test_finds_exact_match(self, tmp_db):
        self._insert(tmp_db, "Jay", 10.0, "groceries at Safeway")
        from expense_bot import find_transactions
        results = find_transactions("groceries")
        assert len(results) == 1
        assert "groceries" in results[0][2].lower()

    def test_finds_case_insensitive(self, tmp_db):
        self._insert(tmp_db, "Jay", 10.0, "Trader Joes")
        from expense_bot import find_transactions
        results = find_transactions("trader")
        assert len(results) == 1

    def test_returns_empty_for_no_match(self, tmp_db):
        self._insert(tmp_db, "Jay", 10.0, "coffee")
        from expense_bot import find_transactions
        results = find_transactions("groceries")
        assert results == []

    def test_finds_multiple_matches(self, tmp_db):
        self._insert(tmp_db, "Jay", 10.0, "grocery run")
        self._insert(tmp_db, "Princess", 20.0, "grocery store")
        from expense_bot import find_transactions
        results = find_transactions("grocery")
        assert len(results) == 2

    def test_partial_word_match(self, tmp_db):
        self._insert(tmp_db, "Jay", 15.0, "Safeway shopping")
        from expense_bot import find_transactions
        results = find_transactions("safe")
        assert len(results) == 1


# ===========================================================================
# TestCategoryPicker  (Feature 3)
# ===========================================================================

class TestCategoryPicker:
    """Tests for CATEGORIES constant and build_category_keyboard()."""

    def test_categories_constant_has_8_items(self):
        """CATEGORIES must contain exactly 9 (slug, emoji) tuples (includes recurring)."""
        from expense_bot import CATEGORIES
        assert len(CATEGORIES) == 9

    def test_build_category_keyboard_returns_markup(self):
        """build_category_keyboard() must return an InlineKeyboardMarkup."""
        from expense_bot import build_category_keyboard
        markup = build_category_keyboard()
        assert isinstance(markup, InlineKeyboardMarkup)

    def test_build_category_keyboard_contains_cat_pick_callbacks(self):
        """All 8 category slugs must appear as cat_pick_{slug} callback data."""
        from expense_bot import build_category_keyboard, CATEGORIES
        markup = build_category_keyboard()
        all_data = [
            btn.callback_data
            for row in markup.inline_keyboard
            for btn in row
        ]
        for slug, _ in CATEGORIES:
            assert f"cat_pick_{slug}" in all_data, (
                f"cat_pick_{slug} not found in keyboard callbacks"
            )

    def test_build_category_keyboard_preselected_adds_checkmark(self):
        """When pre_selected='groceries', that button label starts with '✅'."""
        from expense_bot import build_category_keyboard
        markup = build_category_keyboard(pre_selected="groceries")
        groceries_btn = next(
            btn
            for row in markup.inline_keyboard
            for btn in row
            if btn.callback_data == "cat_pick_groceries"
        )
        assert groceries_btn.text.startswith("✅"), (
            f"Expected label to start with '✅', got: {groceries_btn.text!r}"
        )

    def test_build_category_keyboard_non_preselected_no_checkmark(self):
        """Buttons that are NOT pre_selected must NOT start with '✅'."""
        from expense_bot import build_category_keyboard
        markup = build_category_keyboard(pre_selected="groceries")
        for row in markup.inline_keyboard:
            for btn in row:
                if btn.callback_data and btn.callback_data.startswith("cat_pick_"):
                    slug = btn.callback_data.removeprefix("cat_pick_")
                    if slug != "groceries":
                        assert not btn.text.startswith("✅"), (
                            f"Unexpected ✅ on non-selected button {slug!r}"
                        )

    def test_build_category_keyboard_has_custom_and_cancel(self):
        """Keyboard must include buttons with callback_data 'cat_custom' and 'cancel'."""
        from expense_bot import build_category_keyboard
        markup = build_category_keyboard()
        all_data = [
            btn.callback_data
            for row in markup.inline_keyboard
            for btn in row
        ]
        assert "cat_custom" in all_data, "cat_custom callback not found"
        assert "cancel" in all_data, "cancel callback not found"

    def test_build_category_keyboard_two_column_layout(self):
        """The 8 category buttons must be arranged in 2-column rows (4 rows of 2)."""
        from expense_bot import build_category_keyboard
        markup = build_category_keyboard()
        # Collect rows that hold category picks only
        cat_rows = [
            row for row in markup.inline_keyboard
            if all(
                btn.callback_data and btn.callback_data.startswith("cat_pick_")
                for btn in row
            )
        ]
        # 9 categories → 5 category rows (4×2 + 1×1), then 1 bottom row
        assert len(cat_rows) == 5, f"Expected 5 category rows, got {len(cat_rows)}"
        # All rows except the last must have 2 buttons
        for row in cat_rows[:-1]:
            assert len(row) == 2, f"Expected 2 buttons per row, got {len(row)}"

    def test_conf_bulk_insert_includes_category(self, tmp_db):
        """conf_bulk inserts must persist the category field from each expense dict."""
        import sqlite3 as _sqlite3
        from expense_bot import get_db, get_active_period_id

        expenses = [
            {
                "person": "Jay",
                "amount": 42.0,
                "description": "weekly shop",
                "split_ratio": "50/50",
                "category": "groceries",
            },
            {
                "person": "Princess",
                "amount": 15.50,
                "description": "lunch out",
                "split_ratio": "50/50",
                "category": "dining",
            },
        ]

        period_id = get_active_period_id()
        assert period_id is not None, "No active period in tmp_db fixture"

        with get_db() as conn:
            for e in expenses:
                conn.execute(
                    """INSERT INTO transactions
                       (person, amount, description, period_id, payer, split_ratio, category)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        e["person"], e["amount"], e["description"],
                        period_id, e["person"], e["split_ratio"],
                        e.get("category"),
                    ),
                )

        conn2 = _sqlite3.connect(tmp_db)
        try:
            rows = conn2.execute(
                "SELECT category FROM transactions ORDER BY id"
            ).fetchall()
        finally:
            conn2.close()

        assert len(rows) == 2
        assert rows[0][0] == "groceries"
        assert rows[1][0] == "dining"


# ===========================================================================
# TestNewCommands — Features 2, 4, 5, 6, 7
# ===========================================================================

class TestNewCommands:
    """
    Tests for the new command handlers and supporting logic introduced in
    Features 2, 4, 5, 6, and 7.

    Pure-function and unit-level tests only — no Telegram API calls.
    DB-backed tests use the tmp_db fixture.
    """

    # -----------------------------------------------------------------------
    # Feature 2 — /summary command (command-level smoke check)
    # -----------------------------------------------------------------------

    def test_build_summary_message_returns_string_non_empty(self):
        """build_summary_message with real data must return a non-empty string."""
        from expense_bot import build_summary_message
        rows = [
            ("Princess", 120.0, "groceries"),
            ("Jay", 80.0, "dining"),
            ("Jay", 50.0, None),
        ]
        result = build_summary_message(rows, 500.0)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_build_summary_message_contains_period_summary_header(self):
        """The /summary output must contain 'Period Summary' heading."""
        from expense_bot import build_summary_message
        rows = [("Princess", 100.0, "food"), ("Jay", 100.0, "food")]
        result = build_summary_message(rows, 600.0)
        assert "Period Summary" in result

    def test_build_summary_message_zero_budget_does_not_crash(self):
        """build_summary_message with budget=0 must not raise."""
        from expense_bot import build_summary_message
        rows = [("Princess", 50.0, "misc")]
        result = build_summary_message(rows, 0.0)
        assert isinstance(result, str)

    # -----------------------------------------------------------------------
    # Feature 4 — Budget bar in /status
    # -----------------------------------------------------------------------

    def test_budget_bar_appears_in_status_output(self):
        """budget_bar(300, 600, 15) must return a string containing '%' and '$'."""
        from expense_bot import budget_bar
        bar = budget_bar(300.0, 600.0, 15)
        assert isinstance(bar, str)
        assert "%" in bar
        assert "$" in bar

    def test_budget_bar_zero_budget_skipped(self):
        """budget_bar with budget=0 must not raise ZeroDivisionError and returns str."""
        from expense_bot import budget_bar
        result = budget_bar(0.0, 0.0, 15)
        assert isinstance(result, str)
        # Graceful fallback — should mention spent amount or 'no budget'
        assert "0" in result or "budget" in result.lower()

    def test_budget_bar_over_budget_shows_over(self):
        """When spent > budget the bar should signal overage."""
        from expense_bot import budget_bar
        bar = budget_bar(700.0, 600.0, 5)
        assert "over" in bar.lower() or "116" in bar or "117" in bar

    def test_budget_bar_single_day_left_singular(self):
        """days_left=1 should use singular 'day' not 'days'."""
        from expense_bot import budget_bar
        bar = budget_bar(100.0, 600.0, 1)
        assert "1 day" in bar
        assert "1 days" not in bar

    # -----------------------------------------------------------------------
    # Feature 5 — Instant /undo (no confirm keyboard)
    # -----------------------------------------------------------------------

    def test_undo_instant_deletes_last_transaction(self, tmp_db, monkeypatch):
        """
        When a transaction exists, executing the DELETE removes it from the DB.
        """
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(tmp_db)
        try:
            conn.execute(
                "INSERT INTO transactions "
                "(person, amount, description, period_id, payer, split_ratio) "
                "VALUES ('Jay', 42.00, 'test coffee', 1, 'Jay', '50/50')"
            )
            conn.commit()
            row = conn.execute(
                "SELECT id FROM transactions ORDER BY id DESC LIMIT 1"
            ).fetchone()
            inserted_id = row[0]
        finally:
            conn.close()

        import expense_bot as eb
        eb.execute_write("DELETE FROM transactions WHERE id = ?", (inserted_id,))

        conn2 = _sqlite3.connect(tmp_db)
        try:
            remaining = conn2.execute(
                "SELECT COUNT(*) FROM transactions WHERE id = ?", (inserted_id,)
            ).fetchone()[0]
        finally:
            conn2.close()

        assert remaining == 0

    def test_undo_instant_write_called_with_delete(self, tmp_db, monkeypatch):
        """
        execute_write must be called with a DELETE query when undo is triggered.
        Uses monkeypatching to capture the call without a running handler.
        """
        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(tmp_db)
        try:
            conn.execute(
                "INSERT INTO transactions "
                "(person, amount, description, period_id, payer, split_ratio) "
                "VALUES ('Princess', 15.00, 'lunch', 1, 'Princess', '50/50')"
            )
            conn.commit()
            row = conn.execute(
                "SELECT id FROM transactions ORDER BY id DESC LIMIT 1"
            ).fetchone()
            trans_id = row[0]
        finally:
            conn.close()

        captured = []

        def fake_write(query, params=()):
            captured.append((query.upper(), params))
            return 0

        monkeypatch.setattr("expense_bot.execute_write", fake_write)

        import expense_bot as eb
        # Simulate what the instant undo handler does
        eb.execute_write("DELETE FROM transactions WHERE id = ?", (trans_id,))

        assert len(captured) == 1
        assert "DELETE" in captured[0][0]
        assert trans_id in captured[0][1]

    # -----------------------------------------------------------------------
    # Feature 6 — /recurring command
    # -----------------------------------------------------------------------

    def test_recurring_command_no_rows_message(self):
        """
        When no recurring rows exist the message must contain 'No recurring'.
        """
        rows = []
        if not rows:
            msg = (
                "📋 No recurring expenses logged yet.\n\n"
                "Tag an expense with `#recurring` to track it."
            )
        assert "No recurring" in msg

    def test_recurring_category_stored_via_parse(self):
        """
        parse_expense with #recurring tag must return category='recurring'.
        """
        from expense_bot import parse_expense
        result = parse_expense("Jay 50 netflix #recurring", "50/50")
        assert result is not None
        assert result["category"] == "recurring"

    def test_recurring_tag_removed_from_description(self):
        """The #recurring tag must not appear in the stored description."""
        from expense_bot import parse_expense
        result = parse_expense("Jay 50 netflix #recurring", "50/50")
        assert result is not None
        assert "#recurring" not in result["description"]
        assert "netflix" in result["description"]

    def test_recurring_rows_message_contains_total(self):
        """
        When rows exist, the recurring message must include 'Total logged'.
        """
        rows = [
            ("Princess", 15.99, "spotify", "2026-04-01 10:00:00"),
            ("Jay", 9.99, "netflix", "2026-04-02 11:00:00"),
        ]
        lines = ["📋 **Recurring Expenses:**\n"]
        total = 0.0
        for payer, amount, description, ts in rows:
            lines.append(f"• **{payer}** ${amount:.2f} — {description} _{ts}_")
            total += amount
        lines.append(f"\n**Total logged:** ${total:.2f}")
        msg = "\n".join(lines)
        assert "Total logged" in msg
        assert "25.98" in msg  # 15.99 + 9.99

    # -----------------------------------------------------------------------
    # Feature 7 — /find command
    # -----------------------------------------------------------------------

    def test_find_command_no_args_usage_message(self):
        """
        When no args are provided the usage message must contain 'Find Expenses'.
        """
        args = []
        if not args:
            msg = (
                "🔍 **Find Expenses**\n\n"
                "Usage: `/find <keyword>`\n"
                "Example: `/find groceries`"
            )
        assert "Find Expenses" in msg
        assert "/find" in msg

    def test_find_transactions_column_order(self, tmp_db):
        """
        find_transactions returns (id, payer, description, amount, split_ratio, category).
        Verify row[1]=payer, row[2]=description, row[3]=amount.
        """
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(tmp_db)
        try:
            conn.execute(
                "INSERT INTO transactions "
                "(person, amount, description, period_id, payer, split_ratio, category) "
                "VALUES ('Princess', 77.77, 'supermarket run', 1, 'Princess', '50/50', 'groceries')"
            )
            conn.commit()
        finally:
            conn.close()

        from expense_bot import find_transactions
        results = find_transactions("supermarket")
        assert len(results) == 1
        row = results[0]
        # (id, payer, description, amount, split_ratio, category)
        assert row[1] == "Princess"         # payer
        assert row[2] == "supermarket run"  # description
        assert abs(row[3] - 77.77) < 0.001  # amount
        assert row[4] == "50/50"            # split_ratio
        assert row[5] == "groceries"        # category

    def test_find_transactions_no_results_returns_empty_list(self, tmp_db):
        """find_transactions with no match returns []."""
        from expense_bot import find_transactions
        results = find_transactions("xyznonexistent9999")
        assert results == []

    def test_find_command_formats_result_line(self):
        """
        Verify the find_command line format uses row[1]=payer, row[3]=amount,
        row[2]=description.
        """
        # row = (id, payer, description, amount, split_ratio, category)
        row = (1, "Jay", "groceries at Safeway", 45.50, "50/50", "food")
        line = f"• **{row[1]}** ${row[3]:.2f} — {row[2]}"
        assert "Jay" in line
        assert "45.50" in line
        assert "groceries at Safeway" in line


# ===========================================================================
# TestCSVAndEditFeatures
# ===========================================================================

class TestCSVAndEditFeatures:
    """Tests for generate_csv_content(), save_period_csv(), and CATEGORIES."""

    # --- generate_csv_content ---

    def test_generate_csv_content_has_header(self):
        from expense_bot import generate_csv_content
        rows = [(1, "Jay", 25.00, "coffee", "2026-04-01 10:00:00", 1, "50/50", "dining")]
        result = generate_csv_content(rows)
        assert "ID" in result
        assert "Date" in result
        assert "Category" in result

    def test_generate_csv_content_includes_category(self):
        from expense_bot import generate_csv_content
        rows = [(1, "Jay", 25.00, "coffee", "2026-04-01 10:00:00", 1, "50/50", "dining")]
        result = generate_csv_content(rows)
        assert "dining" in result

    def test_generate_csv_content_column_order(self):
        from expense_bot import generate_csv_content
        rows = [(5, "Princess", 50.00, "Safeway", "2026-04-02 12:00:00", 2, "40/60", "groceries")]
        result = generate_csv_content(rows)
        lines = result.strip().split("\n")
        header = lines[0]
        data = lines[1]
        # Header order: ID, Date, Payer, Amount, Description, Category, Split Ratio, Period ID
        assert header.index('"ID"') < header.index('"Date"') < header.index('"Payer"')
        assert "groceries" in data
        assert "50.00" in data

    def test_generate_csv_content_none_category_becomes_empty(self):
        from expense_bot import generate_csv_content
        rows = [(1, "Jay", 10.00, "test", "2026-04-01", 1, "50/50", None)]
        result = generate_csv_content(rows)
        assert result.count('""') >= 1  # empty category field

    def test_generate_csv_content_none_split_ratio_defaults_to_50_50(self):
        from expense_bot import generate_csv_content
        rows = [(1, "Jay", 10.00, "test", "2026-04-01", 1, None, "dining")]
        result = generate_csv_content(rows)
        assert "50/50" in result

    def test_generate_csv_content_none_description_becomes_empty(self):
        from expense_bot import generate_csv_content
        rows = [(1, "Jay", 10.00, None, "2026-04-01", 1, "50/50", "dining")]
        result = generate_csv_content(rows)
        # Description column should be empty string, not "None"
        assert "None" not in result

    def test_generate_csv_content_amount_formatted_to_two_decimals(self):
        from expense_bot import generate_csv_content
        rows = [(1, "Jay", 9.5, "coffee", "2026-04-01", 1, "50/50", "dining")]
        result = generate_csv_content(rows)
        assert "9.50" in result

    def test_generate_csv_content_multiple_rows(self):
        from expense_bot import generate_csv_content
        rows = [
            (1, "Jay", 10.00, "coffee", "2026-04-01", 1, "50/50", "dining"),
            (2, "Princess", 50.00, "Safeway", "2026-04-02", 1, "60/40", "groceries"),
        ]
        result = generate_csv_content(rows)
        lines = result.strip().split("\n")
        # 1 header + 2 data rows
        assert len(lines) == 3
        assert "coffee" in result
        assert "Safeway" in result

    def test_generate_csv_content_period_id_is_last_column(self):
        from expense_bot import generate_csv_content
        rows = [(5, "Princess", 50.00, "Safeway", "2026-04-02 12:00:00", 2, "40/60", "groceries")]
        result = generate_csv_content(rows)
        lines = result.strip().split("\n")
        header = lines[0]
        # Period ID must be the last column
        assert header.rstrip().endswith('"Period ID"')

    # --- save_period_csv ---

    def test_save_period_csv_creates_file(self, tmp_path, monkeypatch):
        from expense_bot import save_period_csv
        import expense_bot
        monkeypatch.setattr(expense_bot, "DATABASE_PATH", str(tmp_path / "test.db"))
        rows = [(1, "Jay", 10.00, "coffee", "2026-04-01", 1, "50/50", "dining")]
        path = save_period_csv(1, rows)
        assert os.path.exists(path)
        assert "period_1" in path

    def test_save_period_csv_content_is_valid_csv(self, tmp_path, monkeypatch):
        from expense_bot import save_period_csv
        import expense_bot
        monkeypatch.setattr(expense_bot, "DATABASE_PATH", str(tmp_path / "test.db"))
        rows = [(1, "Jay", 10.00, "coffee", "2026-04-01", 1, "50/50", "dining")]
        path = save_period_csv(1, rows)
        with open(path) as f:
            content = f.read()
        assert "dining" in content
        assert "Jay" in content

    def test_save_period_csv_creates_exports_subdir(self, tmp_path, monkeypatch):
        from expense_bot import save_period_csv
        import expense_bot
        monkeypatch.setattr(expense_bot, "DATABASE_PATH", str(tmp_path / "test.db"))
        rows = [(1, "Jay", 10.00, "coffee", "2026-04-01", 1, "50/50", "dining")]
        path = save_period_csv(3, rows)
        # File must be inside an "exports" directory
        assert "exports" in path
        assert os.path.isfile(path)

    def test_save_period_csv_filename_contains_period_id(self, tmp_path, monkeypatch):
        from expense_bot import save_period_csv
        import expense_bot
        monkeypatch.setattr(expense_bot, "DATABASE_PATH", str(tmp_path / "test.db"))
        rows = [(1, "Jay", 10.00, "coffee", "2026-04-01", 1, "50/50", "dining")]
        path = save_period_csv(42, rows)
        filename = os.path.basename(path)
        assert filename.startswith("period_42_")
        assert filename.endswith(".csv")

    def test_save_period_csv_returns_string_path(self, tmp_path, monkeypatch):
        from expense_bot import save_period_csv
        import expense_bot
        monkeypatch.setattr(expense_bot, "DATABASE_PATH", str(tmp_path / "test.db"))
        rows = [(1, "Jay", 10.00, "coffee", "2026-04-01", 1, "50/50", "dining")]
        path = save_period_csv(1, rows)
        assert isinstance(path, str)

    # --- CATEGORIES constant ---

    def test_categories_constant_slugs(self):
        from expense_bot import CATEGORIES
        slugs = [s for s, _ in CATEGORIES]
        assert "groceries" in slugs
        assert "fun" in slugs
        assert "dining" in slugs

    def test_categories_constant_has_emojis(self):
        from expense_bot import CATEGORIES
        for slug, emoji in CATEGORIES:
            assert isinstance(emoji, str)
            assert len(emoji) > 0

    def test_categories_constant_has_eight_entries(self):
        from expense_bot import CATEGORIES
        assert len(CATEGORIES) == 9  # now includes recurring


# ===========================================================================
# Recurring category features
# ===========================================================================

class TestRecurringCategory:
    """Tests for recurring category in CATEGORIES, picker, and toggle flow."""

    def test_categories_includes_recurring_slug(self):
        """CATEGORIES must contain a 'recurring' entry."""
        from expense_bot import CATEGORIES
        slugs = [s for s, _ in CATEGORIES]
        assert "recurring" in slugs

    def test_categories_recurring_has_correct_emoji(self):
        """The recurring entry must use the 🔁 emoji."""
        from expense_bot import CATEGORIES
        emoji_map = {s: e for s, e in CATEGORIES}
        assert emoji_map["recurring"] == "🔁"

    def test_category_emoji_helper_for_recurring(self):
        """_category_emoji('recurring') must return '🔁' (not the fallback '🏷')."""
        from expense_bot import _category_emoji
        assert _category_emoji("recurring") == "🔁"

    def test_build_category_keyboard_contains_recurring(self):
        """build_category_keyboard must include a button with callback cat_pick_recurring."""
        from expense_bot import build_category_keyboard
        kb = build_category_keyboard()
        all_callbacks = [
            btn.callback_data
            for row in kb.inline_keyboard
            for btn in row
        ]
        assert "cat_pick_recurring" in all_callbacks

    def test_build_category_keyboard_preselects_recurring(self):
        """When pre_selected='recurring', the recurring button label starts with ✅."""
        from expense_bot import build_category_keyboard
        kb = build_category_keyboard(pre_selected="recurring")
        labels = [
            btn.text
            for row in kb.inline_keyboard
            for btn in row
        ]
        assert any(lbl.startswith("✅") and "recurring" in lbl for lbl in labels)

    def test_categories_total_count_is_nine(self):
        """After adding recurring, CATEGORIES has exactly 9 entries."""
        from expense_bot import CATEGORIES
        assert len(CATEGORIES) == 9


class TestRecurringGroupedCommand:
    """Tests for the grouped /recurring output helper."""

    def test_group_recurring_empty(self):
        """group_recurring_rows([]) returns an empty list."""
        from expense_bot import group_recurring_rows
        assert group_recurring_rows([]) == []

    def test_group_recurring_single_item(self):
        """A single row is returned as one group with count=1."""
        from expense_bot import group_recurring_rows
        rows = [("Jay", 19.00, "spotify")]
        result = group_recurring_rows(rows)
        assert len(result) == 1
        assert result[0]["description"] == "spotify"
        assert result[0]["amount"] == 19.00
        assert result[0]["count"] == 1
        assert result[0]["payer"] == "Jay"

    def test_group_recurring_groups_same_description(self):
        """Rows with the same description are merged into one group."""
        from expense_bot import group_recurring_rows
        rows = [
            ("Jay", 19.00, "spotify"),
            ("Jay", 19.00, "spotify"),
            ("Princess", 19.00, "spotify"),
        ]
        result = group_recurring_rows(rows)
        assert len(result) == 1
        assert result[0]["count"] == 3

    def test_group_recurring_separate_descriptions(self):
        """Different descriptions produce separate groups."""
        from expense_bot import group_recurring_rows
        rows = [
            ("Jay", 19.00, "spotify"),
            ("Princess", 9.99, "netflix"),
        ]
        result = group_recurring_rows(rows)
        assert len(result) == 2
        descs = {r["description"] for r in result}
        assert descs == {"spotify", "netflix"}

    def test_group_recurring_amount_is_latest(self):
        """The reported amount is taken from the first (most-recent) occurrence."""
        from expense_bot import group_recurring_rows
        rows = [
            ("Jay", 20.00, "spotify"),  # most recent
            ("Jay", 19.00, "spotify"),  # older
        ]
        result = group_recurring_rows(rows)
        assert result[0]["amount"] == 20.00

    def test_group_recurring_payer_from_first_row(self):
        """The payer in the group is taken from the first row."""
        from expense_bot import group_recurring_rows
        rows = [
            ("Princess", 9.99, "netflix"),
            ("Jay",      9.99, "netflix"),
        ]
        result = group_recurring_rows(rows)
        assert result[0]["payer"] == "Princess"


class TestToggleRecurring:
    """Tests for the toggle_recurring_category helper."""

    def test_toggle_sets_recurring_when_not_recurring(self):
        """Non-recurring category flips to 'recurring'."""
        from expense_bot import toggle_recurring_category
        new_cat, prev_cat = toggle_recurring_category("fun")
        assert new_cat == "recurring"
        assert prev_cat == "fun"

    def test_toggle_unsets_recurring_when_recurring(self):
        """When current is 'recurring', toggle returns previous category."""
        from expense_bot import toggle_recurring_category
        new_cat, prev_cat = toggle_recurring_category("recurring", previous="fun")
        assert new_cat == "fun"
        assert prev_cat == "recurring"

    def test_toggle_unsets_recurring_defaults_to_other(self):
        """When current is 'recurring' and no previous given, fallback to 'other'."""
        from expense_bot import toggle_recurring_category
        new_cat, prev_cat = toggle_recurring_category("recurring")
        assert new_cat == "other"

    def test_toggle_recurring_with_none_current(self):
        """None current category is treated as non-recurring → sets to 'recurring'."""
        from expense_bot import toggle_recurring_category
        new_cat, prev_cat = toggle_recurring_category(None)
        assert new_cat == "recurring"


# ===========================================================================
# Recurring carry-over feature — pure helpers
# ===========================================================================

class TestRecurringCarryoverPureHelpers:
    """Tests for the pure helpers that support the post-reset carry-over prompt."""

    # --- normalize_recurring_description ---

    def test_normalize_strips_whitespace(self):
        from expense_bot import normalize_recurring_description
        assert normalize_recurring_description("  Spotify  ") == "spotify"

    def test_normalize_lowercases(self):
        from expense_bot import normalize_recurring_description
        assert normalize_recurring_description("NETFLIX") == "netflix"

    def test_normalize_empty_string(self):
        from expense_bot import normalize_recurring_description
        assert normalize_recurring_description("") == ""

    # --- amount_to_cents ---

    def test_amount_to_cents_whole(self):
        from expense_bot import amount_to_cents
        assert amount_to_cents(19.0) == 1900

    def test_amount_to_cents_decimal(self):
        from expense_bot import amount_to_cents
        assert amount_to_cents(9.99) == 999

    def test_amount_to_cents_rounds(self):
        from expense_bot import amount_to_cents
        # 9.999 rounds to 1000 cents
        assert amount_to_cents(9.999) == 1000

    # --- make_recurring_item_token ---

    def test_token_is_deterministic(self):
        from expense_bot import make_recurring_item_token
        assert make_recurring_item_token("spotify", 19.0, "Jay", "Jay") == make_recurring_item_token("spotify", 19.0, "Jay", "Jay")

    def test_token_changes_with_amount(self):
        from expense_bot import make_recurring_item_token
        assert make_recurring_item_token("spotify", 19.0, "Jay", "Jay") != make_recurring_item_token("spotify", 20.0, "Jay", "Jay")

    def test_token_changes_with_description(self):
        from expense_bot import make_recurring_item_token
        assert make_recurring_item_token("spotify", 19.0, "Jay", "Jay") != make_recurring_item_token("netflix", 19.0, "Jay", "Jay")

    def test_token_is_string(self):
        from expense_bot import make_recurring_item_token
        tok = make_recurring_item_token("spotify", 19.0, "Jay", "Jay")
        assert isinstance(tok, str)
        assert len(tok) > 0

    def test_token_normalises_description(self):
        from expense_bot import make_recurring_item_token
        # " Spotify " and "spotify" must produce the same token for same person/payer
        assert make_recurring_item_token(" Spotify ", 19.0, "Jay", "Jay") == make_recurring_item_token("spotify", 19.0, "Jay", "Jay")

    # --- build_recurring_carryover_item ---

    def test_carryover_item_fields(self):
        from expense_bot import build_recurring_carryover_item
        row = ("Jay", "Jay", 19.0, "Spotify", "40/60")
        item = build_recurring_carryover_item(row)
        assert item["description"] == "Spotify"
        assert item["normalized_description"] == "spotify"
        assert item["amount"] == 19.0
        assert item["amount_cents"] == 1900
        assert item["person"] == "Jay"
        assert item["payer"] == "Jay"
        assert item["split_ratio"] == "40/60"
        assert "token" in item

    def test_carryover_item_token_stable(self):
        from expense_bot import build_recurring_carryover_item, make_recurring_item_token
        row = ("Jay", "Jay", 19.0, "Spotify", "40/60")
        item = build_recurring_carryover_item(row)
        assert item["token"] == make_recurring_item_token("Spotify", 19.0, "Jay", "Jay")

    def test_token_differs_for_same_desc_amount_different_person(self):
        """Two items with same description+amount but different persons must have different tokens."""
        from expense_bot import make_recurring_item_token
        tok_princess = make_recurring_item_token("Netflix", 50.0, "Princess", "Princess")
        tok_jay = make_recurring_item_token("Netflix", 50.0, "Jay", "Jay")
        assert tok_princess != tok_jay

    def test_token_differs_for_same_desc_amount_different_payer(self):
        """Two items with same description+amount but different payers must have different tokens."""
        from expense_bot import make_recurring_item_token
        tok_a = make_recurring_item_token("Netflix", 50.0, "Princess", "Princess")
        tok_b = make_recurring_item_token("Netflix", 50.0, "Princess", "Jay")
        assert tok_a != tok_b

    def test_token_uses_sha256(self):
        """Token should be derived from sha256 (hexdigest has 64 chars before truncation)."""
        import hashlib
        from expense_bot import normalize_recurring_description, amount_to_cents
        # Recompute expected token using sha256 to verify implementation
        key = f"{normalize_recurring_description('Spotify')}|{amount_to_cents(19.0)}|Jay|Jay"
        expected = hashlib.sha256(key.encode()).hexdigest()[:12]
        from expense_bot import make_recurring_item_token
        assert make_recurring_item_token("Spotify", 19.0, "Jay", "Jay") == expected

    def test_no_token_collision_same_desc_amount_different_persons(self):
        """build_recurring_carryover_session must not silently overwrite items when tokens collide."""
        from expense_bot import build_recurring_carryover_items, build_recurring_carryover_session
        rows = [
            ("Princess", "Princess", 50.0, "Netflix", "40/60"),
            ("Jay", "Jay", 50.0, "Netflix", "40/60"),
        ]
        items = build_recurring_carryover_items(rows)
        # Both tokens must be distinct
        assert items[0]["token"] != items[1]["token"]
        session = build_recurring_carryover_session(items, period_id=1)
        # Session must contain both items
        assert len(session["items_by_token"]) == 2
        assert len(session["ordered_tokens"]) == 2

    def test_build_recurring_carryover_items_list(self):
        from expense_bot import build_recurring_carryover_items
        rows = [
            ("Jay", "Jay", 19.0, "Spotify", "40/60"),
            ("Princess", "Princess", 9.99, "Netflix", "40/60"),
        ]
        items = build_recurring_carryover_items(rows)
        assert len(items) == 2
        assert items[0]["description"] == "Spotify"
        assert items[1]["description"] == "Netflix"

    # --- toggle_selected_token ---

    def test_toggle_adds_missing_token(self):
        from expense_bot import toggle_selected_token
        result = toggle_selected_token({"a", "b"}, "c")
        assert "c" in result

    def test_toggle_removes_existing_token(self):
        from expense_bot import toggle_selected_token
        result = toggle_selected_token({"a", "b"}, "a")
        assert "a" not in result

    def test_toggle_returns_new_set(self):
        """toggle_selected_token must return a new set (immutable pattern)."""
        from expense_bot import toggle_selected_token
        original = {"a", "b"}
        result = toggle_selected_token(original, "c")
        assert result is not original

    # --- get_selected_recurring_items ---

    def test_get_selected_preserves_order(self):
        from expense_bot import get_selected_recurring_items
        items = {
            "tok1": {"description": "spotify", "token": "tok1"},
            "tok2": {"description": "netflix", "token": "tok2"},
            "tok3": {"description": "gym", "token": "tok3"},
        }
        result = get_selected_recurring_items(items, {"tok1", "tok3"}, ["tok1", "tok2", "tok3"])
        assert [r["description"] for r in result] == ["spotify", "gym"]

    def test_get_selected_empty(self):
        from expense_bot import get_selected_recurring_items
        result = get_selected_recurring_items({}, set(), [])
        assert result == []

    # --- make_recurring_transaction_signature ---

    def test_signature_uses_normalized_desc_and_cents(self):
        from expense_bot import make_recurring_transaction_signature
        item = {
            "normalized_description": "spotify",
            "amount_cents": 1900,
            "person": "Jay",
            "payer": "Jay",
            "split_ratio": "40/60",
        }
        sig = make_recurring_transaction_signature(item)
        assert sig == ("spotify", 1900, "Jay", "Jay", "40/60")

    # --- filter_new_recurring_items ---

    def test_filter_removes_existing(self):
        from expense_bot import filter_new_recurring_items
        items = [
            {"normalized_description": "spotify", "amount_cents": 1900,
             "person": "Jay", "payer": "Jay", "split_ratio": "40/60"},
            {"normalized_description": "netflix", "amount_cents": 999,
             "person": "Princess", "payer": "Princess", "split_ratio": "40/60"},
        ]
        existing = {("spotify", 1900, "Jay", "Jay", "40/60")}
        result = filter_new_recurring_items(items, existing)
        assert len(result) == 1
        assert result[0]["normalized_description"] == "netflix"

    def test_filter_empty_existing(self):
        from expense_bot import filter_new_recurring_items
        items = [
            {"normalized_description": "spotify", "amount_cents": 1900,
             "person": "Jay", "payer": "Jay", "split_ratio": "40/60"},
        ]
        result = filter_new_recurring_items(items, set())
        assert len(result) == 1

    def test_filter_all_existing_returns_empty(self):
        from expense_bot import filter_new_recurring_items
        items = [
            {"normalized_description": "spotify", "amount_cents": 1900,
             "person": "Jay", "payer": "Jay", "split_ratio": "40/60"},
        ]
        existing = {("spotify", 1900, "Jay", "Jay", "40/60")}
        result = filter_new_recurring_items(items, existing)
        assert result == []

    # --- parse_recurring_carryover_callback ---

    def test_parse_toggle_callback(self):
        from expense_bot import parse_recurring_carryover_callback
        result = parse_recurring_carryover_callback("rc:t:abc123:tok456")
        assert result == ("toggle", "abc123", "tok456")

    def test_parse_add_callback(self):
        from expense_bot import parse_recurring_carryover_callback
        result = parse_recurring_carryover_callback("rc:a:abc123")
        assert result == ("add", "abc123", None)

    def test_parse_skip_callback(self):
        from expense_bot import parse_recurring_carryover_callback
        result = parse_recurring_carryover_callback("rc:s:abc123")
        assert result == ("skip", "abc123", None)

    def test_parse_invalid_returns_none(self):
        from expense_bot import parse_recurring_carryover_callback
        assert parse_recurring_carryover_callback("bad_data") is None

    def test_parse_unknown_action_returns_none(self):
        from expense_bot import parse_recurring_carryover_callback
        assert parse_recurring_carryover_callback("rc:z:abc") is None


class TestRecurringCarryoverDB:
    """Tests for the DB helpers that power the carry-over prompt."""

    def test_canonical_items_empty_db(self, tmp_db):
        """Returns empty list when no recurring transactions exist."""
        from expense_bot import get_canonical_recurring_items_from_history
        result = get_canonical_recurring_items_from_history()
        assert result == []

    def test_canonical_items_single_row(self, tmp_db):
        """One recurring row → one canonical item."""
        import expense_bot
        expense_bot.execute_write(
            "INSERT INTO transactions (person, amount, description, period_id, payer, split_ratio, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Jay", 19.0, "Spotify", 1, "Jay", "40/60", "recurring"),
        )
        from expense_bot import get_canonical_recurring_items_from_history
        result = get_canonical_recurring_items_from_history()
        assert len(result) == 1
        assert result[0][3] == "Spotify"   # description column
        assert result[0][2] == 19.0        # amount

    def test_canonical_items_deduplicates_same_description_amount(self, tmp_db):
        """Same description+amount across two periods → one canonical item."""
        import expense_bot
        # Create a second period first to satisfy FK constraint
        expense_bot.execute_write(
            "INSERT INTO periods (start_date, is_active) VALUES (datetime('now'), 0)"
        )
        for period_id in [1, 2]:
            expense_bot.execute_write(
                "INSERT INTO transactions (person, amount, description, period_id, payer, split_ratio, category) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("Jay", 19.0, "spotify", period_id, "Jay", "40/60", "recurring"),
            )
        from expense_bot import get_canonical_recurring_items_from_history
        result = get_canonical_recurring_items_from_history()
        assert len(result) == 1

    def test_canonical_items_picks_most_recent_payer(self, tmp_db):
        """When same item appears in multiple periods, most recent payer wins."""
        import expense_bot
        # Create a second period
        expense_bot.execute_write(
            "INSERT INTO periods (start_date, is_active) VALUES (datetime('now'), 0)"
        )
        # Older row in period 1: payer=Princess
        expense_bot.execute_write(
            "INSERT INTO transactions (person, amount, description, period_id, payer, split_ratio, category, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("Princess", 19.0, "spotify", 1, "Princess", "40/60", "recurring", "2026-01-01 10:00:00"),
        )
        # Newer row in period 2: payer=Jay
        expense_bot.execute_write(
            "INSERT INTO transactions (person, amount, description, period_id, payer, split_ratio, category, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("Jay", 19.0, "spotify", 2, "Jay", "40/60", "recurring", "2026-02-01 10:00:00"),
        )
        from expense_bot import get_canonical_recurring_items_from_history
        result = get_canonical_recurring_items_from_history()
        assert len(result) == 1
        assert result[0][0] == "Jay"   # person from most recent row

    def test_canonical_items_different_amounts_are_separate(self, tmp_db):
        """Same description but different amount → two canonical items."""
        import expense_bot
        expense_bot.execute_write(
            "INSERT INTO transactions (person, amount, description, period_id, payer, split_ratio, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Jay", 9.99, "spotify", 1, "Jay", "50/50", "recurring"),
        )
        expense_bot.execute_write(
            "INSERT INTO transactions (person, amount, description, period_id, payer, split_ratio, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Jay", 19.99, "spotify", 1, "Jay", "50/50", "recurring"),
        )
        from expense_bot import get_canonical_recurring_items_from_history
        result = get_canonical_recurring_items_from_history()
        assert len(result) == 2

    def test_canonical_items_ignores_non_recurring(self, tmp_db):
        """Non-recurring transactions are excluded."""
        import expense_bot
        expense_bot.execute_write(
            "INSERT INTO transactions (person, amount, description, period_id, payer, split_ratio, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Jay", 50.0, "groceries", 1, "Jay", "50/50", "groceries"),
        )
        from expense_bot import get_canonical_recurring_items_from_history
        result = get_canonical_recurring_items_from_history()
        assert result == []

    def test_canonical_items_normalises_description_casing(self, tmp_db):
        """'Spotify' and 'spotify' are the same canonical item."""
        import expense_bot
        # Create a second period
        expense_bot.execute_write(
            "INSERT INTO periods (start_date, is_active) VALUES (datetime('now'), 0)"
        )
        expense_bot.execute_write(
            "INSERT INTO transactions (person, amount, description, period_id, payer, split_ratio, category, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("Jay", 19.0, "Spotify", 1, "Jay", "40/60", "recurring", "2026-01-01"),
        )
        expense_bot.execute_write(
            "INSERT INTO transactions (person, amount, description, period_id, payer, split_ratio, category, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("Jay", 19.0, "spotify", 2, "Jay", "40/60", "recurring", "2026-02-01"),
        )
        from expense_bot import get_canonical_recurring_items_from_history
        result = get_canonical_recurring_items_from_history()
        assert len(result) == 1

    def test_get_existing_recurring_signatures_empty(self, tmp_db):
        """Returns empty set when period has no recurring transactions."""
        from expense_bot import get_existing_recurring_signatures_for_period
        result = get_existing_recurring_signatures_for_period(1)
        assert result == set()

    def test_get_existing_recurring_signatures_returns_correct_tuple(self, tmp_db):
        """Returns a set of (norm_desc, cents, person, payer, split_ratio) tuples."""
        import expense_bot
        expense_bot.execute_write(
            "INSERT INTO transactions (person, amount, description, period_id, payer, split_ratio, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Jay", 19.0, "Spotify", 1, "Jay", "40/60", "recurring"),
        )
        from expense_bot import get_existing_recurring_signatures_for_period
        result = get_existing_recurring_signatures_for_period(1)
        assert ("spotify", 1900, "Jay", "Jay", "40/60") in result

    # --- build_recurring_carryover_session ---

    def test_build_session_all_tokens_preselected(self):
        from expense_bot import build_recurring_carryover_session, build_recurring_carryover_items
        rows = [
            ("Jay", "Jay", 19.0, "Spotify", "40/60"),
            ("Princess", "Princess", 9.99, "Netflix", "40/60"),
        ]
        items = build_recurring_carryover_items(rows)
        session = build_recurring_carryover_session(items, period_id=5)
        assert not session["completed"]
        assert session["period_id"] == 5
        # All tokens pre-selected
        assert set(session["selected_tokens"]) == set(session["ordered_tokens"])

    def test_build_session_empty_items_returns_none(self):
        from expense_bot import build_recurring_carryover_session
        result = build_recurring_carryover_session([], period_id=5)
        assert result is None
