"""Claim due campaign chunks through the existing one-browser run coordinator."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from zoneinfo import ZoneInfo

from . import db
from .safety import SafetyGovernor, remaining_queue_budget

_LOG = logging.getLogger(__name__)


async def run_due_once(manager, settings, now: datetime | None = None) -> bool:
    """Start one due chunk if permitted; individual targets claim inside the runner."""
    if not settings.allow_live_sends or manager.active() is not None:
        return False
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Queue clock must include a UTC offset")
    now = now.astimezone(timezone.utc)
    now_utc = now.isoformat(timespec="seconds")
    for first in db.list_due_campaign_heads(now_utc):
        campaign_id = first["campaign_id"]
        operator = first["operator"]
        local_now = now.astimezone(ZoneInfo(first["campaign_timezone"]))
        local_date = local_now.date().isoformat()
        if first["campaign_last_run_local_date"] == local_date:
            continue
        if not SafetyGovernor(settings.safety, operator).within_business_hours(local_now):
            continue
        remaining = remaining_queue_budget(operator, settings.safety, now)
        if remaining <= 0:
            continue
        chunk_size = min(first["campaign_daily_chunk"], remaining)
        rows = db.list_due_campaign_chunk(campaign_id, now_utc, chunk_size)
        if not rows or not db.reserve_campaign_day(campaign_id, local_date):
            continue
        jobs = []
        for row in rows:
            job = json.loads(row["job_json"])
            job["queue_target_id"] = row["id"]
            job["campaign_id"] = campaign_id
            job["campaign_timezone"] = first["campaign_timezone"]
            job["campaign_local_date"] = local_date
            jobs.append(job)
        options = json.loads(first["campaign_run_options"] or "{}")
        await manager.start(
            operator, jobs, action=first["campaign_action"], dry_run=False,
            batch_name=f"{first['campaign_name']} | {local_date}",
            send_on_mismatch=bool(options.get("send_on_mismatch", False)),
            ai_personalize=bool(options.get("ai_personalize", False)),
            ai_voice=str(options.get("ai_voice", "auto")),
            exit_node_id=str(options.get("exit_node_id") or ""),
        )
        return True
    return False


async def queue_loop(manager, settings) -> None:
    while True:
        try:
            await run_due_once(manager, settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOG.exception("Scheduled outbound poll failed")
        await asyncio.sleep(15)
