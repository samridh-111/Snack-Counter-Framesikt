"""
State transition tests: illegal transitions raise errors.
"""
import pytest
from datetime import datetime, timezone
from decimal import Decimal

from models import Office, Money, Volunteer, VolunteerStatus, RefillState
from budget import BudgetLedger
from fakes import FakeChat, FakeHR, FakeClock
from refill_service import RefillService, ServiceConfig, InvalidStateTransitionError


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
    return service, chat, hr


class TestInvalidStateTransitions:
    """Illegal state transitions raise errors."""

    def test_cannot_accept_offer_with_wrong_volunteer(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.OSLO)
        service, chat, hr = setup_service(clock, volunteers)

        refill = service.report_empty("vol0", Office.OSLO)
        # Refill is OFFERED, try to accept with wrong volunteer
        with pytest.raises(ValueError, match="Offer not for this volunteer"):
            service.accept_offer(refill.id, "vol1")  # Wrong volunteer

    def test_cannot_decline_offer_in_wrong_state(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.OSLO)
        service, chat, hr = setup_service(clock, volunteers)

        refill = service.report_empty("vol0", Office.OSLO)
        service.accept_offer(refill.id, "vol0")  # Now COMMITTED

        with pytest.raises(InvalidStateTransitionError):
            service.decline_offer(refill.id, "vol0")  # Can't decline after commit

    def test_cannot_submit_receipt_before_commit(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.OSLO)
        service, chat, hr = setup_service(clock, volunteers)

        refill = service.report_empty("vol0", Office.OSLO)
        # State is OFFERED, not COMMITTED

        with pytest.raises(InvalidStateTransitionError):
            service.submit_receipt(refill.id, "vol0", "https://receipt.jpg", Money(Decimal("1000"), "NOK"))

    def test_cannot_submit_receipt_after_reimbursed(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.OSLO)
        service, chat, hr = setup_service(clock, volunteers)

        refill = service.report_empty("vol0", Office.OSLO)
        service.accept_offer(refill.id, "vol0")
        service.submit_receipt(refill.id, "vol0", "https://receipt.jpg", Money(Decimal("1000"), "NOK"))
        hr.approve_claim(refill.id)
        service.process_hr_approval(refill.id)  # Now REIMBURSED

        with pytest.raises(InvalidStateTransitionError):
            service.submit_receipt(refill.id, "vol0", "https://receipt2.jpg", Money(Decimal("1000"), "NOK"))

    def test_cannot_resubmit_without_rejection(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.OSLO)
        service, chat, hr = setup_service(clock, volunteers)

        refill = service.report_empty("vol0", Office.OSLO)
        service.accept_offer(refill.id, "vol0")
        service.submit_receipt(refill.id, "vol0", "https://receipt.jpg", Money(Decimal("1000"), "NOK"))
        # State is CLAIM_SUBMITTED, not RESUBMIT

        with pytest.raises(InvalidStateTransitionError):
            service.resubmit_receipt(refill.id, "vol0", "https://receipt2.jpg", Money(Decimal("1000"), "NOK"))

    def test_cannot_approve_claim_in_wrong_state(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.OSLO)
        service, chat, hr = setup_service(clock, volunteers)

        refill = service.report_empty("vol0", Office.OSLO)
        service.accept_offer(refill.id, "vol0")
        # State is COMMITTED, not CLAIM_SUBMITTED

        with pytest.raises(InvalidStateTransitionError):
            service.process_hr_approval(refill.id)

    def test_cannot_reject_claim_in_wrong_state(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.OSLO)
        service, chat, hr = setup_service(clock, volunteers)

        refill = service.report_empty("vol0", Office.OSLO)
        service.accept_offer(refill.id, "vol0")
        service.submit_receipt(refill.id, "vol0", "https://receipt.jpg", Money(Decimal("1000"), "NOK"))
        hr.approve_claim(refill.id)
        service.process_hr_approval(refill.id)  # Now REIMBURSED

        with pytest.raises(InvalidStateTransitionError):
            service.process_hr_rejection(refill.id, "Some reason")


class TestValidTransitions:
    """Verify valid transitions work correctly."""

    def test_full_happy_path(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.OSLO)
        service, chat, hr = setup_service(clock, volunteers)

        # EMPTY_REPORTED -> OFFERED
        refill = service.report_empty("vol0", Office.OSLO)
        assert refill.state == RefillState.OFFERED

        # OFFERED -> COMMITTED
        service.accept_offer(refill.id, "vol0")
        refill = service.get_refill(refill.id)
        assert refill.state == RefillState.COMMITTED

        # COMMITTED -> RECEIPT_SUBMITTED -> CLAIM_SUBMITTED
        service.submit_receipt(refill.id, "vol0", "https://receipt.jpg", Money(Decimal("1000"), "NOK"))
        refill = service.get_refill(refill.id)
        # After submit_receipt, HR claim is created immediately
        assert refill.state == RefillState.CLAIM_SUBMITTED

        # CLAIM_SUBMITTED -> REIMBURSED
        hr.approve_claim(refill.id)
        service.process_hr_approval(refill.id)
        refill = service.get_refill(refill.id)
        assert refill.state == RefillState.REIMBURSED

    def test_decline_then_reoffer(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(2, Office.OSLO)
        service, chat, hr = setup_service(clock, volunteers)

        refill = service.report_empty("vol0", Office.OSLO)  # Offered to vol0
        assert refill.volunteer_id == "vol0"

        service.decline_offer(refill.id, "vol0")  # vol0 declines
        refill = service.get_refill(refill.id)
        assert refill.state == RefillState.OFFERED
        assert refill.volunteer_id == "vol1"  # Next volunteer

    def test_audit_log_records_all_transitions(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.OSLO)
        service, chat, hr = setup_service(clock, volunteers)

        refill = service.report_empty("vol0", Office.OSLO)
        service.accept_offer(refill.id, "vol0")
        service.submit_receipt(refill.id, "vol0", "https://receipt.jpg", Money(Decimal("1000"), "NOK"))
        hr.approve_claim(refill.id)
        service.process_hr_approval(refill.id)

        refill = service.get_refill(refill.id)
        states = [e.to_state for e in refill.audit_log]

        assert RefillState.OFFERED in states
        assert RefillState.COMMITTED in states
        assert RefillState.RECEIPT_SUBMITTED in states
        assert RefillState.CLAIM_SUBMITTED in states
        assert RefillState.REIMBURSED in states

        # Check actors
        assert any(e.actor == "vol0" for e in refill.audit_log)
        assert any(e.actor == "hr" for e in refill.audit_log)
        assert any(e.actor == "system" for e in refill.audit_log)
