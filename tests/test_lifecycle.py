"""
Lifecycle tests: offer timeout, commit deadline, reassignment, escalation.
"""
import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from models import Office, Money, Volunteer, VolunteerStatus, RefillState
from budget import BudgetLedger
from selector import VolunteerSelector, SelectorConfig
from fakes import FakeChat, FakeHR, FakeClock
from refill_service import RefillService, ServiceConfig


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


class TestOfferTimeout:
    """Unanswered offer moves to next volunteer; escalates after N failures."""

    def test_offer_timeout_moves_to_next_volunteer(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(3, Office.OSLO)
        budget = BudgetLedger()
        chat = FakeChat(clock.now())
        hr = FakeHR(clock.now())
        config = ServiceConfig(offer_timeout_minutes=10)
        service = RefillService(volunteers, budget, chat, hr, clock, config)

        # Report empty - offer goes to vol0
        refill = service.report_empty("vol0", Office.OSLO)
        assert refill.state == RefillState.OFFERED
        assert refill.volunteer_id == "vol0"

        # Advance clock past offer timeout (11 minutes)
        clock.advance(timedelta(minutes=11))

        # Check timeouts
        service.check_timeouts()

        # Should have moved to vol1
        refill = service.get_refill(refill.id)
        assert refill.state == RefillState.OFFERED
        assert refill.volunteer_id == "vol1"

        # Verify vol0 was notified
        msgs = chat.get_messages(to_user="vol0")
        timeout_msgs = [m for m in msgs if "timed out" in m.text.lower()]
        assert len(timeout_msgs) == 1

    def test_offer_timeout_notifies_original_volunteer(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(2, Office.OSLO)
        budget = BudgetLedger()
        chat = FakeChat(clock.now())
        hr = FakeHR(clock.now())
        config = ServiceConfig(offer_timeout_minutes=10)
        service = RefillService(volunteers, budget, chat, hr, clock, config)

        refill = service.report_empty("vol0", Office.OSLO)
        clock.advance(timedelta(minutes=11))
        service.check_timeouts()

        # Original volunteer should get timeout notification
        msgs = chat.get_messages(to_user="vol0")
        assert any("timed out" in m.text.lower() for m in msgs)


class TestCommitDeadline:
    """Committed refill not completed by deadline -> released, reassigned, original volunteer notified."""

    def test_commit_deadline_missed_releases_and_reoffers(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(3, Office.OSLO)
        budget = BudgetLedger()
        chat = FakeChat(clock.now())
        hr = FakeHR(clock.now())
        config = ServiceConfig(commit_deadline_hours=4)
        service = RefillService(volunteers, budget, chat, hr, clock, config)

        # Report empty and accept
        refill = service.report_empty("vol0", Office.OSLO)
        service.accept_offer(refill.id, "vol0")

        assert refill.state == RefillState.COMMITTED
        assert refill.volunteer_id == "vol0"
        assert refill.committed_at is not None

        # Advance past commit deadline (4 hours + 1 min)
        clock.advance(timedelta(hours=4, minutes=1))

        # Check timeouts
        service.check_timeouts()

        # Should be released and offered to vol1
        refill = service.get_refill(refill.id)
        assert refill.state == RefillState.OFFERED
        assert refill.volunteer_id == "vol1"
        # Budget reservation should be released
        available = budget.get_available(Office.OSLO, clock.now())
        assert available.amount == Decimal("6000")  # Full budget available again

        # Original volunteer notified
        msgs = chat.get_messages(to_user="vol0")
        assert any("deadline missed" in m.text.lower() for m in msgs)

        # Admin notified
        admin_msgs = chat.get_messages(to_user="#admin")
        assert any("deadline missed" in m.text.lower() for m in admin_msgs)


class TestEscalation:
    """After N failures -> escalate to admin."""

    def test_max_offer_attempts_escalates_to_admin(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(3, Office.OSLO)  # 3 volunteers for 3 attempts
        budget = BudgetLedger()
        chat = FakeChat(clock.now())
        hr = FakeHR(clock.now())
        config = ServiceConfig(offer_timeout_minutes=10, max_offer_attempts=3)
        service = RefillService(volunteers, budget, chat, hr, clock, config)

        refill = service.report_empty("vol0", Office.OSLO)  # Offer to vol0 (attempt 1)

        # Timeout 1: vol0 -> vol1
        clock.advance(timedelta(minutes=11))
        service.check_timeouts()
        refill = service.get_refill(refill.id)
        assert refill.volunteer_id == "vol1"

        # Timeout 2: vol1 -> vol2
        clock.advance(timedelta(minutes=11))
        service.check_timeouts()
        refill = service.get_refill(refill.id)
        assert refill.volunteer_id == "vol2"

        # Timeout 3: vol2 -> no more volunteers -> escalate
        clock.advance(timedelta(minutes=11))
        service.check_timeouts()

        # Should escalate
        refill = service.get_refill(refill.id)
        assert refill.state == RefillState.ESCALATED

        # Admin escalation message
        admin_msgs = chat.get_messages(to_user="#admin")
        escalation_msgs = [m for m in admin_msgs if "escalat" in m.text.lower()]
        assert len(escalation_msgs) >= 1

    def test_escalation_callback_fired(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.OSLO)
        budget = BudgetLedger()
        chat = FakeChat(clock.now())
        hr = FakeHR(clock.now())
        config = ServiceConfig(max_offer_attempts=1)
        escalated = []

        def on_escalation(refill, reason):
            escalated.append((refill.id, reason))

        service = RefillService(volunteers, budget, chat, hr, clock, config, on_escalation=on_escalation)

        refill = service.report_empty("vol0", Office.OSLO)
        clock.advance(timedelta(minutes=11))
        service.check_timeouts()

        assert len(escalated) == 1
        assert "no_eligible_volunteers" in escalated[0][1] or "max" in escalated[0][1].lower()
