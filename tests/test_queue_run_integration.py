import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import db, orchestrator as orchestrator_module
from app.coordinator import RunCoordinator
from app.models import ItemStatus
from app.queue_worker import run_due_once
from app.runner import ProfileResult
from app.settings import SafetyConfig


def test_scheduled_target_uses_existing_runner_and_is_not_restarted(tmp_path, monkeypatch):
    db.close_db()
    db.init_db(tmp_path / "run.sqlite")
    visited = []

    class FakeRunner:
        def __init__(self, settings, operator):
            self.operator = operator

        async def start(self):
            pass

        async def open_feed(self):
            pass

        async def logged_in_now(self):
            return True

        async def process(self, job, **kwargs):
            visited.append(job["linkedin_url"])
            return ProfileResult(status=ItemStatus.SENT, action_executed="connect")

        async def close(self):
            pass

    monkeypatch.setattr(orchestrator_module, "LinkedInRunner", FakeRunner)
    now = datetime.now(timezone.utc)
    due = (now - timedelta(minutes=1)).isoformat(timespec="seconds")
    campaign_id = db.create_queued_campaign(
        "me", "One target", "connect", "UTC", 1, due,
        [{"job": {"linkedin_url": "https://www.linkedin.com/in/ada", "row_index": 0,
                   "first_name": "Ada", "full_name": "Ada", "precomputed_status": "queued"},
          "available_at_utc": due}],
    )
    settings = SimpleNamespace(
        allow_live_sends=True,
        safety=SafetyConfig(daily_cap=100, queue_weekly_cap=100, min_delay_seconds=0,
                            max_delay_seconds=0, business_hours_only=False),
        behavior=SimpleNamespace(inmail_enabled=False, allow_noteless_fallback=False,
                                 message_if_connected=False),
    )

    async def run():
        manager = RunCoordinator(settings, None)
        assert await run_due_once(manager, settings, now)
        await manager.get("me")._task
        assert not await run_due_once(manager, settings, now)

    try:
        asyncio.run(run())
        assert visited == ["https://www.linkedin.com/in/ada"]
        target = db.list_campaign_targets(campaign_id)[0]
        assert target["state"] == "sent"
        assert target["request_id"] is not None
        assert db.campaign_status(campaign_id) == "finished"
    finally:
        db.close_db()


def test_immediate_run_marks_possible_action_before_browser_step_and_stops_on_unknown(tmp_path, monkeypatch):
    db.close_db()
    db.init_db(tmp_path / "immediate.sqlite")
    visited = []

    class FakeRunner:
        def __init__(self, settings, operator):
            pass

        async def start(self):
            pass

        async def open_feed(self):
            pass

        async def logged_in_now(self):
            return True

        async def process(self, job, **kwargs):
            visited.append(job["linkedin_url"])
            assert db.list_unresolved_outreach("me") == []
            assert db.is_already_contacted(job["linkedin_url"], set(), "other")
            return ProfileResult(status=ItemStatus.FAILED_OTHER, detail="send not confirmed")

        async def close(self):
            pass

    monkeypatch.setattr(orchestrator_module, "LinkedInRunner", FakeRunner)
    settings = SimpleNamespace(
        allow_live_sends=True,
        safety=SafetyConfig(daily_cap=100, queue_weekly_cap=100, min_delay_seconds=0,
                            max_delay_seconds=0, business_hours_only=False),
        behavior=SimpleNamespace(inmail_enabled=False, allow_noteless_fallback=False,
                                 message_if_connected=False),
    )
    jobs = [{"linkedin_url": f"https://linkedin.com/in/immediate-{index}",
             "precomputed_status": "queued"} for index in range(2)]

    async def run():
        manager = RunCoordinator(settings, None)
        await manager.start("me", jobs, action="connect", dry_run=False,
                            batch_name="immediate test", send_on_mismatch=False)
        await manager.get("me")._task

    try:
        asyncio.run(run())
        assert visited == [jobs[0]["linkedin_url"]]
        assert db.is_already_contacted(jobs[0]["linkedin_url"], set(), "other")
        assert not db.is_already_contacted(jobs[1]["linkedin_url"], set(), "other")
        assert len(db.list_unresolved_outreach("me")) == 1
    finally:
        db.close_db()


def test_scheduled_run_missing_login_pauses_without_holding_browser_or_day(tmp_path, monkeypatch):
    db.close_db()
    db.init_db(tmp_path / "missing-login.sqlite")

    class SignedOutRunner:
        def __init__(self, settings, operator):
            pass

        async def start(self):
            pass

        async def open_feed(self):
            pass

        async def logged_in_now(self):
            return False

        async def process(self, job, **kwargs):
            raise AssertionError("Signed-out account must not process a target")

        async def close(self):
            pass

    monkeypatch.setattr(orchestrator_module, "LinkedInRunner", SignedOutRunner)
    now = datetime.now(timezone.utc)
    due = (now - timedelta(minutes=1)).isoformat(timespec="seconds")
    campaign_id = db.create_queued_campaign(
        "me", "Signed out", "connect", "UTC", 1, due,
        [{"job": {"linkedin_url": "https://linkedin.com/in/signed-out",
                  "precomputed_status": "queued"}, "available_at_utc": due}],
    )
    settings = SimpleNamespace(
        allow_live_sends=True,
        safety=SafetyConfig(daily_cap=100, queue_weekly_cap=100,
                            business_hours_only=False),
        behavior=SimpleNamespace(inmail_enabled=False, allow_noteless_fallback=False,
                                 message_if_connected=False),
    )

    async def run():
        manager = RunCoordinator(settings, None)
        assert await run_due_once(manager, settings, now)
        await manager.get("me")._task
        assert manager.active() is None

    try:
        asyncio.run(run())
        assert db.campaign_status(campaign_id) == "paused"
        assert db.get_queued_campaign(campaign_id, "me")["pause_reason"] == "LinkedIn login required"
        assert db.list_campaign_targets(campaign_id)[0]["state"] == "queued"
        assert db._conn().execute(
            "SELECT last_run_local_date FROM campaigns WHERE id=?", (campaign_id,)
        ).fetchone()[0] is None
    finally:
        db.close_db()
