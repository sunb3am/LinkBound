import sqlite3

import pytest

from app import db


@pytest.fixture
def database(tmp_path):
    db.close_db()
    path = tmp_path / "inbound.sqlite"
    db.init_db(path)
    yield path
    db.close_db()


def test_fresh_database_has_inbound_schema_v10(database):
    conn = db._conn()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 10

    expected = {
        "sync_runs": {
            "id", "operator", "mode", "status", "started_at", "finished_at",
            "expected_sections_json", "coverage_json", "error",
            "exit_node_id", "exit_node_name", "egress_ipv4",
        },
        "conversations": {
            "id", "operator", "thread_key", "contact_url", "participant_name",
            "section", "preview_text", "linkedin_unread", "match_state",
            "first_observed_at", "last_observed_at", "reviewed_at",
        },
        "messages": {
            "id", "conversation_id", "source_key", "direction", "body", "source_at",
            "first_observed_at", "last_observed_at",
        },
        "attachments": {
            "id", "message_id", "source_key", "filename", "mime_type", "size_bytes",
            "sha256", "relative_path", "status", "observed_at",
        },
        "relationship_observations": {
            "id", "operator", "contact_url", "fact", "source",
            "first_observed_at", "last_observed_at",
        },
        "conversation_scan_observations": {
            "run_id", "conversation_id", "linkedin_unread_before_open",
            "restore_status", "restored_at", "error",
        },
        "inbox_open_intents": {
            "id", "run_id", "operator", "section", "participant_name",
            "preview_text", "thread_key", "restore_status", "restored_at",
            "error", "created_at",
        },
    }
    for table, columns in expected.items():
        actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert actual == columns
    assert "linkedin_self_url" in {
        row[1] for row in conn.execute("PRAGMA table_info(operators)")
    }

    indexes = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )}
    assert {
        "idx_sync_runs_operator_latest",
        "idx_conversations_operator_latest",
        "idx_messages_conversation_latest",
        "idx_attachments_message_latest",
        "idx_relationship_observations_operator_latest",
        "idx_open_intents_pending",
        "idx_operators_self_url",
    } <= indexes
    foreign_keys = {
        (row[2], row[3], row[4])
        for row in conn.execute("PRAGMA foreign_key_list(messages)")
    }
    assert ("conversations", "conversation_id", "id") in foreign_keys

    scan_foreign_keys = {
        (row[2], row[3], row[4])
        for row in conn.execute("PRAGMA foreign_key_list(conversation_scan_observations)")
    }
    assert ("sync_runs", "run_id", "id") in scan_foreign_keys
    assert ("conversations", "conversation_id", "id") in scan_foreign_keys


def test_v5_upgrade_preserves_data_and_is_idempotent(database):
    conn = db._conn()
    conn.execute(
        "INSERT INTO batches (public_id, name, operator, status) VALUES (?, ?, ?, ?)",
        ("B-1", "existing batch", "operator-a", "complete"),
    )
    batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO outbound_requests (public_id, batch_id, operator, linkedin_url, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("R-1", batch_id, "operator-a", "https://linkedin.com/in/person", "sent"),
    )
    conn.commit()

    for table in ("inbox_open_intents", "conversation_scan_observations", "attachments", "messages",
                  "conversations", "sync_runs", "relationship_observations"):
        conn.execute(f"DROP TABLE {table}")
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    db.close_db()

    db.init_db(database)
    conn = db._conn()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
    assert tuple(conn.execute(
        "SELECT public_id, operator, status FROM batches"
    ).fetchone()) == ("B-1", "operator-a", "complete")
    assert tuple(conn.execute(
        "SELECT public_id, linkedin_url, status FROM outbound_requests"
    ).fetchone()) == ("R-1", "https://linkedin.com/in/person", "sent")

    schema_before = [tuple(row) for row in conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()]
    db.close_db()
    db.init_db(database)
    conn = db._conn()
    schema_after = [tuple(row) for row in conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()]
    assert schema_after == schema_before
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 10


def test_operator_with_inbound_history_cannot_be_deleted(database):
    db.create_operator("sender-a", "Sender A", "profiles/sender-a")
    conn = db._conn()
    conn.execute(
        "INSERT INTO sync_runs (operator, mode, status, started_at) VALUES (?, ?, ?, ?)",
        ("sender-a", "manual", "complete", "2026-01-01T00:00:00Z"),
    )
    conn.commit()

    with pytest.raises(ValueError, match="has history"):
        db.delete_operator("sender-a")
    assert any(row["key"] == "sender-a" for row in db.list_operators())
