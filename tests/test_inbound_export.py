import csv
import hashlib
import io
import json
import zipfile

import pytest

from app import db, inbound_export, inbound_store


@pytest.fixture
def export_data(tmp_path):
    db.close_db()
    data_dir = tmp_path / "data"
    db.init_db(data_dir / "outbound.sqlite")
    db.create_operator("sender_a", "Sender A", "profiles/sender_a")
    db.create_operator("sender_b", "Sender B", "profiles/sender_b")
    yield data_dir
    db.close_db()


def _create_conversation(data_dir, operator, thread_key, person, payload=None):
    run_id = inbound_store.start_sync_run(operator, expected_sections=("inbox",))
    result = inbound_store.record_inventory_section(
        run_id,
        "inbox",
        [{
            "thread_key": thread_key,
            "contact_url": f"https://www.linkedin.com/in/{thread_key}/",
            "participant_name": person,
            "preview_text": "Recent message",
            "linkedin_unread": True,
        }],
        complete=True,
    )
    assert result["stored"] == 1
    inbound_store.finish_sync_run(run_id)
    conversation_id = db._conn().execute(
        "SELECT id FROM conversations WHERE operator=? AND thread_key=?",
        (operator, thread_key),
    ).fetchone()[0]
    message_id = inbound_store.record_message(
        operator,
        conversation_id,
        source_key=f"message-{thread_key}",
        direction="inbound",
        body="I would like to share my resume.",
        source_at="2026-09-22T10:00:00Z",
    )
    if payload is not None:
        inbound_store.save_attachment(
            operator,
            message_id,
            source_key=f"file-{thread_key}",
            filename="Resume final.pdf",
            mime_type="application/pdf",
            data=payload,
            data_dir=data_dir,
        )
    return run_id, conversation_id, message_id


def test_export_is_account_scoped_and_contains_sources_files_and_csv(export_data):
    own_bytes = b"resume bytes for sender A"
    other_bytes = b"private bytes for sender B"
    run_a, conversation_a, _ = _create_conversation(
        export_data, "sender_a", "thread-a", "Candidate A", own_bytes
    )
    run_b, _conversation_b, _ = _create_conversation(
        export_data, "sender_b", "thread-b", "Candidate B", other_bytes
    )

    path = inbound_export.build_export("sender_a", export_data)
    try:
        assert path.is_file()
        assert path.parent == export_data / "exports"
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            assert names == {"manifest.json", "contacts.csv", "files/1-Resume_final.pdf"}
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["operator"] == "sender_a"
            assert manifest["exported_at"]
            assert [run["id"] for run in manifest["sync_runs"]] == [run_a]
            assert [conversation["id"] for conversation in manifest["conversations"]] == [conversation_a]
            assert [message["source_key"] for message in manifest["messages"]] == ["message-thread-a"]
            assert len(manifest["attachments"]) == 1
            attachment = manifest["attachments"][0]
            assert attachment["source_key"] == "file-thread-a"
            assert attachment["filename"] == "Resume final.pdf"
            assert attachment["sha256"] == hashlib.sha256(own_bytes).hexdigest()
            assert attachment["archive_path"] == "files/1-Resume_final.pdf"
            assert archive.read(attachment["archive_path"]) == own_bytes
            assert "thread-b" not in archive.read("manifest.json").decode()
            assert str(run_b) not in {run["id"] for run in manifest["sync_runs"]}

            contacts = list(csv.DictReader(io.StringIO(archive.read("contacts.csv").decode())))
            assert len(contacts) == 1
            assert contacts[0]["participant_name"] == "Candidate A"
            assert contacts[0]["message_count"] == "1"
            assert contacts[0]["file_count"] == "1"
    finally:
        path.unlink(missing_ok=True)


def test_export_checksum_failure_removes_partial_zip(export_data):
    _create_conversation(export_data, "sender_a", "thread-a", "Candidate A", b"original")
    row = db._conn().execute(
        "SELECT relative_path FROM attachments WHERE source_key='file-thread-a'"
    ).fetchone()
    stored_file = export_data / row["relative_path"]
    stored_file.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="checksum"):
        inbound_export.build_export("sender_a", export_data)
    assert list((export_data / "exports").glob("*.zip")) == []


def test_export_includes_only_own_unread_open_intents(export_data):
    own_run = inbound_store.start_sync_run("sender_a", expected_sections=("focused",))
    inbound_store.record_open_intent("sender_a", own_run, "focused", "Own person", "Own preview")
    inbound_store.finish_sync_run(own_run)
    other_run = inbound_store.start_sync_run("sender_b", expected_sections=("focused",))
    inbound_store.record_open_intent("sender_b", other_run, "focused", "Other person", "Other preview")
    inbound_store.finish_sync_run(other_run)

    path = inbound_export.build_export("sender_a", export_data)
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            intents = manifest["inbox_open_intents"]
            assert len(intents) == 1
            assert intents[0]["participant_name"] == "Own person"
            assert "Other person" not in archive.read("manifest.json").decode()
    finally:
        path.unlink(missing_ok=True)


def test_export_neutralizes_spreadsheet_formulas(export_data):
    _create_conversation(export_data, "sender_a", "formula", "=HYPERLINK(\"https://example.com\")")
    path = inbound_export.build_export("sender_a", export_data)
    try:
        with zipfile.ZipFile(path) as archive:
            contacts = list(csv.DictReader(io.StringIO(archive.read("contacts.csv").decode())))
            assert contacts[0]["participant_name"].startswith("'=")
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["conversations"][0]["participant_name"].startswith("=HYPERLINK")
    finally:
        path.unlink(missing_ok=True)
