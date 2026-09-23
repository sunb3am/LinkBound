import asyncio
import hashlib
import json

import httpx
import pytest
from fastapi import FastAPI

from app import db, inbound, inbound_store


@pytest.fixture
def database(tmp_path):
    db.close_db()
    db.init_db(tmp_path / "outbound.db")
    db.create_operator("sender_a", "Sender A", "profiles/sender_a")
    db.create_operator("sender_b", "Sender B", "profiles/sender_b")
    yield tmp_path
    db.close_db()


def _inventory(operator, *, thread="thread-100", section="focused", preview="Hello", complete=True):
    run = inbound_store.start_sync_run(operator, expected_sections=(section,))
    counts = inbound_store.record_inventory_section(run, section, [{
        "thread_key": thread,
        "participant_name": "Pat Lee",
        "preview_text": preview,
        "linkedin_unread": True,
    }], complete=complete)
    result = inbound_store.finish_sync_run(run)
    return run, counts, result


def test_operator_identity_binding_is_unique_and_preserved_with_inbox_history(database):
    url = db.set_operator_self_profile_url(
        "sender_a", "https://www.linkedin.com/in/Sender-A/?trk=menu"
    )
    assert url == "https://linkedin.com/in/sender-a"
    assert db.set_operator_self_profile_url("sender_a", url) == url
    with pytest.raises(ValueError, match="profile URL"):
        db.set_operator_self_profile_url("sender_b", "https://linkedin.com/in/sender-a/recent-activity")
    with pytest.raises(ValueError, match="already bound"):
        db.set_operator_self_profile_url("sender_b", url)
    _inventory("sender_a")
    with pytest.raises(ValueError, match="has history"):
        db.set_operator_self_profile_url("sender_a", "https://linkedin.com/in/other")
    assert next(item for item in db.list_operators() if item["key"] == "sender_a")[
        "linkedin_self_url"
    ] == url


def test_repeated_inventory_is_idempotent_and_review_is_independent(database):
    first, counts, result = _inventory("sender_a")
    assert counts == {"observed": 1, "stored": 1, "unresolved": 0}
    assert result["status"] == "complete"
    row = inbound_store.list_conversations("sender_a")[0]
    assert row["match_state"] == "unmatched"
    assert row["linkedin_unread"] == 1
    assert row["reviewed_at"] is None
    assert inbound_store.mark_reviewed("sender_a", row["id"])

    _, _, result = _inventory("sender_a")
    assert result["status"] == "complete"
    same = inbound_store.list_conversations("sender_a")
    assert len(same) == 1
    assert same[0]["id"] == row["id"]
    assert same[0]["reviewed_at"] is not None
    assert same[0]["linkedin_unread"] == 1

    _inventory("sender_a", preview="New reply")
    assert inbound_store.list_conversations("sender_a")[0]["reviewed_at"] is None
    assert inbound_store.list_sync_runs("sender_a")[0]["id"] > first


def test_unstable_thread_key_does_not_merge_people_or_claim_complete_coverage(database):
    run = inbound_store.start_sync_run("sender_a", expected_sections=("other",))
    counts = inbound_store.record_inventory_section(run, "other", [
        {"thread_key": "", "participant_name": "Same Name", "preview_text": "One"},
        {"thread_key": "", "participant_name": "Same Name", "preview_text": "Two"},
    ], complete=True)
    result = inbound_store.finish_sync_run(run)
    assert counts["unresolved"] == 2
    assert result["status"] == "partial"
    assert result["coverage"]["other"]["status"] == "incomplete"
    assert inbound_store.list_conversations("sender_a") == []


def test_missing_expected_section_is_partial(database):
    run = inbound_store.start_sync_run("sender_a", expected_sections=("focused", "other"))
    inbound_store.record_inventory_section(run, "focused", [], complete=True)
    result = inbound_store.finish_sync_run(run)
    assert result["status"] == "partial"
    assert "other" not in result["coverage"]
    assert inbound_store.list_sync_runs("sender_a")[0]["coverage"]["other"]["status"] == "not_scanned"


