import json
import pytest

from app import db


@pytest.fixture
def queue_db(tmp_path):
    db.close_db()
    path = tmp_path / "queue.sqlite"
    db.init_db(path)
    yield path
    db.close_db()


def _target(url, available="2026-09-23T10:00:00+00:00", **extra):
    job = {"linkedin_url": url, "first_name": "Ada", **extra}
    return {"job": job, "available_at_utc": available}


def _campaign(targets=None):
    return db.create_queued_campaign(
        "me", "Autumn outreach", "connect_note", "America/Los_Angeles", 20,
        "2026-09-23T10:00:00+00:00", targets or [_target("https://www.linkedin.com/in/ada/")],
    )


def test_schema_v3_and_campaign_targets_survive_reopen(queue_db):
    campaign_id = _campaign([_target("https://www.linkedin.com/in/ada/", note="hello")])
    target_before = db.list_campaign_targets(campaign_id)[0]
    db.close_db()

    db.init_db(queue_db)
    target_after = db.list_campaign_targets(campaign_id)[0]
    assert int(db._conn().execute("PRAGMA user_version").fetchone()[0]) == 5
    assert target_after["state"] == "queued"
    assert target_after["normalized_linkedin_url"] == "https://linkedin.com/in/ada"
    assert target_after["job"] == target_before["job"] == {
        "linkedin_url": "https://www.linkedin.com/in/ada/", "first_name": "Ada", "note": "hello"
    }
    campaign = db._conn().execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    assert campaign["timezone"] == "America/Los_Angeles"
    assert campaign["daily_chunk"] == 20
    assert campaign["start_at_utc"] == "2026-09-23T10:00:00+00:00"


