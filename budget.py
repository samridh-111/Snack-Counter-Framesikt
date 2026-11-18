"""
Budget management with per-office monthly ledgers, reservations, and FX conversion.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from models import Office, Money
import threading


@dataclass
class MonthlyBudget:
    office: Office
    year: int
    month: int
    limit: Money
    spent: Money = field(init=False)
    reserved: Money = field(init=False)

    def __post_init__(self):
        self.spent = Money(Decimal("0"), self.limit.currency)
        self.reserved = Money(Decimal("0"), self.limit.currency)

    @property
    def available(self) -> Money:
        return self.limit - self.spent - self.reserved

    def can_reserve(self, amount: Money) -> bool:
        return amount.currency == self.limit.currency and amount <= self.available

    def reserve(self, amount: Money) -> bool:
        if not self.can_reserve(amount):
            return False
        self.reserved += amount
        return True

    def release(self, amount: Money) -> bool:
        if amount.currency != self.limit.currency:
            return False
        self.reserved = Money(max(Decimal("0"), self.reserved.amount - amount.amount), self.limit.currency)
        return True

    def commit_actual(self, reserved: Money, actual: Money) -> Money:
        """Replace reservation with actual spend. Returns delta (actual - reserved)."""
        if actual.currency != self.limit.currency:
            raise ValueError("Currency mismatch")
        if reserved.currency != self.limit.currency:
            raise ValueError("Currency mismatch")
        
        self.release(reserved)
        self.spent += actual
        return actual - reserved


@dataclass
class ExchangeRate:
    from_currency: str
    to_currency: str
    rate: Decimal  # 1 from_currency = rate to_currency
    valid_from: datetime


class BudgetExceededError(Exception):
    pass


class BudgetToleranceExceededError(Exception):
    pass


class BudgetLedger:
    """Manages per-office monthly budgets and FX rates."""

    def __init__(self):
        self._budgets: dict[tuple[Office, int, int], MonthlyBudget] = {}
        self._fx_rates: list[ExchangeRate] = []
        self._lock = threading.Lock()

    def _get_budget_key(self, office: Office, dt: datetime) -> tuple:
        # Convert to office timezone for month boundary
        # Simplified: use UTC month (production would use office tz)
        return (office, dt.year, dt.month)

    def get_or_create_budget(self, office: Office, dt: datetime) -> MonthlyBudget:
        key = self._get_budget_key(office, dt)
        with self._lock:
            if key not in self._budgets:
                self._budgets[key] = MonthlyBudget(
                    office=office,
                    year=key[1],
                    month=key[2],
                    limit=Money(office.monthly_budget, office.currency)
                )
            return self._budgets[key]

    def get_budget(self, office: Office, dt: datetime) -> Optional[MonthlyBudget]:
        key = self._get_budget_key(office, dt)
        with self._lock:
            return self._budgets.get(key)

    def reserve_for_commit(self, office: Office, dt: datetime, estimate: Money) -> bool:
        """Reserve estimated amount for a commit. Returns True if successful, raises if budget exceeded."""
        budget = self.get_or_create_budget(office, dt)
        with self._lock:
            if not budget.reserve(estimate):
                raise BudgetExceededError(f"Insufficient budget: available {budget.available}, requested {estimate}")
            return True

    def release_reservation(self, office: Office, dt: datetime, estimate: Money) -> bool:
        """Release a reservation (e.g., on timeout)."""
        budget = self.get_budget(office, dt)
        if not budget:
            return False
        with self._lock:
            return budget.release(estimate)

    def submit_receipt(self, office: Office, dt: datetime, reserved: Money, actual: Money) -> Money:
        """Replace reservation with actual spend. Returns delta."""
        budget = self.get_budget(office, dt)
        if not budget:
            raise BudgetExceededError("No budget for this period")
        with self._lock:
            # Check tolerance: actual should not exceed reserved by more than 20%
            tolerance = reserved.amount * Decimal("1.2")
            if actual.amount > tolerance:
                raise BudgetToleranceExceededError(
                    f"Actual {actual} exceeds reserved {reserved} by more than 20%"
                )
            # Check if actual exceeds remaining budget + released reservation
            if actual > budget.available + reserved:
                raise BudgetExceededError(
                    f"Actual {actual} exceeds available budget {budget.available}"
                )
            return budget.commit_actual(reserved, actual)

    def get_spent(self, office: Office, dt: datetime) -> Money:
        budget = self.get_budget(office, dt)
        if not budget:
            return Money(Decimal("0"), office.currency)
        return budget.spent

    def get_reserved(self, office: Office, dt: datetime) -> Money:
        budget = self.get_budget(office, dt)
        if not budget:
            return Money(Decimal("0"), office.currency)
        return budget.reserved

    def get_available(self, office: Office, dt: datetime) -> Money:
        budget = self.get_budget(office, dt)
        if not budget:
            return Money(office.monthly_budget, office.currency)
        return budget.available

    # FX Rate management
    def add_fx_rate(self, from_currency: str, to_currency: str, rate: Decimal, valid_from: datetime):
        """Add an exchange rate. Rates should be added in chronological order."""
        with self._lock:
            self._fx_rates.append(ExchangeRate(from_currency, to_currency, rate, valid_from))
            # Keep sorted by valid_from
            self._fx_rates.sort(key=lambda r: r.valid_from)

    def get_fx_rate(self, from_currency: str, to_currency: str, at: datetime) -> Optional[Decimal]:
        """Get the latest rate valid at or before the given datetime."""
        if from_currency == to_currency:
            return Decimal("1")
        with self._lock:
            for rate in reversed(self._fx_rates):
                if rate.from_currency == from_currency and rate.to_currency == to_currency:
                    if rate.valid_from <= at:
                        return rate.rate
        return None

    def convert_to_nok(self, amount: Money, at: datetime) -> Money:
        """Convert an amount to NOK using the rate table."""
        if amount.currency == "NOK":
            return amount
        rate = self.get_fx_rate(amount.currency, "NOK", at)
        if rate is None:
            raise ValueError(f"No FX rate from {amount.currency} to NOK at {at}")
        converted = (amount.amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return Money(converted, "NOK")
