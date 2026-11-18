"""
Test doubles: FakeClock, FakeChat, FakeHR.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional
from models import Money, Office
import uuid


class FakeClock:
    """Controllable clock for testing."""

    def __init__(self, start: datetime = None):
        self._now = start or datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta):
        self._now += delta

    def set(self, dt: datetime):
        self._now = dt


@dataclass
class ChatMessage:
    to_user: str
    text: str
    timestamp: datetime


class FakeChat:
    """In-memory chat adapter capturing sent messages."""

    def __init__(self, now: datetime = None):
        self._now = now or datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        self.messages: list[ChatMessage] = []

    def send_dm(self, user_id: str, text: str):
        self.messages.append(ChatMessage(user_id, text, self._now))

    def send_channel(self, channel: str, text: str):
        self.messages.append(ChatMessage(channel, text, self._now))

    def get_messages(self, to_user: str = None) -> list[ChatMessage]:
        if to_user:
            return [m for m in self.messages if m.to_user == to_user]
        return self.messages[:]

    def clear(self):
        self.messages.clear()


class HRUnavailableError(Exception):
    """Raised when HR system is unavailable."""
    pass


@dataclass
class HRClaimRecord:
    id: str
    refill_id: str
    employee_id: str
    amount: Money
    description: str
    receipt_image_url: str
    status: str = "pending"  # pending, approved, rejected
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class FakeHR:
    """Fake HR system with idempotency, failure simulation, and outbox."""

    def __init__(self, now: datetime = None):
        self._now = now or datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        self.claims: dict[str, HRClaimRecord] = {}  # key = refill_id (idempotency key)
        self._fail_next_n = 0
        self._fail_mode = False

    def set_time(self, now: datetime):
        self._now = now

    def set_failure_mode(self, enabled: bool, count: int = 1):
        """Simulate HR being down for N calls."""
        self._fail_mode = enabled
        self._fail_next_n = count if enabled else 0

    def create_claim(
        self,
        refill_id: str,
        employee_id: str,
        amount: Money,
        description: str,
        receipt_image_url: str
    ) -> str:
        """Create a claim. Idempotent on refill_id."""
        # Check idempotency
        if refill_id in self.claims:
            return self.claims[refill_id].id

        # Simulate failure
        if self._fail_mode and self._fail_next_n > 0:
            self._fail_next_n -= 1
            raise HRUnavailableError("HR system unavailable")

        claim_id = f"HR-{str(uuid.uuid4())[:8]}"
        claim = HRClaimRecord(
            id=claim_id,
            refill_id=refill_id,
            employee_id=employee_id,
            amount=amount,
            description=description,
            receipt_image_url=receipt_image_url,
            status="pending",
            created_at=self._now
        )
        self.claims[refill_id] = claim
        return claim_id

    def approve_claim(self, refill_id: str) -> bool:
        """Approve a claim (simulate HR action)."""
        if refill_id in self.claims:
            self.claims[refill_id].status = "approved"
            return True
        return False

    def reject_claim(self, refill_id: str, reason: str = "") -> bool:
        """Reject a claim."""
        if refill_id in self.claims:
            self.claims[refill_id].status = "rejected"
            return True
        return False

    def get_claim(self, refill_id: str) -> Optional[HRClaimRecord]:
        return self.claims.get(refill_id)

    def get_pending_claims(self) -> list[HRClaimRecord]:
        return [c for c in self.claims.values() if c.status == "pending"]
