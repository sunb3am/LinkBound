"""Safety governor: enforces daily caps, randomized delays, and time windows."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from . import db
from .settings import SafetyConfig


class SafetyGovernor:
    def __init__(self, safety: SafetyConfig, operator: str):
        self.safety = safety
        self.operator = operator

    def remaining_budget(self, action: str) -> int:
        return remaining_queue_budget(self.operator, self.safety, action=action)

    def limit_message(self, action: str) -> str:
        if action_family(action) == "message":
            return f"Internal message limit of {self.safety.message_daily_cap} per 24 hours reached."
        return (
            f"Internal invitation limit of {self.safety.daily_cap} per 24 hours "
            f"or {self.safety.queue_weekly_cap} per 7 days reached."
        )

    def within_business_hours(self, now: datetime | None = None) -> bool:
        if not self.safety.business_hours_only:
            return True
        now = now or datetime.now()
        return self.safety.business_hours_start <= now.hour < self.safety.business_hours_end

    def next_delay_seconds(self) -> int:
        lo = self.safety.min_delay_seconds
        hi = max(self.safety.max_delay_seconds, lo)
        return random.randint(lo, hi)


def action_family(action: str) -> str:
    """Unresolved AUTO actions reserve invitation capacity conservatively."""
    value = getattr(action, "value", action)
    return "message" if value in {"message", "inmail"} else "invitation"


def remaining_queue_budget(
    operator: str, safety: SafetyConfig, now: datetime | None = None,
    *, action: str = "connect",
) -> int:
    """Apply separate rolling budgets for invitations and direct messages."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day_start = (now - timedelta(days=1)).isoformat()
    family = action_family(action)
    if family == "message":
        return max(0, safety.message_daily_cap -
                   db.count_sent_since(operator, day_start, family="message"))
    week_start = (now - timedelta(days=7)).isoformat()
    return max(0, min(
        safety.daily_cap - db.count_sent_since(operator, day_start, family="invitation"),
        safety.queue_weekly_cap - db.count_sent_since(operator, week_start, family="invitation"),
    ))
