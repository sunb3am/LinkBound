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

    def remaining_today(self) -> int:
        sent = db.count_sent_today(self.operator)
        return max(0, self.safety.daily_cap - sent)

    def daily_cap_reached(self) -> bool:
        return self.remaining_today() <= 0

    def within_business_hours(self, now: datetime | None = None) -> bool:
        if not self.safety.business_hours_only:
            return True
        now = now or datetime.now()
        return self.safety.business_hours_start <= now.hour < self.safety.business_hours_end

    def next_delay_seconds(self) -> int:
        lo = self.safety.min_delay_seconds
        hi = max(self.safety.max_delay_seconds, lo)
        return random.randint(lo, hi)


def remaining_queue_budget(operator: str, safety: SafetyConfig, now: datetime | None = None) -> int:
    """Apply rolling 24-hour and 7-day caps across all queued campaigns on an account."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day_start = (now - timedelta(days=1)).isoformat()
    week_start = (now - timedelta(days=7)).isoformat()
    return max(0, min(
        safety.daily_cap - db.count_sent_since(operator, day_start),
        safety.queue_weekly_cap - db.count_sent_since(operator, week_start),
    ))
