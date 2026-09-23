import asyncio
from types import SimpleNamespace

from app import db
from app import orchestrator as orchestrator_module
from app.models import ItemStatus
from app.runner import ProfileResult


def test_second_target_is_rechecked_before_browser_action(tmp_path, monkeypatch):
    db.close_db()
    db.init_db(tmp_path / "presend.sqlite")
    processed = []

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
            processed.append(job["linkedin_url"])
            return ProfileResult(status=ItemStatus.SENT, action_executed="connect")

        async def close(self):
            pass

    class FakeGovernor:
        def __init__(self, settings, operator):
            pass

        def daily_cap_reached(self):
            return False

        def within_business_hours(self):
            return True

        def next_delay_seconds(self):
            return 0

    monkeypatch.setattr(orchestrator_module, "LinkedInRunner", FakeRunner)
    monkeypatch.setattr(orchestrator_module, "SafetyGovernor", FakeGovernor)
    settings = SimpleNamespace(
        safety=SimpleNamespace(stop_on_limit_warning=True),
        behavior=SimpleNamespace(
            inmail_enabled=False, allow_noteless_fallback=False,
            message_if_connected=False,
        ),
    )
    jobs = [{
        "linkedin_url": "linkedin.com/in/person", "precomputed_status": "queued",
        "full_name": "Person", "first_name": "Person", "row_index": index,
    } for index in (0, 1)]

    async def run():
        orch = orchestrator_module.Orchestrator(settings)
        await orch.start(jobs, "sender-a", action="connect", dry_run=False,
                         batch_name="duplicate", send_on_mismatch=False)
        assert orch.batch_id is not None
        await orch._task
        return orch

    try:
        orch = asyncio.run(run())
        assert len(processed) == 1
        assert [row["status"] for row in db.list_requests(orch.batch_id)] == [
            "sent", "skipped_dedup"
        ]
        assert orch.totals["sent"] == 1
    finally:
        db.close_db()
