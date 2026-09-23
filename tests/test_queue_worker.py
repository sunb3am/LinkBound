import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from app import db
from app.queue_worker import run_due_once
from app.settings import SafetyConfig


class FakeManager:
    def __init__(self):
        self.calls = []

    def active(self):
        return None

    async def start(self, operator, jobs, **options):
        self.calls.append((operator, jobs, options))


def test_worker_never_starts_on_no_send_host_and_runs_one_chunk_per_local_day(tmp_path):
    db.close_db()
    db.init_db(tmp_path / "queue.sqlite")
    try:
        campaign_id = db.create_queued_campaign(
            "me", "Recruiting", "connect_note", "UTC", 2,
            "2026-09-23T09:00:00+00:00",
            [
                {"job": {"linkedin_url": f"https://linkedin.com/in/person-{i}", "precomputed_status": "queued"},
                 "available_at_utc": "2026-09-23T09:00:00+00:00" if i < 2 else "2026-09-24T09:00:00+00:00"}
                for i in range(3)
            ],
        )
        manager = FakeManager()
        settings = SimpleNamespace(allow_live_sends=False, safety=SimpleNamespace(
            daily_cap=3, queue_weekly_cap=3, business_hours_only=False))
        today = datetime(2026, 9, 23, 10, tzinfo=timezone.utc)
        assert not asyncio.run(run_due_once(manager, settings, today))
        assert manager.calls == []

        settings.allow_live_sends = True
        assert asyncio.run(run_due_once(manager, settings, today))
        assert len(manager.calls) == 1
        assert len(manager.calls[0][1]) == 2
        assert all(job["campaign_id"] == campaign_id for job in manager.calls[0][1])
        assert not asyncio.run(run_due_once(manager, settings, today))
        # The fake manager does not run a browser, so emulate those two terminal outcomes.
        with db._LOCK:
            db._conn().execute(
                "UPDATE campaign_targets SET state='sent' WHERE campaign_id=? AND ordinal<2",
                (campaign_id,),
            )
            db._conn().commit()
        assert asyncio.run(run_due_once(manager, settings, datetime(2026, 9, 24, 10, tzinfo=timezone.utc)))
        assert len(manager.calls[1][1]) == 1
    finally:
        db.close_db()


def test_large_due_campaign_does_not_hide_another_campaign(tmp_path):
    db.close_db()
    db.init_db(tmp_path / "fairness.sqlite")
    try:
        due = "2026-09-23T09:00:00+00:00"
        first = db.create_queued_campaign(
            "me", "Large", "connect", "UTC", 1, due,
            [{"job": {"linkedin_url": f"https://linkedin.com/in/large-{i}"},
              "available_at_utc": due} for i in range(501)],
        )
        second = db.create_queued_campaign(
            "other", "Small", "connect", "UTC", 1, due,
            [{"job": {"linkedin_url": "https://linkedin.com/in/small"},
              "available_at_utc": due}],
        )
        assert db.reserve_campaign_day(first, "2026-09-23")
        manager = FakeManager()
        settings = SimpleNamespace(
            allow_live_sends=True,
            safety=SimpleNamespace(daily_cap=10, queue_weekly_cap=10,
                                   business_hours_only=False),
        )
        assert asyncio.run(run_due_once(manager, settings,
                                        datetime(2026, 9, 23, 10, tzinfo=timezone.utc)))
        assert manager.calls[0][1][0]["campaign_id"] == second
    finally:
        db.close_db()


def test_outside_business_hours_waits_without_using_daily_reservation(tmp_path):
    db.close_db()
    db.init_db(tmp_path / "hours.sqlite")
    try:
        due = "2026-09-23T05:00:00+00:00"
        campaign_id = db.create_queued_campaign(
            "me", "Early schedule", "connect", "UTC", 1, due,
            [{"job": {"linkedin_url": "https://linkedin.com/in/early"},
              "available_at_utc": due}],
        )
        manager = FakeManager()
        settings = SimpleNamespace(
            allow_live_sends=True,
            safety=SafetyConfig(daily_cap=10, queue_weekly_cap=10,
                                business_hours_only=True,
                                business_hours_start=9, business_hours_end=17),
        )
        assert not asyncio.run(run_due_once(
            manager, settings, datetime(2026, 9, 23, 6, tzinfo=timezone.utc)))
        assert db._conn().execute(
            "SELECT last_run_local_date FROM campaigns WHERE id=?", (campaign_id,)
        ).fetchone()[0] is None
        assert asyncio.run(run_due_once(
            manager, settings, datetime(2026, 9, 23, 10, tzinfo=timezone.utc)))
        assert len(manager.calls) == 1
    finally:
        db.close_db()
