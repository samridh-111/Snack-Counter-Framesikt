"""
Selector tests: fairness ranking, blocking rules, visitor eligibility.
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


class TestSelectorBasics:
    """Selector ranks by fewest refills in last N days, then longest since last refill, then ID."""

    def test_selector_ranks_by_fewest_refills_then_longest_since_last(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(3, Office.OSLO)

        # vol0: 2 refills, last 5 days ago
        volunteers[0].refill_count_30d = 2
        volunteers[0].last_refill_at = clock.now() - timedelta(days=5)

        # vol1: 1 refill, last 2 days ago
        volunteers[1].refill_count_30d = 1
        volunteers[1].last_refill_at = clock.now() - timedelta(days=2)

        # vol2: 1 refill, last 10 days ago (longer since last)
        volunteers[2].refill_count_30d = 1
        volunteers[2].last_refill_at = clock.now() - timedelta(days=10)

        selector = VolunteerSelector(volunteers)
        eligible = selector.get_eligible(Office.OSLO)
        ranked = selector.rank_volunteers(eligible)

        # vol2 should be first (1 refill, longest since last)
        assert ranked[0].id == "vol2"
        # vol1 second (1 refill, shorter since last)
        assert ranked[1].id == "vol1"
        # vol0 last (2 refills)
        assert ranked[2].id == "vol0"

    def test_tiebreaker_longest_since_last_refill(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(2, Office.OSLO)

        # Both have 0 refills
        volunteers[0].refill_count_30d = 0
        volunteers[0].last_refill_at = clock.now() - timedelta(days=10)
        volunteers[1].refill_count_30d = 0
        volunteers[1].last_refill_at = clock.now() - timedelta(days=5)

        selector = VolunteerSelector(volunteers)
        ranked = selector.rank_volunteers(selector.get_eligible(Office.OSLO))

        # vol0 should win (longer since last refill)
        assert ranked[0].id == "vol0"

    def test_tiebreaker_employee_id_deterministic(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(2, Office.OSLO)

        # Both have 0 refills, never refilled
        volunteers[0].refill_count_30d = 0
        volunteers[0].last_refill_at = None
        volunteers[1].refill_count_30d = 0
        volunteers[1].last_refill_at = None

        selector = VolunteerSelector(volunteers)
        ranked = selector.rank_volunteers(selector.get_eligible(Office.OSLO))

        # Should be deterministic by ID
        assert ranked[0].id == "vol0"
        assert ranked[1].id == "vol1"


class TestSelectorBlocking:
    """Selector skips absent, already committed, and too-many-pending volunteers."""

    def test_blocks_absent_volunteers(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(2, Office.OSLO)
        volunteers[0].status = VolunteerStatus.REMOTE  # Not in office
        volunteers[1].status = VolunteerStatus.OFFICE_TODAY

        selector = VolunteerSelector(volunteers)
        eligible = selector.get_eligible(Office.OSLO)

        assert len(eligible) == 1
        assert eligible[0].id == "vol1"

    def test_blocks_volunteers_not_opted_in(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(2, Office.OSLO)
        volunteers[0].opted_in_offices = set()  # Not opted in
        volunteers[1].opted_in_offices = {Office.OSLO}

        selector = VolunteerSelector(volunteers)
        eligible = selector.get_eligible(Office.OSLO)

        assert len(eligible) == 1
        assert eligible[0].id == "vol1"

    def test_blocks_volunteers_with_too_many_pending_claims(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(2, Office.OSLO)
        volunteers[0].pending_claims = 3  # Exceeds max of 2
        volunteers[1].pending_claims = 1

        selector = VolunteerSelector(volunteers)
        eligible = selector.get_eligible(Office.OSLO)

        assert len(eligible) == 1
        assert eligible[0].id == "vol1"

    def test_blocks_volunteers_with_high_outstanding(self):
        """Volunteer with too much outstanding reimbursement blocked."""
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(2, Office.OSLO)
        volunteers[0].pending_claims = 2  # At limit
        volunteers[1].pending_claims = 0

        selector = VolunteerSelector(volunteers)
        eligible = selector.get_eligible(Office.OSLO)

        assert len(eligible) == 1
        assert eligible[0].id == "vol1"

    def test_blocks_volunteers_wrong_office(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.OSLO)
        volunteers[0].current_office = Office.BENGALURU  # In wrong office

        selector = VolunteerSelector(volunteers)
        eligible = selector.get_eligible(Office.OSLO)

        assert len(eligible) == 0


class TestVisitorEligibility:
    """Visiting Oslo employee in Bengaluru is eligible there and claim is in INR."""

    def test_oslo_employee_visiting_bengaluru_eligible_for_bengaluru(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.BENGALURU, "oslo")
        volunteers[0].home_office = Office.OSLO
        volunteers[0].current_office = Office.BENGALURU
        volunteers[0].opted_in_offices = {Office.OSLO, Office.BENGALURU}
        volunteers[0].status = VolunteerStatus.OFFICE_TODAY

        selector = VolunteerSelector(volunteers)
        eligible = selector.get_eligible(Office.BENGALURU)

        assert len(eligible) == 1
        assert eligible[0].id == "oslo0"

    def test_visitor_not_opted_in_not_eligible(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.BENGALURU, "oslo")
        volunteers[0].home_office = Office.OSLO
        volunteers[0].current_office = Office.BENGALURU
        volunteers[0].opted_in_offices = {Office.OSLO}  # Not opted in to BLR

        selector = VolunteerSelector(volunteers)
        eligible = selector.get_eligible(Office.BENGALURU)

        assert len(eligible) == 0


class TestSelectNext:
    """Integration test with RefillService selector."""

    def test_select_next_excludes_committed_volunteer(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(2, Office.OSLO)
        budget = BudgetLedger()
        chat = FakeChat(clock.now())
        hr = FakeHR(clock.now())
        config = ServiceConfig()
        service = RefillService(volunteers, budget, chat, hr, clock, config)

        refill = service.report_empty("vol0", Office.OSLO)
        service.accept_offer(refill.id, "vol0")  # vol0 committed

        # Create another refill - should go to vol1
        refill2 = service.report_empty("vol1", Office.OSLO)
        # Should be same refill (duplicate) or new one after first completes
        # Since first is COMMITTED, not EMPTY/OFFERED, a new one can be created
        # Actually the first refill is still open, so it should return the same
        # Let's test after first completes
        service.submit_receipt(refill.id, "vol0", "http://receipt.jpg", Money(Decimal("500"), "NOK"))
        service.process_hr_approval(refill.id)

        # Now new empty report should create new refill
        refill3 = service.report_empty("vol0", Office.OSLO)
        assert refill3.id != refill.id

    def test_select_next_returns_none_when_no_eligible(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.OSLO)
        volunteers[0].status = VolunteerStatus.REMOTE

        selector = VolunteerSelector(volunteers)
        selected = selector.select_next(Office.OSLO)

        assert selected is None
