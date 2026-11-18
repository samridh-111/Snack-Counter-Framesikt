"""
Report tests: monthly report with cross-office totals.
"""
import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from models import Office, Money, Volunteer, VolunteerStatus, Refill, RefillState
from budget import BudgetLedger
from fakes import FakeChat, FakeHR, FakeClock
from refill_service import RefillService, ServiceConfig
from report import ReportGenerator


def make_volunteers(count: int, office: Office, prefix: str = "vol") -> list[Volunteer]:
    vols = []
    for i in range(count):
        v = Volunteer(
            id=f"{prefix}{i}",
            name=f"Volunteer {i}",
            home_office=office,
            current_office=office,
            status=VolunteerStatus.OFFICE_TODAY,
            opted_in_offices={office}
        )
        vols.append(v)
    return vols


def setup_service(clock, volunteers):
    budget = BudgetLedger()
    chat = FakeChat(clock.now())
    hr = FakeHR(clock.now())
    config = ServiceConfig()
    service = RefillService(volunteers, budget, chat, hr, clock, config)
    return service, chat, hr, budget


class TestMonthlyReport:
    """Monthly report totals correct across both currencies with given rate."""

    def test_report_totals_per_office_local_currency(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(2, Office.OSLO) + make_volunteers(2, Office.BENGALURU, "blr")
        service, chat, hr, budget = setup_service(clock, volunteers)

        # Add FX rate
        budget.add_fx_rate("INR", "NOK", Decimal("0.11"), datetime(2025, 1, 1, tzinfo=timezone.utc))

        # Complete an Oslo refill
        refill1 = service.report_empty("vol0", Office.OSLO)
        service.accept_offer(refill1.id, "vol0")
        service.submit_receipt(refill1.id, "vol0", "http://r1.jpg", Money(Decimal("1000"), "NOK"))
        hr.approve_claim(refill1.id)
        service.process_hr_approval(refill1.id)

        # Complete a Bengaluru refill
        refill2 = service.report_empty("blr0", Office.BENGALURU)
        service.accept_offer(refill2.id, "blr0")
        service.submit_receipt(refill2.id, "blr0", "http://r2.jpg", Money(Decimal("2000"), "INR"))
        hr.approve_claim(refill2.id)
        service.process_hr_approval(refill2.id)

        # Generate report
        generator = ReportGenerator(service.refills, budget)
        report = generator.generate(2025, 1)

        assert report.oslo.total_spent.amount == Decimal("1000")
        assert report.oslo.total_spent.currency == "NOK"
        assert report.bengaluru.total_spent.amount == Decimal("2000")
        assert report.bengaluru.total_spent.currency == "INR"

    def test_report_grand_total_nok_with_fx(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.OSLO) + make_volunteers(1, Office.BENGALURU, "blr")
        service, chat, hr, budget = setup_service(clock, volunteers)

        budget.add_fx_rate("INR", "NOK", Decimal("0.11"), datetime(2025, 1, 1, tzinfo=timezone.utc))

        # Oslo: 1000 NOK
        refill1 = service.report_empty("vol0", Office.OSLO)
        service.accept_offer(refill1.id, "vol0")
        service.submit_receipt(refill1.id, "vol0", "http://r1.jpg", Money(Decimal("1000"), "NOK"))
        hr.approve_claim(refill1.id)
        service.process_hr_approval(refill1.id)

        # Bengaluru: 2000 INR = 220 NOK at 0.11
        refill2 = service.report_empty("blr0", Office.BENGALURU)
        service.accept_offer(refill2.id, "blr0")
        service.submit_receipt(refill2.id, "blr0", "http://r2.jpg", Money(Decimal("2000"), "INR"))
        hr.approve_claim(refill2.id)
        service.process_hr_approval(refill2.id)

        generator = ReportGenerator(service.refills, budget)
        report = generator.generate(2025, 1)

        # Grand total = 1000 + 220 = 1220 NOK
        assert report.grand_total_nok.amount == Decimal("1220.00")
        assert report.grand_total_nok.currency == "NOK"
        assert report.fx_rate_used == Decimal("0.11")

    def test_report_budget_used_percent(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.OSLO)
        service, chat, hr, budget = setup_service(clock, volunteers)

        # Add FX rate for report generation
        budget.add_fx_rate("INR", "NOK", Decimal("0.11"), datetime(2025, 1, 1, tzinfo=timezone.utc))

        # Use amount within 20% of estimate (1200)
        refill = service.report_empty("vol0", Office.OSLO)
        service.accept_offer(refill.id, "vol0")
        service.submit_receipt(refill.id, "vol0", "http://r1.jpg", Money(Decimal("1300"), "NOK"))
        hr.approve_claim(refill.id)
        service.process_hr_approval(refill.id)

        generator = ReportGenerator(service.refills, budget)
        report = generator.generate(2025, 1)

        # 1300 / 6000 = 21.67%
        assert report.oslo.budget_used_pct == Decimal("21.7")

    def test_report_avg_times(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.OSLO)
        service, chat, hr, budget = setup_service(clock, volunteers)

        # Add FX rate for report generation
        budget.add_fx_rate("INR", "NOK", Decimal("0.11"), datetime(2025, 1, 1, tzinfo=timezone.utc))

        refill = service.report_empty("vol0", Office.OSLO)
        
        # Advance 2 hours, accept
        clock.advance(timedelta(hours=2))
        service.accept_offer(refill.id, "vol0")
        
        # Advance 3 hours, submit receipt
        clock.advance(timedelta(hours=3))
        service.submit_receipt(refill.id, "vol0", "http://r1.jpg", Money(Decimal("500"), "NOK"))
        
        # Advance 2 days, approve
        clock.advance(timedelta(days=2))
        hr.approve_claim(refill.id)
        service.process_hr_approval(refill.id)

        generator = ReportGenerator(service.refills, budget)
        report = generator.generate(2025, 1)

        # empty->refilled: 2 hours (from EMPTY_REPORTED to COMMITTED)
        assert report.oslo.avg_empty_to_refilled_hours == Decimal("2.0")
        # purchase->reimbursed: 3 hours + 48 hours = 51 hours
        assert report.oslo.avg_purchase_to_reimbursed_hours == Decimal("51.0")

    def test_report_items_breakdown(self):
        """Placeholder for item breakdown - not implemented yet."""
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.OSLO)
        service, chat, hr, budget = setup_service(clock, volunteers)

        # Add FX rate for report generation
        budget.add_fx_rate("INR", "NOK", Decimal("0.11"), datetime(2025, 1, 1, tzinfo=timezone.utc))

        refill = service.report_empty("vol0", Office.OSLO)
        service.accept_offer(refill.id, "vol0")
        service.submit_receipt(refill.id, "vol0", "http://r1.jpg", Money(Decimal("500"), "NOK"))
        hr.approve_claim(refill.id)
        service.process_hr_approval(refill.id)

        generator = ReportGenerator(service.refills, budget)
        report = generator.generate(2025, 1)

        # Items breakdown is empty dict for now
        assert report.oslo.items_breakdown == {}

    def test_report_only_counts_reimbursed_refills(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.OSLO)
        service, chat, hr, budget = setup_service(clock, volunteers)

        # Add FX rate for report generation
        budget.add_fx_rate("INR", "NOK", Decimal("0.11"), datetime(2025, 1, 1, tzinfo=timezone.utc))

        # One completed
        refill1 = service.report_empty("vol0", Office.OSLO)
        service.accept_offer(refill1.id, "vol0")
        service.submit_receipt(refill1.id, "vol0", "http://r1.jpg", Money(Decimal("500"), "NOK"))
        hr.approve_claim(refill1.id)
        service.process_hr_approval(refill1.id)

        # One stuck in COMMITTED
        refill2 = service.report_empty("vol0", Office.OSLO)
        service.accept_offer(refill2.id, "vol0")

        generator = ReportGenerator(service.refills, budget)
        report = generator.generate(2025, 1)

        # Only reimbursed refills counted
        assert report.oslo.refill_count == 1

    def test_format_report_produces_readable_output(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.OSLO)
        service, chat, hr, budget = setup_service(clock, volunteers)

        budget.add_fx_rate("INR", "NOK", Decimal("0.11"), datetime(2025, 1, 1, tzinfo=timezone.utc))

        refill = service.report_empty("vol0", Office.OSLO)
        service.accept_offer(refill.id, "vol0")
        service.submit_receipt(refill.id, "vol0", "http://r1.jpg", Money(Decimal("1000"), "NOK"))
        hr.approve_claim(refill.id)
        service.process_hr_approval(refill.id)

        generator = ReportGenerator(service.refills, budget)
        report = generator.generate(2025, 1)
        formatted = report.format()

        assert "Snack Counter Monthly Report" in formatted
        assert "Oslo (NOK)" in formatted
        assert "Bengaluru (INR)" in formatted
        assert "Grand Total (NOK)" in formatted
        assert "FX Rate" in formatted
