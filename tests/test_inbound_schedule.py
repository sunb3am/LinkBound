import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from app import inbound_schedule


class Coordinator:
    def __init__(self, busy=False):
        self.busy = busy

    def active(self):
        return object() if self.busy else None


def settings(enabled=True):
    return SimpleNamespace(
        operators={"me": object()},
        inbound_schedule=SimpleNamespace(
            enabled=enabled, operator="me", time_local="18:00",
            timezone="America/Los_Angeles", max_rows_per_folder=2,
        ),
    )


def test_daily_inbound_runs_once_after_local_time(monkeypatch):
    calls = []
    runs = []

    async def scan(_settings, _manager, operator, *, max_rows_per_folder, max_list_rows):
        calls.append((operator, max_rows_per_folder, max_list_rows))
        runs.insert(0, {"started_at": "2026-09-24T01:05:00+00:00"})
        return {"run_id": 1, "status": "partial", "stopped": False}

    monkeypatch.setattr(inbound_schedule, "scan_account", scan)
    monkeypatch.setattr(inbound_schedule.inbound_store, "list_sync_runs", lambda *_: runs)
    before = datetime(2026, 9, 24, 0, 59, tzinfo=timezone.utc)
    due = datetime(2026, 9, 24, 1, 5, tzinfo=timezone.utc)
    assert asyncio.run(inbound_schedule.run_due_once(Coordinator(), settings(), before)) is False
    assert asyncio.run(inbound_schedule.run_due_once(Coordinator(busy=True), settings(), due)) is False
    assert asyncio.run(inbound_schedule.run_due_once(Coordinator(), settings(), due)) is True
    assert asyncio.run(inbound_schedule.run_due_once(Coordinator(), settings(), due)) is False
    assert calls == [("me", 2, 20)]


def test_daily_inbound_disabled_by_default(monkeypatch):
    monkeypatch.setattr(inbound_schedule.inbound_store, "list_sync_runs",
                        lambda *_: (_ for _ in ()).throw(AssertionError("unexpected DB read")))
    assert asyncio.run(inbound_schedule.run_due_once(
        Coordinator(), settings(enabled=False),
        datetime(2026, 9, 24, 1, 5, tzinfo=timezone.utc),
    )) is False