def test_unread_state_requires_boolean_or_unknown(database):
    run = inbound_store.start_sync_run("sender_a", expected_sections=("inbox",))
    with pytest.raises(ValueError, match="linkedin_unread"):
        inbound_store.record_inventory_section(run, "inbox", [{
            "thread_key": "thread-1", "linkedin_unread": "false",
        }], complete=True)
    assert inbound_store.list_conversations("sender_a") == []


def test_scan_observation_is_account_scoped_idempotent_and_keeps_original_unread(database):
    run = inbound_store.start_sync_run("sender_a", expected_sections=("focused",))
    inbound_store.record_inventory_section(run, "focused", [{
        "thread_key": "thread-snapshot", "participant_name": "Pat Lee",
        "preview_text": "Original", "linkedin_unread": True,
    }], complete=True)
    conversation = inbound_store.list_conversations("sender_a")[0]

    inbound_store.record_scan_observation("sender_a", run, "thread-snapshot", True)
    inbound_store.record_scan_observation("sender_a", run, "thread-snapshot", True)
    with pytest.raises(ValueError):
        inbound_store.record_scan_observation("sender_b", run, "thread-snapshot", True)

    rows = db._conn().execute(
        "SELECT run_id, conversation_id, linkedin_unread_before_open, restore_status, restored_at, error "
        "FROM conversation_scan_observations"
    ).fetchall()
    assert len(rows) == 1
    assert tuple(rows[0]) == (run, conversation["id"], 1, "pending", None, "")

    # Later inbox inventory changes the latest state, while this run's pre-open
    # snapshot remains the value observed before opening the thread.
    next_run = inbound_store.start_sync_run("sender_a", expected_sections=("focused",))
    inbound_store.record_inventory_section(next_run, "focused", [{
        "thread_key": "thread-snapshot", "participant_name": "Pat Lee",
        "preview_text": "Changed", "linkedin_unread": False,
    }], complete=True)
    assert inbound_store.list_conversations("sender_a")[0]["linkedin_unread"] == 0
    assert db._conn().execute(
        "SELECT linkedin_unread_before_open FROM conversation_scan_observations WHERE run_id=?",
        (run,),
    ).fetchone()[0] == 1


def test_verified_unread_restore_records_outcome_and_latest_state(database):
    run = inbound_store.start_sync_run("sender_a", expected_sections=("focused",))
    inbound_store.record_inventory_section(run, "focused", [{
        "thread_key": "thread-restore", "participant_name": "Pat Lee",
        "preview_text": "Hello", "linkedin_unread": True,
    }], complete=True)
    inbound_store.record_scan_observation("sender_a", run, "thread-restore", True)

    inbound_store.record_unread_restore(
        "sender_a", run, "thread-restore", "restored", True,
    )
    inbound_store.record_unread_restore(
        "sender_a", run, "thread-restore", "restored", True,
    )
    conversation = inbound_store.list_conversations("sender_a")[0]
    assert conversation["linkedin_unread"] == 1
    row = db._conn().execute(
        "SELECT linkedin_unread_before_open, restore_status, restored_at, error "
        "FROM conversation_scan_observations WHERE run_id=?",
        (run,),
    ).fetchone()
    assert tuple(row) == (1, "restored", row["restored_at"], "")
    assert row["restored_at"]

    with pytest.raises(ValueError):
        inbound_store.record_unread_restore(
            "sender_b", run, "thread-restore", "restored", True,
        )
    with pytest.raises(ValueError):
        inbound_store.record_unread_restore(
            "sender_a", run, "thread-restore", "verified", True,
        )