def test_source_and_validation_report_are_retained_with_campaign(queue_db):
    original = b"linkedin_url\nhttps://linkedin.com/in/ada\n"
    campaign_id = db.create_queued_campaign(
        "me", "Source archive", "connect", "America/Los_Angeles", 20,
        "2026-09-23T10:00:00+00:00", [_target("https://linkedin.com/in/ada")],
        source_name="candidates.csv", source_bytes=original,
        validation_json='[{"row": 3, "reason": "missing URL"}]',
    )
    db.close_db()
    db.init_db(queue_db)
    row = db._conn().execute("SELECT source_name, source_bytes, validation_json FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    assert row["source_name"] == "candidates.csv"
    assert row["source_bytes"] == original
    assert json.loads(row["validation_json"])[0]["row"] == 3


def test_duplicate_normalized_url_rolls_back_campaign_and_all_targets(queue_db):
    with pytest.raises(ValueError, match="active or unresolved queued outreach"):
        _campaign([
            _target("https://www.linkedin.com/in/ada/"),
            _target("linkedin.com/in/ada"),
        ])
    assert db._conn().execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == 0
    assert db._conn().execute("SELECT COUNT(*) FROM campaign_targets").fetchone()[0] == 0
    assert not db._conn().in_transaction


def test_due_target_claim_is_exclusive_and_pause_hides_remaining_targets(queue_db):
    campaign_id = _campaign([
        _target("https://linkedin.com/in/ada", available="2026-09-23T09:00:00+00:00"),
        _target("https://linkedin.com/in/grace", available="2026-09-23T11:00:00+00:00"),
    ])
    due = db.list_due_targets("2026-09-23T10:00:00+00:00", 10)
    assert len(due) == 1
    assert due[0]["campaign_id"] == campaign_id
    second = db.list_campaign_targets(campaign_id)[1]
    assert not db.claim_campaign_target(second["id"], 40, "2026-09-23T10:00:00+00:00")
    assert db.claim_campaign_target(due[0]["id"], 41, "2026-09-23T10:00:01+00:00")
    assert not db.claim_campaign_target(due[0]["id"], 42, "2026-09-23T10:00:02+00:00")
    assert db.release_campaign_target(due[0]["id"], 41)
    assert db.claim_campaign_target(due[0]["id"], 42, "2026-09-23T10:00:03+00:00")
    assert db.set_campaign_status(campaign_id, "paused")
    assert db.list_due_targets("2026-09-23T12:00:00+00:00", 10) == []
    assert db.set_campaign_status(campaign_id, "queued")
    assert len(db.list_due_targets("2026-09-23T12:00:00+00:00", 10)) == 1


def test_recovery_marks_inflight_uncertain_without_requeue(queue_db):
    campaign_id = _campaign()
    target = db.list_campaign_targets(campaign_id)[0]
    assert db.claim_campaign_target(target["id"], 19, "2026-09-23T10:01:00+00:00")
    assert db.mark_inflight_targets_uncertain() == 1
    recovered = db.list_campaign_targets(campaign_id)[0]
    assert recovered["state"] == "uncertain"
    assert recovered["batch_id"] == 19
    assert "restarted" in recovered["detail"].lower()
    assert db.campaign_status(campaign_id) == "paused"
    assert db.is_already_contacted(target["linkedin_url"], set(), "other")
    assert db.mark_inflight_targets_uncertain() == 0
    assert db.list_due_targets("2026-09-23T12:00:00+00:00", 10) == []


def test_cancel_only_cancels_queued_targets_and_rejects_invalid_transition(queue_db):
    campaign_id = _campaign([
        _target("https://linkedin.com/in/ada"),
        _target("https://linkedin.com/in/grace"),
    ])
    targets = db.list_campaign_targets(campaign_id)
    assert db.claim_campaign_target(targets[0]["id"], 22, "2026-09-23T10:01:00+00:00")
    assert db.set_campaign_status(campaign_id, "cancelled")
    states = {t["state"] for t in db.list_campaign_targets(campaign_id)}
    assert states == {"sending", "cancelled"}
    assert db.list_due_targets("2026-09-23T12:00:00+00:00", 10) == []
    with pytest.raises(ValueError):
        db.set_campaign_status(campaign_id, "queued")
    assert db._conn().execute("SELECT status FROM campaigns WHERE id=?", (campaign_id,)).fetchone()[0] == "cancelled"
    assert not db._conn().in_transaction


def test_active_or_uncertain_target_blocks_another_campaign_and_immediate_run(queue_db):
    first_campaign = _campaign()
    target = db.list_campaign_targets(first_campaign)[0]
    assert db.is_already_contacted(target["linkedin_url"], set(), "other")
    with pytest.raises(ValueError, match="active or unresolved queued outreach"):
        db.create_queued_campaign("other", "duplicate", "connect", "UTC", 1,
                                  "2026-09-23T10:00:00+00:00", [_target(target["linkedin_url"])])
    assert db.claim_campaign_target(target["id"], 21, "2026-09-23T10:01:00+00:00")
    assert not db.is_already_contacted(target["linkedin_url"], set(), "me",
                                       exclude_queue_target_id=target["id"])
    assert db.mark_inflight_targets_uncertain(21) == 1
    assert db.is_already_contacted(target["linkedin_url"], set(), "me")
    with pytest.raises(ValueError, match="unresolved or confirmed outreach"):
        db.create_queued_campaign("other", "still duplicate", "connect", "UTC", 1,
                                  "2026-09-23T10:00:00+00:00", [_target(target["linkedin_url"])])
    assert db._conn().execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == 1


def test_due_results_include_campaign_context(queue_db):
    campaign_id = _campaign()
    due = db.list_due_targets("2026-09-23T12:00:00+00:00", 10)
    assert len(due) == 1
    assert due[0]["campaign_name"] == "Autumn outreach"
    assert due[0]["campaign_action"] == "connect_note"
    assert json.loads(due[0]["job_json"])["first_name"] == "Ada"


def test_claimed_target_and_request_complete_in_one_transaction(queue_db):
    campaign_id = _campaign()
    target = db.list_campaign_targets(campaign_id)[0]
    batch_id, _ = db.create_batch("me", "daily chunk", "connect_note", False, 1)
    outcome = dict(
        batch_id=batch_id, operator="me", linkedin_url=target["linkedin_url"],
        full_name="Ada", first_name="Ada", company_csv="", role="", email="",
        action_requested="connect_note", action_executed="connect_note",
        template_id=None, template_name="", message_rendered="hello", status="sent",
        queue_target_id=target["id"],
    )
    with pytest.raises(ValueError, match="claim is missing"):
        db.record_outcome(**outcome)
    assert db._conn().execute("SELECT COUNT(*) FROM outbound_requests").fetchone()[0] == 0

    assert db.claim_campaign_target(target["id"], batch_id, "2026-09-23T10:00:01+00:00")
    request_id, _ = db.record_outcome(**outcome)
    saved = db.list_campaign_targets(campaign_id)[0]
    assert saved["state"] == "sent"
    assert saved["request_id"] == request_id
    assert db.finish_campaign_if_drained(campaign_id)
    assert not db.finish_campaign_if_drained(campaign_id)
