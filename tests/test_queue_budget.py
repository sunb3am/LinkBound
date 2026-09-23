from app import db
from app.safety import remaining_queue_budget
from app.settings import SafetyConfig
from datetime import datetime, timezone


def _sent(operator):
    batch_id, _ = db.create_batch(operator, "budget", "connect", False, 1)
    db.record_outcome(
        batch_id=batch_id, operator=operator,
        linkedin_url=f"https://linkedin.com/in/{operator}",
        full_name=operator, first_name=operator, company_csv="", role="", email="",
        action_requested="connect", action_executed="connect", template_id=None,
        template_name="", message_rendered="", status="sent",
    )


def test_rolling_queue_budget_counts_confirmed_sends_per_account(tmp_path):
    db.close_db()
    db.init_db(tmp_path / "budget.sqlite")
    try:
        _sent("me")
        assert remaining_queue_budget("me", SafetyConfig(daily_cap=100, queue_weekly_cap=1)) == 0
        assert remaining_queue_budget("me", SafetyConfig(daily_cap=1, queue_weekly_cap=100)) == 0
        assert remaining_queue_budget("other", SafetyConfig(daily_cap=1, queue_weekly_cap=1)) == 1
    finally:
        db.close_db()


def test_uncertain_claim_consumes_budget_until_reconciled(tmp_path):
    db.close_db()
    db.init_db(tmp_path / "uncertain-budget.sqlite")
    try:
        due = "2026-09-23T09:00:00+00:00"
        campaign_id = db.create_queued_campaign(
            "me", "Possible send", "connect", "UTC", 1, due,
            [{"job": {"linkedin_url": "https://linkedin.com/in/possibly-sent"},
              "available_at_utc": due}],
        )
        target_id = db.list_campaign_targets(campaign_id)[0]["id"]
        now = datetime.now(timezone.utc)
        assert db.claim_campaign_target(target_id, 44, now.isoformat())
        assert db.mark_inflight_targets_uncertain(44) == 1
        assert remaining_queue_budget("me", SafetyConfig(daily_cap=1, queue_weekly_cap=1), now) == 0
    finally:
        db.close_db()
