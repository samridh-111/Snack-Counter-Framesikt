"""
Volunteer selection logic for fair refill assignment.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from models import Volunteer, Office, VolunteerStatus


@dataclass
class SelectorConfig:
    lookback_days: int = 30
    max_pending_claims: int = 2


class VolunteerSelector:
    """Selects the best volunteer for a refill based on fairness criteria."""

    def __init__(self, volunteers: list[Volunteer], config: Optional[SelectorConfig] = None):
        self.volunteers = {v.id: v for v in volunteers}
        self.config = config or SelectorConfig()

    def get_eligible(self, office: Office, exclude_ids: set[str] = None) -> list[Volunteer]:
        """Get volunteers eligible for a refill in the given office."""
        exclude_ids = exclude_ids or set()
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.config.lookback_days)

        eligible = []
        for v in self.volunteers.values():
            if v.id in exclude_ids:
                continue
            if not v.is_eligible_for_office(office):
                continue
            # Note: refill_count_30d should be pre-computed and set on the volunteer
            # Don't overwrite it here - it's set by the service based on actual history
            eligible.append(v)

        return eligible

    def rank_volunteers(self, volunteers: list[Volunteer]) -> list[Volunteer]:
        """Rank by: fewest refills in lookback window, then longest since last refill, then ID."""
        now = datetime.now(timezone.utc)

        def sort_key(v: Volunteer):
            # Fewest refills in last N days (lower is better)
            refills_recent = v.refill_count_30d
            # Longest since last refill (older is better, so negate)
            last_refill = v.last_refill_at or datetime.min.replace(tzinfo=timezone.utc)
            days_since_last = (now - last_refill).days
            # Deterministic tiebreak by ID
            return (refills_recent, -days_since_last, v.id)

        return sorted(volunteers, key=sort_key)

    def select_next(self, office: Office, exclude_ids: set[str] = None) -> Optional[Volunteer]:
        """Select the best eligible volunteer for the given office."""
        eligible = self.get_eligible(office, exclude_ids)
        if not eligible:
            return None
        ranked = self.rank_volunteers(eligible)
        return ranked[0]

    def update_volunteer_stats(self, volunteer_id: str, refill_count_30d: int, last_refill_at: datetime):
        """Update volunteer statistics after a refill."""
        if volunteer_id in self.volunteers:
            self.volunteers[volunteer_id].refill_count_30d = refill_count_30d
            self.volunteers[volunteer_id].last_refill_at = last_refill_at
            self.volunteers[volunteer_id].pending_claims += 1

    def release_volunteer(self, volunteer_id: str):
        """Release a volunteer after refill completion or timeout."""
        if volunteer_id in self.volunteers:
            self.volunteers[volunteer_id].pending_claims = max(
                0, self.volunteers[volunteer_id].pending_claims - 1
            )
