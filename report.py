"""
Monthly report generation.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from models import Office, Money, Refill, RefillState
from budget import BudgetLedger


@dataclass
class OfficeReport:
    office: Office
    currency: str
    budget_limit: Money
    total_spent: Money
    budget_used_pct: Decimal
    refill_count: int
    avg_empty_to_refilled_hours: Optional[Decimal]
    avg_purchase_to_reimbursed_hours: Optional[Decimal]
    items_breakdown: dict[str, int]  # snack item -> count (placeholder)


@dataclass
class MonthlyReport:
    month: int
    year: int
    oslo: OfficeReport
    bengaluru: OfficeReport
    grand_total_nok: Money
    fx_rate_used: Decimal
    fx_rate_date: datetime

    def format(self) -> str:
        lines = [
            f"=== Snack Counter Monthly Report {self.year}-{self.month:02d} ===",
            "",
            f"Oslo (NOK):",
            f"  Budget: {self.oslo.budget_limit}",
            f"  Spent:  {self.oslo.total_spent} ({self.oslo.budget_used_pct:.1f}%)",
            f"  Refills: {self.oslo.refill_count}",
            f"  Avg empty→refilled: {self.oslo.avg_empty_to_refilled_hours or 'N/A'}h",
            f"  Avg purchase→reimbursed: {self.oslo.avg_purchase_to_reimbursed_hours or 'N/A'}h",
            "",
            f"Bengaluru (INR):",
            f"  Budget: {self.bengaluru.budget_limit}",
            f"  Spent:  {self.bengaluru.total_spent} ({self.bengaluru.budget_used_pct:.1f}%)",
            f"  Refills: {self.bengaluru.refill_count}",
            f"  Avg empty→refilled: {self.bengaluru.avg_empty_to_refilled_hours or 'N/A'}h",
            f"  Avg purchase→reimbursed: {self.bengaluru.avg_purchase_to_reimbursed_hours or 'N/A'}h",
            "",
            f"Grand Total (NOK): {self.grand_total_nok}",
            f"  FX Rate (INR→NOK): {self.fx_rate_used} (as of {self.fx_rate_date.date()})",
        ]
        return "\n".join(lines)


class ReportGenerator:
    """Generates monthly cross-office reports."""

    def __init__(self, refills: dict[str, Refill], budget: BudgetLedger):
        self.refills = refills
        self.budget = budget

    def generate(self, year: int, month: int, fx_rate_date: datetime = None) -> MonthlyReport:
        """Generate report for a specific month."""
        fx_rate_date = fx_rate_date or datetime(year, month, 1, tzinfo=timezone.utc)

        # Get FX rate for INR -> NOK
        fx_rate = self.budget.get_fx_rate("INR", "NOK", fx_rate_date)
        if fx_rate is None:
            raise ValueError(f"No FX rate for INR->NOK at {fx_rate_date}")

        # Filter refills for this month - only REIMBURSED refills count
        month_refills = []
        for refill in self.refills.values():
            # Use committed_at as proxy for "purchase month"
            if refill.state == RefillState.REIMBURSED and refill.committed_at and refill.committed_at.year == year and refill.committed_at.month == month:
                month_refills.append(refill)

        # Split by office
        oslo_refills = [r for r in month_refills if r.office == Office.OSLO]
        blr_refills = [r for r in month_refills if r.office == Office.BENGALURU]

        # Oslo report
        oslo_spent = self.budget.get_spent(Office.OSLO, datetime(year, month, 1, tzinfo=timezone.utc))
        oslo_limit = Money(Office.OSLO.monthly_budget, "NOK")
        oslo_pct = (oslo_spent.amount / oslo_limit.amount * 100) if oslo_limit.amount > 0 else Decimal("0")
        oslo_avg_empty = self._avg_empty_to_refilled(oslo_refills)
        oslo_avg_reimb = self._avg_purchase_to_reimbursed(oslo_refills)

        oslo_report = OfficeReport(
            office=Office.OSLO,
            currency="NOK",
            budget_limit=oslo_limit,
            total_spent=oslo_spent,
            budget_used_pct=oslo_pct.quantize(Decimal("0.1")),
            refill_count=len(oslo_refills),
            avg_empty_to_refilled_hours=oslo_avg_empty,
            avg_purchase_to_reimbursed_hours=oslo_avg_reimb,
            items_breakdown={}  # Would need item tracking
        )

        # Bengaluru report
        blr_spent = self.budget.get_spent(Office.BENGALURU, datetime(year, month, 1, tzinfo=timezone.utc))
        blr_limit = Money(Office.BENGALURU.monthly_budget, "INR")
        blr_pct = (blr_spent.amount / blr_limit.amount * 100) if blr_limit.amount > 0 else Decimal("0")
        blr_avg_empty = self._avg_empty_to_refilled(blr_refills)
        blr_avg_reimb = self._avg_purchase_to_reimbursed(blr_refills)

        blr_report = OfficeReport(
            office=Office.BENGALURU,
            currency="INR",
            budget_limit=blr_limit,
            total_spent=blr_spent,
            budget_used_pct=blr_pct.quantize(Decimal("0.1")),
            refill_count=len(blr_refills),
            avg_empty_to_refilled_hours=blr_avg_empty,
            avg_purchase_to_reimbursed_hours=blr_avg_reimb,
            items_breakdown={}
        )

        # Grand total in NOK
        blr_in_nok = self.budget.convert_to_nok(blr_spent, fx_rate_date)
        grand_total = oslo_spent + blr_in_nok

        return MonthlyReport(
            month=month,
            year=year,
            oslo=oslo_report,
            bengaluru=blr_report,
            grand_total_nok=grand_total,
            fx_rate_used=fx_rate,
            fx_rate_date=fx_rate_date
        )

    def _avg_empty_to_refilled(self, refills: list[Refill]) -> Optional[Decimal]:
        """Average time from EMPTY_REPORTED to COMMITTED (refilled)."""
        durations = []
        for r in refills:
            # Find EMPTY_REPORTED and COMMITTED in audit log
            empty_time = None
            committed_time = None
            for entry in r.audit_log:
                if entry.from_state == RefillState.EMPTY_REPORTED and entry.to_state == RefillState.OFFERED:
                    empty_time = entry.timestamp
                if entry.to_state == RefillState.COMMITTED:
                    committed_time = entry.timestamp
                    break
            if empty_time and committed_time:
                hours = (committed_time - empty_time).total_seconds() / 3600
                durations.append(Decimal(str(hours)).quantize(Decimal("0.1")))
        if not durations:
            return None
        return (sum(durations) / len(durations)).quantize(Decimal("0.1"))

    def _avg_purchase_to_reimbursed(self, refills: list[Refill]) -> Optional[Decimal]:
        """Average time from COMMITTED to REIMBURSED."""
        durations = []
        for r in refills:
            if r.state == RefillState.REIMBURSED:
                committed_time = None
                reimbursed_time = None
                for entry in r.audit_log:
                    if entry.to_state == RefillState.COMMITTED:
                        committed_time = entry.timestamp
                    if entry.to_state == RefillState.REIMBURSED:
                        reimbursed_time = entry.timestamp
                        break
                if committed_time and reimbursed_time:
                    hours = (reimbursed_time - committed_time).total_seconds() / 3600
                    durations.append(Decimal(str(hours)).quantize(Decimal("0.1")))
        if not durations:
            return None
        return (sum(durations) / len(durations)).quantize(Decimal("0.1"))