def test_messages_and_files_are_account_scoped_and_byte_verified(database, monkeypatch):
    _, _, _ = _inventory("sender_a")
    conversation_id = inbound_store.list_conversations("sender_a")[0]["id"]
    message_id = inbound_store.record_message(
        "sender_a", conversation_id, source_key="msg-1", direction="inbound",
        body="Here is my resume", source_at="2026-09-23T10:00:00+00:00",
    )
    assert inbound_store.record_message(
        "sender_a", conversation_id, source_key="msg-1", direction="inbound",
        body="Here is my resume", source_at="2026-09-23T10:00:00+00:00",
    ) == message_id
    with pytest.raises(ValueError, match="conflicts"):
        inbound_store.record_message(
            "sender_a", conversation_id, source_key="msg-1", direction="inbound",
            body="Different content", source_at="2026-09-23T10:00:00+00:00",
        )
    payload = b"%PDF-1.4\nresume bytes"
    file_id = inbound_store.save_attachment(
        "sender_a", message_id, source_key="file-1", filename="../../resume.pdf",
        mime_type="application/pdf", data=payload, data_dir=database,
    )
    assert inbound_store.save_attachment(
        "sender_a", message_id, source_key="file-1", filename="resume.pdf",
        mime_type="application/pdf", data=payload, data_dir=database,
    ) == file_id
    path, record = inbound_store.attachment_file("sender_a", file_id, database)
    assert path.read_bytes() == payload
    assert record["sha256"] == hashlib.sha256(payload).hexdigest()
    assert record["filename"] == "resume.pdf"
    assert inbound_store.attachment_file("sender_b", file_id, database) is None
    assert inbound_store.get_conversation("sender_b", conversation_id) is None
    with pytest.raises(ValueError, match="not on this account"):
        inbound_store.record_message("sender_b", conversation_id, source_key="x",
                                     direction="inbound", body="wrong account")
    with pytest.raises(ValueError, match="different bytes"):
        inbound_store.save_attachment(
            "sender_a", message_id, source_key="file-1", filename="resume.pdf",
            mime_type="application/pdf", data=b"other", data_dir=database,
        )
    assert not (database / "inbound_files" / "sender_a" /
                hashlib.sha256(b"other").hexdigest()[:2] /
                hashlib.sha256(b"other").hexdigest()).exists()
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="corrupt"):
        inbound_store.attachment_file("sender_a", file_id, database)


def test_message_direction_and_timestamp_gain_evidence_without_duplicate(database):
    _inventory("sender_a")
    conversation_id = inbound_store.list_conversations("sender_a")[0]["id"]
    message_id = inbound_store.record_message(
        "sender_a", conversation_id, source_key="msg-late-evidence",
        direction="unknown", body="Thanks",
    )
    assert inbound_store.record_message(
        "sender_a", conversation_id, source_key="msg-late-evidence",
        direction="inbound", body="Thanks", source_at="2026-09-23T10:00:00Z",
    ) == message_id
    assert inbound_store.record_message(
        "sender_a", conversation_id, source_key="msg-late-evidence",
        direction="unknown", body="Thanks",
    ) == message_id
    row = db._conn().execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
    assert (row["direction"], row["source_at"]) == ("inbound", "2026-09-23T10:00:00Z")
    with pytest.raises(ValueError, match="conflicts"):
        inbound_store.record_message(
            "sender_a", conversation_id, source_key="msg-late-evidence",
            direction="outbound", body="Thanks",
        )


def test_interrupted_scan_stays_visible(database):
    run = inbound_store.start_sync_run("sender_a", expected_sections=("focused",))
    inbound_store.record_inventory_section(run, "focused", [], complete=True)
    assert inbound_store.mark_interrupted_runs() == 1
    latest = inbound_store.list_sync_runs("sender_a")[0]
    assert latest["id"] == run
    assert latest["status"] == "interrupted"
    assert latest["coverage"]["focused"]["status"] == "complete"


