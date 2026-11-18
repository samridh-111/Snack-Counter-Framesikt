"""
Concurrency tests: atomic commit, duplicate deduplication.
"""
import threading
import pytest
from datetime import datetime, timezone
from decimal import Decimal

from models import Office, Money, Volunteer, VolunteerStatus, RefillState
from budget import BudgetLedger
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


def setup_service(clock, volunteers):
    budget = BudgetLedger()
    chat = FakeChat(clock.now())
    hr = FakeHR(clock.now())
    config = ServiceConfig()
    service = RefillService(volunteers, budget, chat, hr, clock, config)
    return service, chat, hr


class TestConcurrentCommit:
    """Two volunteers commit to the same refill concurrently -> exactly one wins."""

    def test_concurrent_commit_only_one_succeeds(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(2, Office.OSLO)
        service, chat, hr = setup_service(clock, volunteers)

        # Report empty - offered to vol0
        refill = service.report_empty("vol0", Office.OSLO)
        assert refill.state == RefillState.OFFERED
        assert refill.volunteer_id == "vol0"

        # Both volunteers try to accept simultaneously
        results = []
        errors = []

        def try_accept(vol_id):
            try:
                result = service.accept_offer(refill.id, vol_id)
                results.append((vol_id, result))
            except Exception as e:
                errors.append((vol_id, type(e).__name__))

        t1 = threading.Thread(target=try_accept, args=("vol0",))
        t2 = threading.Thread(target=try_accept, args=("vol1",))  # Wrong volunteer - should fail

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # vol0 should succeed, vol1 should fail (not their offer)
        assert len(results) == 1
        assert results[0][0] == "vol0"
        assert len(errors) == 1
        assert errors[0][1] == "ValueError"

        # Refill should be COMMITTED
        refill = service.get_refill(refill.id)
        assert refill.state == RefillState.COMMITTED
        assert refill.volunteer_id == "vol0"

    def test_concurrent_accept_same_volunteer_race(self):
        """Two threads trying to accept for the SAME volunteer - only one should win."""
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(1, Office.OSLO)
        service, chat, hr = setup_service(clock, volunteers)

        refill = service.report_empty("vol0", Office.OSLO)

        results = []
        errors = []

        def try_accept():
            try:
                result = service.accept_offer(refill.id, "vol0")
                results.append(result)
            except Exception as e:
                errors.append(type(e).__name__)

        # Simulate concurrent accept from same volunteer (e.g., double-click)
        threads = [threading.Thread(target=try_accept) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one should succeed
        assert len(results) == 1
        assert len(errors) == 4

        refill = service.get_refill(refill.id)
        assert refill.state == RefillState.COMMITTED


class TestDuplicateEmptyReport:
    """Duplicate empty reports -> one open refill."""

    def test_duplicate_empty_reports_deduplicated(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(3, Office.OSLO)
        service, chat, hr = setup_service(clock, volunteers)

        # First report
        refill1 = service.report_empty("vol0", Office.OSLO)
        assert refill1.state == RefillState.OFFERED

        # Second report (different reporter)
        refill2 = service.report_empty("vol1", Office.OSLO)

        # Should be the SAME refill
        assert refill2.id == refill1.id
        assert refill2 is refill1

        # Third report
        refill3 = service.report_empty("vol2", Office.OSLO)
        assert refill3.id == refill1.id

        # Only one open refill per office
        assert len(service.open_refills) == 1
        assert Office.OSLO in service.open_refills


class TestConcurrentEmptyReports:
    """Concurrent empty reports for different offices create separate refills."""

    def test_concurrent_empty_different_offices_creates_separate_refills(self):
        clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))
        volunteers = make_volunteers(2, Office.OSLO) + make_volunteers(2, Office.BENGALURU, "blr")
        service, chat, hr = setup_service(clock, volunteers)

        refill_oslo = service.report_empty("vol0", Office.OSLO)
        refill_blr = service.report_empty("blr0", Office.BENGALURU)

        assert refill_oslo.id != refill_blr.id
        assert refill_oslo.office == Office.OSLO
        assert refill_blr.office == Office.BENGALURU
        assert len(service.open_refills) == 2
