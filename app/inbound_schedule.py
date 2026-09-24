"""One daily no-send inbox run inside the single LinkBound app process."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from zoneinfo import ZoneInfo

from . import inbound_store
from .inbox_sync import scan_account


_LOG = logging.getLogger(__name__)
DAILY_LIST_ROW_LIMIT = 20


async def run_due_once(manager, settings, now: datetime | None = None) -> bool:
    schedule = settings.inbound_schedule
    if not schedule.enabled or manager.active() is not None:
        return False
    if schedule.operator not in settings.operators:
        raise ValueError("Scheduled inbound account is not configured")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Inbound schedule clock must include a UTC offset")
    local_now = now.astimezone(ZoneInfo(schedule.timezone))
    if local_now.strftime("%H:%M") < schedule.time_local:
        return False
    latest = inbound_store.list_sync_runs(schedule.operator, 1)
    if latest and datetime.fromisoformat(latest[0]["started_at"]).astimezone(
        ZoneInfo(schedule.timezone)
    ).date() == local_now.date():
        return False
    result = await scan_account(
        settings, manager, schedule.operator,
        max_rows_per_folder=schedule.max_rows_per_folder,
        max_list_rows=DAILY_LIST_ROW_LIMIT,
    )
    _LOG.info("Scheduled inbound scan finished: run=%s status=%s stopped=%s",
              result["run_id"], result["status"], result["stopped"])
    return True


async def inbound_loop(manager, settings) -> None:
    while True:
        try:
            await run_due_once(manager, settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOG.exception("Scheduled inbound poll failed")
        await asyncio.sleep(60)
