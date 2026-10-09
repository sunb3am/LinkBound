from datetime import datetime, timezone

import pytest

from app import db
from app.safety import remaining_queue_budget
from app.settings import SafetyConfig


@pytest.fixture
def outreach_db(tmp_path):
    db.close_db()
    db.init_db(tmp_path / "outreach.sqlite")
    yield
    db.close_db()


def _outcome(batch_id, url, uncertainty_id, status):
    return db.record_outcome(
        batch_id=batch_id, operator="me", linkedin_url=url,
        full_name="Ada", first_name="Ada", company_csv="", role="", email="",
        action_requested="connect", action_executed="connect", template_id=None,
        template_name="", message_rendered="", status=status,
        uncertainty_id=uncertainty_id,
    )


def test_immediate_marker_blocks_retry_until_review_and_records_verdict(outreach_db):
    url = "https://linkedin.com/in/possibly-sent"
    batch_id, _ = db.create_batch("me", "immediate", "connect", False, 1)
    marker = db.begin_immediate_attempt(batch_id, "me", url)
    assert db.is_already_contacted(url, set(), "other")
    assert remaining_queue_budget("me", SafetyConfig(daily_cap=1, queue_weekly_cap=1)) == 0
    assert db.list_unresolved_outreach("me") == []
    assert not db.review_outreach_uncertainty(marker, "me", "not_sent", "premature")
    request_id, _ = _outcome(batch_id, url, marker, "failed_other")
    db.finalize_batch(batch_id, "finished")
    pending = db.list_unresolved_outreach("me")
    assert len(pending) == 1
    assert pending[0]["request_id"] == request_id
    assert db.list_unresolved_outreach("other") == []
    with pytest.raises(ValueError, match="note"):
        db.review_outreach_uncertainty(marker, "me", "not_sent", "")
    assert not db.review_outreach_uncertainty(marker, "other", "not_sent", "checked")
    assert db.review_outreach_uncertainty(marker, "me", "not_sent", "No invitation in Sent")
    assert not db.is_already_contacted(url, set(), "other")
    assert remaining_queue_budget("me", SafetyConfig(daily_cap=1, queue_weekly_cap=1)) == 1
    assert db.list_unresolved_outreach("me") == []
    assert not db.review_outreach_uncertainty(marker, "me", "sent", "duplicate review")


def test_human_confirmed_send_remains_suppressed_and_budgeted(outreach_db):
    url = "https://linkedin.com/in/confirmed-by-person"
    batch_id, _ = db.create_batch("me", "immediate", "connect", False, 1)
    marker = db.begin_immediate_attempt(batch_id, "me", url)
    db.finalize_batch(batch_id, "interrupted")
    assert db.review_outreach_uncertainty(marker, "me", "sent", "Invitation visible in Sent")
    assert db.is_already_contacted(url, set(), "other")
    assert db.get_account_contact("me", url)["last_observed_status"] == "sent"
    assert remaining_queue_budget("me", SafetyConfig(daily_cap=1, queue_weekly_cap=1)) == 0
    with pytest.raises(ValueError, match="unresolved or confirmed outreach"):
        db.create_queued_campaign(
            "other", "duplicate", "connect", "UTC", 1,
            datetime.now(timezone.utc).isoformat(),
            [{"job": {"linkedin_url": url}, "available_at_utc": datetime.now(timezone.utc).isoformat()}],
        )


def test_crashed_queue_target_requires_review_and_never_auto_requeues(outreach_db):
    url = "https://linkedin.com/in/crashed-queue"
    due = datetime.now(timezone.utc).isoformat()
    campaign_id = db.create_queued_campaign(
        "me", "crash", "connect", "UTC", 1, due,
        [{"job": {"linkedin_url": url}, "available_at_utc": due}],
    )
    target_id = db.list_campaign_targets(campaign_id)[0]["id"]
    assert db.claim_campaign_target(target_id, 19, datetime.now(timezone.utc).isoformat())
    assert db.mark_inflight_targets_uncertain(19) == 1
    assert db.campaign_status(campaign_id) == "paused"
    item = db.list_unresolved_outreach("me")[0]
    assert item["target_id"] == target_id
    assert db.review_outreach_uncertainty(item["id"], "me", "not_sent", "No invitation in Sent")
    assert db.list_campaign_targets(campaign_id)[0]["state"] == "failed"
    assert db.campaign_status(campaign_id) == "paused"
