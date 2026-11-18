"""
Budget tests: reservation, actual replacement, monthly rollover, FX conversion.
"""
import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from models import Office, Money
from budget import BudgetLedger, BudgetExceededError, BudgetToleranceExceededError
from fakes import FakeClock


class TestBudgetReservation:
    """Budget reservation, actual replacement, per-office monthly rollover."""

    def test_reserve_blocks_overspend(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        budget = BudgetLedger()

        # Oslo budget: 6000 NOK
        reserved = budget.reserve_for_commit(Office.OSLO, clock.now(), Money(Decimal("1200"), "NOK"))
        assert reserved is True

        # Available should be 6000 - 1200 = 4800
        available = budget.get_available(Office.OSLO, clock.now())
        assert available.amount == Decimal("4800")

    def test_actual_replaces_reservation(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        budget = BudgetLedger()

        reserved_amt = Money(Decimal("1200"), "NOK")
        budget.reserve_for_commit(Office.OSLO, clock.now(), reserved_amt)
        actual = Money(Decimal("1000"), "NOK")

        delta = budget.submit_receipt(Office.OSLO, clock.now(), reserved_amt, actual)

        # Spent should be actual (1000), reserved should be 0
        spent = budget.get_spent(Office.OSLO, clock.now())
        assert spent.amount == Decimal("1000")

        reserved_after = budget.get_reserved(Office.OSLO, clock.now())
        assert reserved_after.amount == Decimal("0")

        # Delta should be -200 (actual - reserved)
        assert delta.amount == Decimal("-200")

    def test_actual_exceeds_tolerance_raises(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        budget = BudgetLedger()

        reserved_amt = Money(Decimal("1200"), "NOK")
        budget.reserve_for_commit(Office.OSLO, clock.now(), reserved_amt)
        # 20% tolerance = 1440 NOK max
        actual = Money(Decimal("1500"), "NOK")

        with pytest.raises(BudgetToleranceExceededError):
            budget.submit_receipt(Office.OSLO, clock.now(), reserved_amt, actual)

    def test_actual_within_tolerance_allowed(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        budget = BudgetLedger()

        reserved_amt = Money(Decimal("1200"), "NOK")
        budget.reserve_for_commit(Office.OSLO, clock.now(), reserved_amt)
        # Within 20% = 1440 NOK max
        actual = Money(Decimal("1400"), "NOK")

        delta = budget.submit_receipt(Office.OSLO, clock.now(), reserved_amt, actual)
        assert delta.amount == Decimal("200")

        spent = budget.get_spent(Office.OSLO, clock.now())
        assert spent.amount == Decimal("1400")

    def test_actual_exceeds_remaining_budget_raises(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        budget = BudgetLedger()

        # Exhaust budget by multiple reserves and submits
        # Each: reserve 1200, spend 1300 (within 20% tolerance)
        # After 4 cycles: spent = 5200, available = 800
        for _ in range(4):
            budget.reserve_for_commit(Office.OSLO, clock.now(), Money(Decimal("1200"), "NOK"))
            budget.submit_receipt(Office.OSLO, clock.now(), 
                                  Money(Decimal("1200"), "NOK"), 
                                  Money(Decimal("1300"), "NOK"))

        # Now spent = 5200, available = 800
        # Try fifth reserve - should fail because 1200 > 800
        with pytest.raises(BudgetExceededError):
            budget.reserve_for_commit(Office.OSLO, clock.now(), Money(Decimal("1200"), "NOK"))

    def test_release_reservation_on_timeout(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        budget = BudgetLedger()

        reserved_amt = Money(Decimal("1200"), "NOK")
        budget.reserve_for_commit(Office.OSLO, clock.now(), reserved_amt)
        available_before = budget.get_available(Office.OSLO, clock.now())

        budget.release_reservation(Office.OSLO, clock.now(), reserved_amt)

        available_after = budget.get_available(Office.OSLO, clock.now())
        assert available_after.amount == Decimal("6000")  # Back to full budget

    def test_monthly_rollover_separate_months(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        budget = BudgetLedger()

        # Spend in January
        budget.reserve_for_commit(Office.OSLO, clock.now(), Money(Decimal("1200"), "NOK"))
        budget.submit_receipt(Office.OSLO, clock.now(), 
                              Money(Decimal("1200"), "NOK"), 
                              Money(Decimal("1000"), "NOK"))

        jan_spent = budget.get_spent(Office.OSLO, datetime(2025, 1, 15, tzinfo=timezone.utc))
        assert jan_spent.amount == Decimal("1000")

        # February should be fresh (separate budget object)
        feb_spent = budget.get_spent(Office.OSLO, datetime(2025, 2, 15, tzinfo=timezone.utc))
        assert feb_spent.amount == Decimal("0")

        # January still shows spent
        jan_spent_again = budget.get_spent(Office.OSLO, datetime(2025, 1, 20, tzinfo=timezone.utc))
        assert jan_spent_again.amount == Decimal("1000")

    def test_oslo_and_bengaluru_never_mixed(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        budget = BudgetLedger()

        budget.reserve_for_commit(Office.OSLO, clock.now(), Money(Decimal("1200"), "NOK"))
        budget.reserve_for_commit(Office.BENGALURU, clock.now(), Money(Decimal("3000"), "INR"))

        oslo_reserved = budget.get_reserved(Office.OSLO, clock.now())
        blr_reserved = budget.get_reserved(Office.BENGALURU, clock.now())

        assert oslo_reserved.currency == "NOK"
        assert oslo_reserved.amount == Decimal("1200")
        assert blr_reserved.currency == "INR"
        assert blr_reserved.amount == Decimal("3000")

        # Operations on one office don't affect the other
        budget.submit_receipt(Office.OSLO, clock.now(), 
                              Money(Decimal("1200"), "NOK"), 
                              Money(Decimal("1000"), "NOK"))
        oslo_spent = budget.get_spent(Office.OSLO, clock.now())
        blr_spent = budget.get_spent(Office.BENGALURU, clock.now())

        assert oslo_spent.amount == Decimal("1000")
        assert blr_spent.amount == Decimal("0")


class TestFXConversion:
    """FX conversion for reporting."""

    def test_convert_inr_to_nok_using_rate_table(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        budget = BudgetLedger()

        # Set rate: 1 INR = 0.11 NOK
        budget.add_fx_rate("INR", "NOK", Decimal("0.11"), datetime(2025, 1, 1, tzinfo=timezone.utc))

        inr_amount = Money(Decimal("15000"), "INR")
        nok_amount = budget.convert_to_nok(inr_amount, datetime(2025, 1, 15, tzinfo=timezone.utc))

        assert nok_amount.currency == "NOK"
        assert nok_amount.amount == Decimal("1650.00")  # 15000 * 0.11

    def test_convert_nok_returns_same(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        budget = BudgetLedger()

        nok_amount = Money(Decimal("5000"), "NOK")
        result = budget.convert_to_nok(nok_amount, datetime(2025, 1, 15, tzinfo=timezone.utc))

        assert result == nok_amount

    def test_convert_without_rate_raises(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        budget = BudgetLedger()

        inr_amount = Money(Decimal("1000"), "INR")

        with pytest.raises(ValueError, match="No FX rate"):
            budget.convert_to_nok(inr_amount, datetime(2025, 1, 15, tzinfo=timezone.utc))

    def test_rate_lookup_uses_latest_before_date(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        budget = BudgetLedger()

        # Set rate on Jan 1
        budget.add_fx_rate("INR", "NOK", Decimal("0.10"), datetime(2025, 1, 1, tzinfo=timezone.utc))
        # Set rate on Jan 10
        budget.add_fx_rate("INR", "NOK", Decimal("0.11"), datetime(2025, 1, 10, tzinfo=timezone.utc))
        # Set rate on Jan 20
        budget.add_fx_rate("INR", "NOK", Decimal("0.12"), datetime(2025, 1, 20, tzinfo=timezone.utc))

        # Query for Jan 15 should get Jan 10 rate (latest before Jan 15)
        rate = budget.get_fx_rate("INR", "NOK", datetime(2025, 1, 15, tzinfo=timezone.utc))
        assert rate == Decimal("0.11")

        # Query for Jan 25 should get Jan 20 rate
        rate = budget.get_fx_rate("INR", "NOK", datetime(2025, 1, 25, tzinfo=timezone.utc))
        assert rate == Decimal("0.12")
