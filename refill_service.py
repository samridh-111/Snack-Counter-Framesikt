"""
Core refill service orchestrating the lifecycle.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, Callable
import threading
import uuid

from models import (
    Office, Volunteer, VolunteerStatus, RefillState, Refill, Money,
    InvalidStateTransitionError, get_refill_lock, new_refill_id
)
from selector import VolunteerSelector, SelectorConfig
from budget import BudgetLedger, BudgetExceededError, BudgetToleranceExceededError
from fakes import FakeChat, FakeHR, FakeClock, HRUnavailableError


@dataclass
class ServiceConfig:
    offer_timeout_minutes: int = 10
    commit_deadline_hours: int = 4
    max_offer_attempts: int = 3
    max_resubmits: int = 2
    estimate_nok: Decimal = Decimal("1200")
    estimate_inr: Decimal = Decimal("3000")


@dataclass
class RefillService:
    volunteers: list[Volunteer]
    budget: BudgetLedger
    chat: FakeChat
    hr: FakeHR
    clock: FakeClock
    config: ServiceConfig = field(default_factory=ServiceConfig)
    selector: VolunteerSelector = field(init=False)
    refills: dict[str, Refill] = field(default_factory=dict)
    # office -> current open refill id
    open_refills: dict[Office, str] = field(default_factory=dict)
    # callbacks
    on_escalation: Optional[Callable[[Refill, str], None]] = None

    def __post_init__(self):
        self.selector = VolunteerSelector(self.volunteers, SelectorConfig())

    def _get_estimate(self, office: Office) -> Money:
        if office == Office.OSLO:
            return Money(self.config.estimate_nok, "NOK")
        return Money(self.config.estimate_inr, "INR")

    def _get_office_refills(self, office: Office) -> list[Refill]:
        return [r for r in self.refills.values() if r.office == office]

    def _get_open_refill(self, office: Office) -> Optional[Refill]:
        refill_id = self.open_refills.get(office)
        if refill_id and refill_id in self.refills:
            refill = self.refills[refill_id]
            if refill.state in (RefillState.EMPTY_REPORTED, RefillState.OFFERED, RefillState.COMMITTED):
                return refill
        return None

    def report_empty(self, reporter_id: str, office: Office) -> Refill:
        """Report an empty snack box. Returns the refill (existing or new)."""
        # Check for existing open refill
        existing = self._get_open_refill(office)
        if existing:
            return existing

        # Create new refill
        refill = Refill(
            id=new_refill_id(),
            office=office,
            state=RefillState.EMPTY_REPORTED,
        )
        self.refills[refill.id] = refill
        self.open_refills[office] = refill.id

        # Immediately offer to best volunteer
        self._offer_to_next(refill, reporter_id)
        return refill

    def _get_offered_volunteers(self, refill: Refill) -> set[str]:
        """Extract volunteer IDs who have been offered this refill."""
        offered = set()
        for entry in refill.audit_log:
            if entry.to_state == RefillState.OFFERED and entry.reason.startswith("Offered to "):
                vol_id = entry.reason.replace("Offered to ", "")
                offered.add(vol_id)
        return offered

    def _offer_to_next(self, refill: Refill, actor: str) -> bool:
        """Offer refill to next eligible volunteer. Returns True if offered."""
        exclude = self._get_offered_volunteers(refill)

        volunteer = self.selector.select_next(refill.office, exclude)
        if not volunteer:
            # Escalate
            refill.transition(RefillState.ESCALATED, "system", "No eligible volunteers", self.clock.now())
            self._notify_admin(refill, "ESCALATED: No eligible volunteers for refill")
            if self.on_escalation:
                self.on_escalation(refill, "no_eligible_volunteers")
            return False

        refill.transition(RefillState.OFFERED, "system", f"Offered to {volunteer.id}", self.clock.now())
        refill.volunteer_id = volunteer.id
        refill.offered_at = self.clock.now()
        self.chat.send_dm(volunteer.id, f"Snack box empty in {refill.office.value}. Reply 'accept' or 'decline'.")
        return True

    def accept_offer(self, refill_id: str, volunteer_id: str) -> Refill:
        """Volunteer accepts the offer."""
        refill = self.refills.get(refill_id)
        if not refill:
            raise ValueError("Refill not found")
        if refill.volunteer_id != volunteer_id:
            raise ValueError("Offer not for this volunteer")
        if refill.state != RefillState.OFFERED:
            raise InvalidStateTransitionError(f"Cannot accept in state {refill.state}")

        # Atomic commit with compare-and-set
        lock = get_refill_lock(refill_id)
        with lock:
            # Re-check state under lock
            if refill.state != RefillState.OFFERED or refill.volunteer_id != volunteer_id:
                raise InvalidStateTransitionError("Offer no longer valid")

            # Reserve budget
            estimate = self._get_estimate(refill.office)
            if not self.budget.reserve_for_commit(refill.office, self.clock.now(), estimate):
                raise BudgetExceededError("Budget reservation failed")

            refill.transition(RefillState.COMMITTED, volunteer_id, "Accepted offer", self.clock.now())
            refill.committed_at = self.clock.now()
            self.chat.send_dm(volunteer_id, f"Committed! Buy snacks (up to {estimate}). Reply with receipt image.")
            return refill

    def decline_offer(self, refill_id: str, volunteer_id: str) -> Refill:
        """Volunteer declines the offer."""
        refill = self.refills.get(refill_id)
        if not refill:
            raise ValueError("Refill not found")
        if refill.volunteer_id != volunteer_id:
            raise ValueError("Offer not for this volunteer")
        if refill.state != RefillState.OFFERED:
            raise InvalidStateTransitionError(f"Cannot decline in state {refill.state}")

        refill.transition(RefillState.DECLINED, volunteer_id, "Declined offer", self.clock.now())
        # Offer to next volunteer
        self._offer_to_next(refill, volunteer_id)
        return refill

    def submit_receipt(self, refill_id: str, volunteer_id: str, image_url: str, amount: Money) -> Refill:
        """Submit receipt after purchase."""
        refill = self.refills.get(refill_id)
        if not refill:
            raise ValueError("Refill not found")
        if refill.volunteer_id != volunteer_id:
            raise ValueError("Not your refill")
        if refill.state != RefillState.COMMITTED:
            raise InvalidStateTransitionError(f"Cannot submit receipt in state {refill.state}")

        # Validate receipt
        if not image_url:
            raise ValueError("Receipt image required")
        if amount.amount <= 0:
            raise ValueError("Amount must be positive")
        if amount.currency != refill.office.currency:
            raise ValueError(f"Currency must be {refill.office.currency}")

        estimate = self._get_estimate(refill.office)
        # Replace reservation with actual
        try:
            delta = self.budget.submit_receipt(refill.office, self.clock.now(), estimate, amount)
        except BudgetExceededError as e:
            # Flag for manager approval
            self.chat.send_channel("#admin", f"BUDGET ALERT: {refill_id} actual {amount} exceeds budget. Manager approval needed.")
            raise
        except BudgetToleranceExceededError as e:
            # Flag for manager approval
            self.chat.send_channel("#admin", f"BUDGET ALERT: {refill_id} actual {amount} exceeds estimate {estimate} by >20%. Manager approval needed.")
            raise

        refill.receipt_amount = amount
        refill.receipt_image = image_url
        refill.transition(RefillState.RECEIPT_SUBMITTED, volunteer_id, "Receipt submitted", self.clock.now())

        # Create HR claim (idempotent with refill_id as key)
        try:
            claim_id = self.hr.create_claim(
                refill_id=refill.id,
                employee_id=volunteer_id,
                amount=amount,
                description=f"Snack refill {refill.office.value}",
                receipt_image_url=image_url
            )
            refill.claim_id = claim_id
            refill.transition(RefillState.CLAIM_SUBMITTED, "system", f"HR claim created: {claim_id}", self.clock.now())
        except HRUnavailableError:
            # HR is down - stay in RECEIPT_SUBMITTED, will retry later
            self.chat.send_channel("#admin", f"HR OUTAGE: Could not create claim for {refill.id}. Will retry.")
            # Don't transition to CLAIM_SUBMITTED

        return refill

    def resubmit_receipt(self, refill_id: str, volunteer_id: str, image_url: str, amount: Money) -> Refill:
        """Resubmit receipt after HR rejection."""
        refill = self.refills.get(refill_id)
        if not refill:
            raise ValueError("Refill not found")
        if refill.volunteer_id != volunteer_id:
            raise ValueError("Not your refill")
        if refill.state != RefillState.RESUBMIT:
            raise InvalidStateTransitionError(f"Cannot resubmit in state {refill.state}")
        if refill.resubmit_count >= self.config.max_resubmits:
            refill.transition(RefillState.ESCALATED, "system", "Max resubmits exceeded", self.clock.now())
            self._notify_admin(refill, f"ESCALATED: Max resubmits ({self.config.max_resubmits}) exceeded")
            if self.on_escalation:
                self.on_escalation(refill, "max_resubmits_exceeded")
            return refill

        # Validate
        if not image_url:
            raise ValueError("Receipt image required")
        if amount.amount <= 0:
            raise ValueError("Amount must be positive")
        if amount.currency != refill.office.currency:
            raise ValueError(f"Currency must be {refill.office.currency}")

        refill.receipt_amount = amount
        refill.receipt_image = image_url
        refill.resubmit_count += 1
        refill.transition(RefillState.RECEIPT_SUBMITTED, volunteer_id, f"Resubmit #{refill.resubmit_count}", self.clock.now())

        # Create new HR claim (same idempotency key)
        try:
            claim_id = self.hr.create_claim(
                refill_id=refill.id,
                employee_id=volunteer_id,
                amount=amount,
                description=f"Snack refill {refill.office.value} (resubmit {refill.resubmit_count})",
                receipt_image_url=image_url
            )
            refill.claim_id = claim_id
            refill.transition(RefillState.CLAIM_SUBMITTED, "system", f"HR claim created: {claim_id}", self.clock.now())
        except HRUnavailableError:
            self.chat.send_channel("#admin", f"HR OUTAGE: Could not create claim for {refill.id}. Will retry.")

        return refill

    def process_hr_approval(self, refill_id: str) -> Refill:
        """Process HR approval (called by webhook or polling)."""
        refill = self.refills.get(refill_id)
        if not refill:
            raise ValueError("Refill not found")
        if refill.state != RefillState.CLAIM_SUBMITTED:
            raise InvalidStateTransitionError(f"Cannot approve in state {refill.state}")

        refill.transition(RefillState.REIMBURSED, "hr", "HR approved", self.clock.now())
        self.chat.send_dm(refill.volunteer_id, f"Reimbursed: {refill.receipt_amount}")
        self.selector.release_volunteer(refill.volunteer_id)
        # Clear open refill for this office
        if self.open_refills.get(refill.office) == refill_id:
            del self.open_refills[refill.office]
        return refill

    def process_hr_rejection(self, refill_id: str, reason: str) -> Refill:
        """Process HR rejection."""
        refill = self.refills.get(refill_id)
        if not refill:
            raise ValueError("Refill not found")
        if refill.state != RefillState.CLAIM_SUBMITTED:
            raise InvalidStateTransitionError(f"Cannot reject in state {refill.state}")

        refill.transition(RefillState.REJECTED, "hr", reason, self.clock.now())
        refill.transition(RefillState.RESUBMIT, "system", "Awaiting resubmit", self.clock.now())
        
        # Check if max resubmits exceeded (resubmit_count is number of completed resubmits)
        # If this rejection would lead to a resubmit that exceeds max, escalate
        if refill.resubmit_count >= self.config.max_resubmits:
            refill.transition(RefillState.ESCALATED, "system", "Max resubmits exceeded", self.clock.now())
            self._notify_admin(refill, f"ESCALATED: Max resubmits ({self.config.max_resubmits}) exceeded")
            if self.on_escalation:
                self.on_escalation(refill, "max_resubmits_exceeded")
            return refill

        self.chat.send_dm(refill.volunteer_id, f"Receipt rejected: {reason}. Please resubmit.")
        return refill

    def check_timeouts(self) -> list[Refill]:
        """Check for offer timeouts and commit deadlines. Returns affected refills."""
        affected = []
        now = self.clock.now()

        for refill in list(self.refills.values()):
            if refill.state == RefillState.OFFERED and refill.offered_at:
                if now - refill.offered_at > timedelta(minutes=self.config.offer_timeout_minutes):
                    # Offer timed out
                    volunteer_id = refill.volunteer_id
                    refill.transition(RefillState.RELEASED, "system", "Offer timeout", self.clock.now())
                    self.chat.send_dm(volunteer_id, "Offer timed out. Next volunteer will be asked.")
                    self.selector.release_volunteer(volunteer_id)
                    self._offer_to_next(refill, "system")
                    affected.append(refill)

            elif refill.state == RefillState.COMMITTED and refill.committed_at:
                if now - refill.committed_at > timedelta(hours=self.config.commit_deadline_hours):
                    # Commit deadline missed
                    volunteer_id = refill.volunteer_id
                    estimate = self._get_estimate(refill.office)
                    self.budget.release_reservation(refill.office, self.clock.now(), estimate)
                    refill.transition(RefillState.RELEASED, "system", "Commit deadline missed", self.clock.now())
                    self.chat.send_dm(volunteer_id, "Commit deadline missed. Refill reassigned.")
                    self.chat.send_channel("#admin", f"Deadline missed: {refill.id} by {volunteer_id}")
                    self.selector.release_volunteer(volunteer_id)
                    self._offer_to_next(refill, "system")
                    affected.append(refill)

        return affected

    def retry_pending_claims(self) -> int:
        """Retry HR claims that failed due to outage. Returns count retried."""
        retried = 0
        for refill in self.refills.values():
            if refill.state == RefillState.RECEIPT_SUBMITTED and not refill.claim_id:
                # Claim wasn't created, try again
                try:
                    claim_id = self.hr.create_claim(
                        refill_id=refill.id,
                        employee_id=refill.volunteer_id,
                        amount=refill.receipt_amount,
                        description=f"Snack refill {refill.office.value} (retry)",
                        receipt_image_url=refill.receipt_image
                    )
                    refill.claim_id = claim_id
                    refill.transition(RefillState.CLAIM_SUBMITTED, "system", f"HR claim created (retry): {claim_id}", self.clock.now())
                    retried += 1
                except Exception:
                    pass  # Will retry next cycle
        return retried

    def check_reimbursement_alerts(self, days: int = 10) -> list[Refill]:
        """Check for claims pending reimbursement > N days."""
        alerted = []
        now = self.clock.now()
        for refill in self.refills.values():
            if refill.state == RefillState.CLAIM_SUBMITTED and refill.committed_at:
                if now - refill.committed_at > timedelta(days=days):
                    self._notify_admin(refill, f"REIMBURSEMENT ALERT: Claim pending {days}+ days")
                    alerted.append(refill)
        return alerted

    def _notify_admin(self, refill: Refill, message: str):
        self.chat.send_channel("#admin", f"{message} (refill={refill.id}, office={refill.office.value})")

    def get_refill(self, refill_id: str) -> Optional[Refill]:
        return self.refills.get(refill_id)
