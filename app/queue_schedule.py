"""Turn a local campaign start time and daily chunk size into UTC due times."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _local_to_utc(local: datetime, zone: ZoneInfo) -> datetime:
    valid = []
    for fold in (0, 1):
        utc = local.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == local and utc not in valid:
            valid.append(utc)
    if not valid:
        raise ValueError(f"Local time does not exist due to daylight saving change: {local.isoformat()}")
    if len(valid) > 1:
        raise ValueError(f"Ambiguous local time due to daylight saving change: {local.isoformat()}")
    return valid[0]


def distribute_due_times(
    start_at_local: str, timezone_name: str, daily_chunk: int, target_count: int
) -> list[str]:
    """Return one UTC due time per target, preserving wall-clock time across days."""
    if not 1 <= daily_chunk <= 100:
        raise ValueError("daily_chunk must be between 1 and 100")
    if target_count < 1:
        raise ValueError("A queued campaign needs at least one target")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("A valid IANA timezone is required") from exc
    try:
        start = datetime.fromisoformat(start_at_local)
    except (ValueError, TypeError) as exc:
        raise ValueError("start_at_local must be an ISO local date and time") from exc
    if start.tzinfo is not None:
        raise ValueError("start_at_local must not contain a UTC offset")

    due_by_day = []
    for day in range((target_count - 1) // daily_chunk + 1):
        local = start + timedelta(days=day)
        due_by_day.append(_local_to_utc(local, zone).isoformat(timespec="seconds"))
    return [due_by_day[position // daily_chunk] for position in range(target_count)]
