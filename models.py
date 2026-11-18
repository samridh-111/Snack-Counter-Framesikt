"""
Domain models for Snack Counter Automation.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional
import uuid
import threading


class Office(Enum):
    OSLO = "OSLO"
    BENGALURU = "BENGALURU"

    @property
    def currency(self) -> str:
        return "NOK" if self == Office.OSLO else "INR"

    @property
    def timezone(self) -> str:
        return "Europe/Oslo" if self == Office.OSLO else "Asia/Kolkata"

    @property
    def monthly_budget(self) -> Decimal:
        return Decimal("6000") if self == Office.OSLO else Decimal("15000")

    @property
    def max_pending_claims(self) -> int:
        return 2


class VolunteerStatus(Enum):
    OFFICE_TODAY = "OFFICE_TODAY"
    REMOTE = "REMOTE"
    HOLIDAY = "HOLIDAY"


class RefillState(Enum):
    EMPTY_REPORTED = "EMPTY_REPORTED"
    OFFERED = "OFFERED"
    COMMITTED = "COMMITTED"
    RECEIPT_SUBMITTED = "RECEIPT_SUBMITTED"
    CLAIM_SUBMITTED = "CLAIM_SUBMITTED"
    REIMBURSED = "REIMBURSED"
    DECLINED = "DECLINED"
    RELEASED = "RELEASED"
    REJECTED = "REJECTED"
    RESUBMIT = "RESUBMIT"
    ESCALATED = "ESCALATED"


# Valid state transitions: from_state -> set of valid to_states
VALID_TRANSITIONS = {
    RefillState.EMPTY_REPORTED: {RefillState.OFFERED, RefillState.ESCALATED},
    RefillState.OFFERED: {RefillState.COMMITTED, RefillState.DECLINED, RefillState.RELEASED, RefillState.ESCALATED},
    RefillState.COMMITTED: {RefillState.RECEIPT_SUBMITTED, RefillState.RELEASED, RefillState.ESCALATED},
    RefillState.RECEIPT_SUBMITTED: {RefillState.CLAIM_SUBMITTED, RefillState.RESUBMIT},
    RefillState.CLAIM_SUBMITTED: {RefillState.REIMBURSED, RefillState.REJECTED},
    RefillState.REJECTED: {RefillState.RESUBMIT, RefillState.ESCALATED},
    RefillState.RESUBMIT: {RefillState.RECEIPT_SUBMITTED, RefillState.ESCALATED},
    RefillState.DECLINED: {RefillState.OFFERED},
    RefillState.RELEASED: {RefillState.OFFERED, RefillState.ESCALATED},
    RefillState.REIMBURSED: set(),  # Terminal
    RefillState.ESCALATED: set(),   # Terminal
}


@dataclass
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self):
        if not isinstance(self.amount, Decimal):
            self.amount = Decimal(str(self.amount))

    def __add__(self, other: "Money") -> "Money":
        if self.currency != other.currency:
            raise ValueError(f"Currency mismatch: {self.currency} vs {other.currency}")
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        if self.currency != other.currency:
            raise ValueError(f"Currency mismatch: {self.currency} vs {other.currency}")
        return Money(self.amount - other.amount, self.currency)

    def __lt__(self, other: "Money") -> bool:
        if self.currency != other.currency:
            raise ValueError(f"Currency mismatch: {self.currency} vs {other.currency}")
        return self.amount < other.amount

    def __le__(self, other: "Money") -> bool:
        if self.currency != other.currency:
            raise ValueError(f"Currency mismatch: {self.currency} vs {other.currency}")
        return self.amount <= other.amount

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return False
        return self.amount == other.amount and self.currency == other.currency

    def __repr__(self) -> str:
        return f"Money({self.amount}, {self.currency})"


@dataclass
class Volunteer:
    id: str
    name: str
    home_office: Office
    current_office: Office
    status: VolunteerStatus
    opted_in_offices: set[Office]
    last_refill_at: Optional[datetime] = None
    refill_count_30d: int = 0
    pending_claims: int = 0

    def is_eligible_for_office(self, office: Office) -> bool:
        return (
            self.current_office == office
            and self.status == VolunteerStatus.OFFICE_TODAY
            and office in self.opted_in_offices
            and self.pending_claims < office.max_pending_claims
        )


@dataclass
class AuditEntry:
    timestamp: datetime
    actor: str  # volunteer_id, "system", "hr"
    from_state: RefillState
    to_state: RefillState
    reason: str = ""


@dataclass
class Refill:
    id: str
    office: Office
    state: RefillState
    version: int = 1
    volunteer_id: Optional[str] = None
    offered_at: Optional[datetime] = None
    committed_at: Optional[datetime] = None
    receipt_amount: Optional[Money] = None
    receipt_image: Optional[str] = None
    claim_id: Optional[str] = None
    resubmit_count: int = 0
    audit_log: list[AuditEntry] = field(default_factory=list)

    def add_audit(self, actor: str, from_state: RefillState, to_state: RefillState, reason: str = "", timestamp: Optional[datetime] = None):
        self.audit_log.append(AuditEntry(
            timestamp=timestamp or datetime.now(timezone.utc),
            actor=actor,
            from_state=from_state,
            to_state=to_state,
            reason=reason
        ))

    def can_transition(self, to_state: RefillState) -> bool:
        return to_state in VALID_TRANSITIONS.get(self.state, set())

    def transition(self, to_state: RefillState, actor: str, reason: str = "", timestamp: Optional[datetime] = None) -> None:
        if not self.can_transition(to_state):
            raise InvalidStateTransitionError(f"Cannot transition from {self.state} to {to_state}")
        self.add_audit(actor, self.state, to_state, reason, timestamp)
        self.state = to_state
        self.version += 1


class InvalidStateTransitionError(Exception):
    pass


# Per-refill locks for atomic compare-and-set
_refill_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def get_refill_lock(refill_id: str) -> threading.Lock:
    """Get or create a lock for a refill ID."""
    with _locks_lock:
        if refill_id not in _refill_locks:
            _refill_locks[refill_id] = threading.Lock()
        return _refill_locks[refill_id]


def new_refill_id() -> str:
    return str(uuid.uuid4())[:8]