def test_manual_link_can_create_an_inbound_only_contact(database):
    _inventory("sender_a")
    conversation_id = inbound_store.list_conversations("sender_a")[0]["id"]
    assert inbound_store.link_contact("sender_a", conversation_id, "linkedin.com/in/pat-lee")
    conversation = inbound_store.get_conversation("sender_a", conversation_id)
    assert conversation["match_state"] == "matched"
    assert conversation["contact_url"] == "https://linkedin.com/in/pat-lee"
    contact = db.list_contacts("sender_a")[0]
    assert contact["linkedin_url"] == conversation["contact_url"]
    assert contact["full_name"] == "Pat Lee"
    assert db.list_contacts("sender_b") == []


def test_contact_milestones_require_positive_account_scoped_evidence(database):
    url = "https://linkedin.com/in/pat"
    batch, _ = db.create_batch("sender_a", "A", "connect_note", False, 1)
    db.record_outcome(
        batch_id=batch, operator="sender_a", linkedin_url=url,
        full_name="Pat Lee", first_name="Pat", company_csv="", role="", email="",
        action_requested="connect_note", action_executed="connect_note",
        template_id=None, template_name="", message_rendered="Hello", status="sent",
    )
    a = db.list_contacts("sender_a")[0]
    assert a["invited_at"] and not a["accepted_at"] and not a["replied_at"]
    connection_id = inbound_store.record_connection_observation(
        "sender_a", url, source="profile_first_degree"
    )
    assert inbound_store.record_connection_observation(
        "sender_a", url, source="profile_first_degree"
    ) == connection_id
    assert db.list_contacts("sender_a")[0]["accepted_at"]
    assert not db.list_contacts("sender_b")

    run = inbound_store.start_sync_run("sender_a", expected_sections=("focused",))
    inbound_store.record_inventory_section(run, "focused", [{
        "thread_key": "thread-pat", "participant_name": "Pat Lee",
        "contact_url": url, "preview_text": "Thanks", "linkedin_unread": False,
    }], complete=True)
    inbound_store.finish_sync_run(run)
    cid = inbound_store.list_conversations("sender_a")[0]["id"]
    mid = inbound_store.record_message("sender_a", cid, source_key="msg-pat",
                                       direction="inbound", body="Resume attached")
    inbound_store.save_attachment("sender_a", mid, source_key="file-pat",
                                  filename="resume.pdf", mime_type="application/pdf",
                                  data=b"resume", data_dir=database)
    status = db.list_contacts("sender_a")[0]
    assert status["replied_at"] and status["file_received_at"]
    assert status["accepted_at"] and status["invited_at"]


def test_private_routes_enforce_account_scope(database, monkeypatch):
    run, _, _ = _inventory("sender_a")
    cid = inbound_store.list_conversations("sender_a")[0]["id"]
    app = FastAPI()
    app.include_router(inbound.router)
    monkeypatch.setenv("LINKBOUND_DATA_DIR", str(database))

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            own = await client.get("/api/inbound/conversations", params={"operator": "sender_a"})
            assert own.status_code == 200 and len(own.json()["conversations"]) == 1
            other = await client.get(f"/api/inbound/conversations/{cid}",
                                     params={"operator": "sender_b"})
            assert other.status_code == 404
            missing = await client.get("/api/inbound/runs", params={"operator": "missing"})
            assert missing.status_code == 400
            reviewed = await client.post(f"/api/inbound/conversations/{cid}/review",
                                         params={"operator": "sender_a"})
            assert reviewed.status_code == 200
            runs = await client.get("/api/inbound/runs", params={"operator": "sender_a"})
            assert runs.json()["runs"][0]["id"] == run
            export = await client.get("/api/inbound/export", params={"operator": "sender_a"})
            assert export.status_code == 200
            assert export.headers["content-type"] == "application/zip"
            other_export = await client.get("/api/inbound/export", params={"operator": "missing"})
            assert other_export.status_code == 400

    asyncio.run(check())
