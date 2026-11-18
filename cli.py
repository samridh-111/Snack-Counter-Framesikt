#!/usr/bin/env python3
"""
Simple CLI for Snack Counter Automation demo.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import sys

from models import Office, Money, Volunteer, VolunteerStatus, RefillState
from budget import BudgetLedger
from selector import VolunteerSelector, SelectorConfig
from fakes import FakeChat, FakeHR, FakeClock
from refill_service import RefillService, ServiceConfig
from report import ReportGenerator


def create_demo_setup():
    """Create a demo with volunteers in both offices."""
    clock = FakeClock(datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc))

    volunteers = [
        # Oslo volunteers
        Volunteer(id="oslo1", name="Anna (Oslo)", home_office=Office.OSLO, current_office=Office.OSLO,
                  status=VolunteerStatus.OFFICE_TODAY, opted_in_offices={Office.OSLO}),
        Volunteer(id="oslo2", name="Bjorn (Oslo)", home_office=Office.OSLO, current_office=Office.OSLO,
                  status=VolunteerStatus.OFFICE_TODAY, opted_in_offices={Office.OSLO}),
        Volunteer(id="oslo3", name="Cecilie (Oslo)", home_office=Office.OSLO, current_office=Office.OSLO,
                  status=VolunteerStatus.OFFICE_TODAY, opted_in_offices={Office.OSLO}),
        # Bengaluru volunteers
        Volunteer(id="blr1", name="Deepak (BLR)", home_office=Office.BENGALURU, current_office=Office.BENGALURU,
                  status=VolunteerStatus.OFFICE_TODAY, opted_in_offices={Office.BENGALURU}),
        Volunteer(id="blr2", name="Priya (BLR)", home_office=Office.BENGALURU, current_office=Office.BENGALURU,
                  status=VolunteerStatus.OFFICE_TODAY, opted_in_offices={Office.BENGALURU}),
        # Visitor: Oslo employee visiting Bengaluru
        Volunteer(id="oslo_visitor", name="Erik (Oslo@BLR)", home_office=Office.OSLO, current_office=Office.BENGALURU,
                  status=VolunteerStatus.OFFICE_TODAY, opted_in_offices={Office.OSLO, Office.BENGALURU}),
    ]

    budget = BudgetLedger()
    chat = FakeChat(clock.now())
    hr = FakeHR(clock.now())
    config = ServiceConfig()
    service = RefillService(volunteers, budget, chat, hr, clock, config)

    # Add FX rate for reporting
    budget.add_fx_rate("INR", "NOK", Decimal("0.11"), datetime(2025, 1, 1, tzinfo=timezone.utc))

    return clock, service, chat, hr, budget, volunteers


def print_header(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def print_refill(refill, service):
    print(f"\n  Refill: {refill.id[:8]} | Office: {refill.office.value} | State: {refill.state.value}")
    if refill.volunteer_id:
        vol = next((v for v in service.volunteers if v.id == refill.volunteer_id), None)
        print(f"  Volunteer: {vol.name if vol else refill.volunteer_id}")
    if refill.receipt_amount:
        print(f"  Amount: {refill.receipt_amount}")
    if refill.claim_id:
        print(f"  HR Claim: {refill.claim_id}")


def demo_basic_flow():
    clock, service, chat, hr, budget, volunteers = create_demo_setup()

    print_header("DEMO: Basic Refill Flow (Oslo)")

    # 1. Report empty
    print("\n1. Anna reports empty snack box in Oslo")
    refill = service.report_empty("oslo1", Office.OSLO)
    print_refill(refill, service)

    # 2. Check chat
    msgs = chat.get_messages(to_user="oslo1")
    print(f"  DM to Anna: {msgs[0].text}")

    # 3. Accept offer
    print("\n2. Anna accepts the offer")
    service.accept_offer(refill.id, "oslo1")
    refill = service.get_refill(refill.id)
    print_refill(refill, service)

    # 4. Submit receipt
    print("\n3. Anna buys snacks and submits receipt (1000 NOK)")
    service.submit_receipt(refill.id, "oslo1", "https://receipt.jpg", Money(Decimal("1000"), "NOK"))
    refill = service.get_refill(refill.id)
    print_refill(refill, service)

    # 5. HR approves
    print("\n4. HR approves the claim")
    hr.approve_claim(refill.id)
    service.process_hr_approval(refill.id)
    refill = service.get_refill(refill.id)
    print_refill(refill, service)

    # 6. Check DM to volunteer
    msgs = chat.get_messages(to_user="oslo1")
    reimb_msg = [m for m in msgs if "reimbursed" in m.text.lower()][0]
    print(f"  DM to Anna: {reimb_msg.text}")

    # 7. Show budget
    print(f"\n5. Oslo budget: {budget.get_available(Office.OSLO, clock.now())} available")
    print(f"   Spent: {budget.get_spent(Office.OSLO, clock.now())}")
    print(f"   Reserved: {budget.get_reserved(Office.OSLO, clock.now())}")


def demo_offer_timeout():
    clock, service, chat, hr, budget, volunteers = create_demo_setup()

    print_header("DEMO: Offer Timeout -> Reassign")

    print("\n1. Report empty - offered to Anna")
    refill = service.report_empty("oslo1", Office.OSLO)
    print_refill(refill, service)

    print("\n2. Anna doesn't respond... advance clock 11 minutes")
    clock.advance(timedelta(minutes=11))
    service.check_timeouts()

    refill = service.get_refill(refill.id)
    print_refill(refill, service)

    msgs = chat.get_messages(to_user="oslo1")
    print(f"  DM to Anna: {msgs[-1].text}")

    msgs = chat.get_messages(to_user="oslo2")
    print(f"  DM to Bjorn: {msgs[0].text}")


def demo_commit_deadline():
    clock, service, chat, hr, budget, volunteers = create_demo_setup()

    print_header("DEMO: Commit Deadline Missed -> Reassign")

    print("\n1. Report empty and Anna accepts")
    refill = service.report_empty("oslo1", Office.OSLO)
    service.accept_offer(refill.id, "oslo1")
    print_refill(refill, service)

    print("\n2. Anna doesn't buy snacks... advance clock 4 hours 1 minute")
    clock.advance(timedelta(hours=4, minutes=1))
    service.check_timeouts()

    refill = service.get_refill(refill.id)
    print_refill(refill, service)

    msgs = chat.get_messages(to_user="oslo1")
    print(f"  DM to Anna: {[m.text for m in msgs if 'deadline' in m.text.lower()][0]}")

    admin_msgs = chat.get_messages(to_user="#admin")
    print(f"  Admin alert: {admin_msgs[0].text}")

    print(f"\n  Budget restored: {budget.get_available(Office.OSLO, clock.now())}")


def demo_hr_outage():
    clock, service, chat, hr, budget, volunteers = create_demo_setup()

    print_header("DEMO: HR Outage -> Auto Retry")

    print("\n1. Complete refill to receipt submission")
    refill = service.report_empty("oslo1", Office.OSLO)
    service.accept_offer(refill.id, "oslo1")
    service.submit_receipt(refill.id, "oslo1", "https://receipt.jpg", Money(Decimal("1000"), "NOK"))
    refill = service.get_refill(refill.id)
    print(f"  State after receipt: {refill.state}, Claim ID: {refill.claim_id}")

    print("\n2. HR goes down, submit another receipt")
    hr.set_failure_mode(True, count=1)
    refill2 = service.report_empty("oslo2", Office.OSLO)
    # Get who was offered
    refill2 = service.get_refill(refill2.id)
    offered_vol = refill2.volunteer_id
    print(f"  Offered to: {next(v.name for v in volunteers if v.id == offered_vol)}")
    service.accept_offer(refill2.id, offered_vol)
    try:
        service.submit_receipt(refill2.id, offered_vol, "https://receipt2.jpg", Money(Decimal("1200"), "NOK"))
    except Exception as e:
        print(f"  Expected error: {e}")

    refill2 = service.get_refill(refill2.id)
    print(f"  State: {refill2.state}, Claim ID: {refill2.claim_id}")

    print("\n3. HR recovers, run retry")
    hr.set_failure_mode(False)
    retried = service.retry_pending_claims()
    print(f"  Retried {retried} claims")

    refill2 = service.get_refill(refill2.id)
    print(f"  State after retry: {refill2.state}, Claim ID: {refill2.claim_id}")


def demo_fairness():
    clock, service, chat, hr, budget, volunteers = create_demo_setup()

    print_header("DEMO: Fairness Selector")

    # Give Anna 2 refills in last 30 days
    for v in volunteers:
        if v.id == "oslo1":
            v.refill_count_30d = 2
            v.last_refill_at = clock.now() - timedelta(days=5)
        elif v.id == "oslo2":
            v.refill_count_30d = 1
            v.last_refill_at = clock.now() - timedelta(days=10)
        elif v.id == "oslo3":
            v.refill_count_30d = 0
            v.last_refill_at = None

    print("\nVolunteer refill counts (30d):")
    for v in volunteers:
        if v.home_office == Office.OSLO:
            print(f"  {v.name}: {v.refill_count_30d} refills, last: {v.last_refill_at}")

    print("\nSimulate 2 refill cycles (decline -> next):")
    for i in range(2):
        refill = service.report_empty("oslo1", Office.OSLO)
        vol = next(v for v in volunteers if v.id == refill.volunteer_id)
        print(f"  Cycle {i+1}: Offered to {vol.name} (30d refills: {vol.refill_count_30d})")
        # Decline to move to next
        service.decline_offer(refill.id, refill.volunteer_id)
        clock.advance(timedelta(minutes=1))

    print("\n(3rd cycle would offer to Anna with 2 refills - fairness works!)")


def demo_visitor():
    clock, service, chat, hr, budget, volunteers = create_demo_setup()

    print_header("DEMO: Visitor Eligibility (Oslo employee in Bengaluru)")

    print("\nErik (Oslo home office) is visiting Bengaluru today")
    print("He is opted in to both offices")

    # Only Erik is in Bengaluru today
    for v in volunteers:
        if v.home_office == Office.BENGALURU:
            v.status = VolunteerStatus.REMOTE

    refill = service.report_empty("oslo_visitor", Office.BENGALURU)
    print(f"\nRefill offered to: {next(v.name for v in volunteers if v.id == refill.volunteer_id)}")
    print(f"Receipt will be in: {refill.office.currency}")


def demo_report():
    clock, service, chat, hr, budget, volunteers = create_demo_setup()

    print_header("DEMO: Monthly Report")

    # Complete a few refills
    for i, (vol_id, office) in enumerate([("oslo1", Office.OSLO), ("oslo2", Office.OSLO), ("blr1", Office.BENGALURU)]):
        refill = service.report_empty(vol_id, office)
        # Accept with whoever was offered
        offered_vol = refill.volunteer_id
        service.accept_offer(refill.id, offered_vol)
        amount = Money(Decimal("1000"), "NOK") if office == Office.OSLO else Money(Decimal("2500"), "INR")
        service.submit_receipt(refill.id, offered_vol, f"https://receipt{i}.jpg", amount)
        hr.approve_claim(refill.id)
        service.process_hr_approval(refill.id)
        clock.advance(timedelta(hours=1))  # Advance for avg time calc

    # Generate report
    report_gen = ReportGenerator(service.refills, budget)
    report = report_gen.generate(2025, 1)
    print(report.format())


def main():
    demos = {
        "1": ("Basic Flow", demo_basic_flow),
        "2": ("Offer Timeout", demo_offer_timeout),
        "3": ("Commit Deadline", demo_commit_deadline),
        "4": ("HR Outage & Retry", demo_hr_outage),
        "5": ("Fairness Selector", demo_fairness),
        "6": ("Visitor Eligibility", demo_visitor),
        "7": ("Monthly Report", demo_report),
    }

    if len(sys.argv) > 1:
        choice = sys.argv[1]
        if choice == "all":
            for _, (name, func) in demos.items():
                print(f"\n{'='*60}")
                print(f"  Running: {name}")
                print(f"{'='*60}")
                func()
        elif choice in demos:
            demos[choice][1]()
        else:
            print(f"Unknown demo: {choice}")
            print(f"Available: {', '.join(list(demos.keys()) + ['all'])}")
    else:
        print("\nSnack Counter Automation - Demo CLI")
        print("\nAvailable demos:")
        for k, (name, _) in demos.items():
            print(f"  python cli.py {k}  # {name}")
        print("\nRun: python cli.py all  # Run all demos")


if __name__ == "__main__":
    main()