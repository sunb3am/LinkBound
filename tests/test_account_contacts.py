import sqlite3

import pytest

from app import db


@pytest.fixture
def database(tmp_path):
    db.close_db()
    db.init_db(tmp_path / "contacts.sqlite")
    yield
    db.close_db()


def _record(batch_id, operator, url, status, **overrides):
    data = dict(full_name="Test Person", first_name="Test", company_csv="Acme",
        role="Engineer", email="", action_requested="connect",
        action_executed="connect", template_id=None, template_name="",
        message_rendered="hello", status=status)
    data.update(overrides)
    return db.record_outcome(
        batch_id=batch_id, operator=operator, linkedin_url=url,
        **data,
    )


def test_account_contacts_are_scoped_and_successful_history_is_global(database):
    batch_a, _ = db.create_batch("sender-a", "A", "connect", False, 1)
    batch_b, _ = db.create_batch("sender-b", "B", "connect", False, 1)
    _record(batch_a, "sender-a", "https://www.linkedin.com/in/Person/", "sent")

    assert db.get_account_contact("sender-a", "linkedin.com/in/person")
    assert db.get_account_contact("sender-b", "linkedin.com/in/person") is None
    assert db.is_already_contacted("linkedin.com/in/person", {"pending"}, "sender-b")
    assert not db.should_suppress_contact(
        "linkedin.com/in/person", set(), "sender-b", global_suppression=False
    )
    assert not db.should_suppress_contact(
        "linkedin.com/in/person", set(), "sender-b", allow_override=True
    )
    assert db.list_account_contacts("sender-a")[0]["has_successful_send"] is True


def test_account_key_with_history_cannot_be_deleted_and_reused(database):
    db.create_operator("sender-a", "Sender A", "profiles/sender-a")
    batch, _ = db.create_batch("sender-a", "A", "connect", False, 1)
    _record(batch, "sender-a", "linkedin.com/in/person", "sent")

    with pytest.raises(ValueError, match="has history"):
        db.delete_operator("sender-a")
    assert any(op["key"] == "sender-a" for op in db.list_operators())


def test_later_skip_does_not_erase_prior_successful_send(database):
    batch, _ = db.create_batch("sender-a", "A", "connect", False, 1)
    _record(batch, "sender-a", "linkedin.com/in/person", "sent")
    _record(batch, "sender-a", "linkedin.com/in/person", "skipped_dedup")

    assert db.get_account_contact("sender-a", "linkedin.com/in/person")["last_observed_status"] == "sent"
    assert db.is_already_contacted("linkedin.com/in/person", set(), "sender-a")


def test_account_observation_is_used_without_global_status_truth(database):
    batch, _ = db.create_batch("sender-a", "A", "connect", False, 1)
    _record(batch, "sender-a", "linkedin.com/in/person", "pending")

    assert db.is_already_contacted("linkedin.com/in/person", {"pending"}, "sender-a")
    assert not db.is_already_contacted("linkedin.com/in/person", {"pending"}, "sender-b")


def test_pending_observation_survives_skips_and_dry_runs(database):
    batch, _ = db.create_batch("sender-a", "A", "connect", False, 3)
    _record(batch, "sender-a", "linkedin.com/in/person", "pending")
    _record(batch, "sender-a", "linkedin.com/in/person", "skipped_dedup")
    _record(batch, "sender-a", "linkedin.com/in/person", "dry_run")

    assert db.get_account_contact("sender-a", "linkedin.com/in/person")["last_observed_status"] == "pending"
    assert db.is_already_contacted("linkedin.com/in/person", {"pending"}, "sender-a")
    assert db.should_suppress_contact(
        "linkedin.com/in/person", {"pending"}, "sender-a", allow_override=True
    )


def test_contact_timeline_scopes_outbound_and_keeps_shared_annotations(database):
    batch_a, _ = db.create_batch("sender-a", "A", "connect", False, 1)
    batch_b, _ = db.create_batch("sender-b", "B", "connect", False, 1)
    _record(batch_a, "sender-a", "linkedin.com/in/person", "sent")
    _record(batch_b, "sender-b", "linkedin.com/in/person", "failed_other")
    conn = db._conn()
    conn.execute("INSERT INTO contact_tags (contact_url, tag, created_at) VALUES (?, ?, ?)",
        ("https://www.linkedin.com/in/person/", "warm", "2026-01-01T00:00:00+00:00"))
    conn.execute("INSERT INTO contact_notes (contact_url, note, created_at) VALUES (?, ?, ?)",
        ("linkedin.com/in/person", "Shared note", "2026-01-02T00:00:00+00:00"))
    conn.commit()

    timeline = db.list_contact_timeline("sender-a", "https://www.linkedin.com/in/person/")
    assert [event["type"] for event in timeline].count("outbound_request") == 1
    assert {event["type"] for event in timeline} == {"outbound_request", "tag", "note"}
    assert any(event.get("note") == "Shared note" for event in timeline)
    request = next(event for event in timeline if event["type"] == "outbound_request")
    assert request["id"] > 0 and request["public_id"].startswith("OBR-")
    assert "operator" in request and "linkedin_url" in request and "decision_trace" in request
    assert next(event for event in timeline if event["type"] == "tag")["id"] > 0
    assert next(event for event in timeline if event["type"] == "note")["id"] > 0
    assert not any(event.get("status") == "failed_other" for event in timeline)


def test_new_outcome_updates_shared_profile_without_changing_legacy_truth(database):
    db._conn().execute(
        """INSERT INTO contacts
               (linkedin_url, normalized_linkedin_url, full_name, first_name,
                company_csv, last_status, operator)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("https://linkedin.com/in/person", "https://linkedin.com/in/person",
         "Old Name", "Old", "Old Co", "sent", "old-account"),
    )
    db._conn().commit()
    batch, _ = db.create_batch("new-account", "New", "connect", False, 1)
    _record(batch, "new-account", "linkedin.com/in/person", "skipped_dedup")

    profile = db.get_contact("linkedin.com/in/person")
    assert profile["full_name"] == "Test Person"
    assert profile["operator"] == "old-account"
    assert profile["last_status"] == "sent"


def test_blank_later_profile_values_preserve_known_details(database):
    batch, _ = db.create_batch("sender-a", "A", "connect", False, 2)
    _record(batch, "sender-a", "linkedin.com/in/person", "sent", headline="Known headline")
    _record(batch, "sender-a", "linkedin.com/in/person", "failed_other",
        full_name="", first_name="", company_csv="", headline="")

    profile = db.get_contact("linkedin.com/in/person")
    assert profile["full_name"] == "Test Person"
    assert profile["first_name"] == "Test"
    assert profile["company_csv"] == "Acme"
    assert profile["headline"] == "Known headline"


def test_failed_only_attempt_is_listed_without_relationship_status(database):
    batch, _ = db.create_batch("sender-a", "A", "connect", False, 1)
    _record(batch, "sender-a", "linkedin.com/in/failed", "failed_other")
    row = db.get_account_contact("sender-a", "linkedin.com/in/failed")
    assert row is not None
    assert row["last_observed_status"] is None
    assert any(item["linkedin_url"] == row["linkedin_url"] for item in db.list_contacts("sender-a"))


def test_versioned_backfill_skips_ambiguous_legacy_attribution(tmp_path):
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE contacts (linkedin_url TEXT PRIMARY KEY, full_name TEXT,
          first_name TEXT, company_csv TEXT, last_status TEXT, template_used TEXT,
          message_sent TEXT, operator TEXT, first_seen_at TEXT, last_action_at TEXT,
          degree TEXT, last_action_type TEXT, headline TEXT);
        CREATE TABLE outbound_requests (id INTEGER PRIMARY KEY, public_id TEXT,
          batch_id INTEGER, operator TEXT, linkedin_url TEXT, full_name TEXT,
          first_name TEXT, company_csv TEXT, role TEXT, email TEXT, action_requested TEXT,
          action_executed TEXT, template_id INTEGER, template_name TEXT, message_rendered TEXT,
          status TEXT, detail TEXT, decision_trace TEXT, screenshot_path TEXT,
          created_at TEXT, completed_at TEXT, headline TEXT);
        INSERT INTO contacts VALUES ('https://linkedin.com/in/a', 'A', 'A', '', 'sent', '', '', 'one', '', '', '', '', '');
        INSERT INTO contacts VALUES ('https://linkedin.com/in/b', 'B', 'B', '', 'sent', '', '', 'one', '', '', '', '', '');
        INSERT INTO contacts VALUES ('https://www.linkedin.com/in/c/', 'Legacy WWW Person', 'Legacy', 'Old Co', 'sent', '', '', 'one', '', '', '', '', '');
        INSERT INTO outbound_requests (operator, linkedin_url, status) VALUES
          ('one', 'https://linkedin.com/in/b', 'sent'),
          ('two', 'https://linkedin.com/in/b', 'sent'),
          ('one', 'https://www.linkedin.com/in/c/', 'sent'),
          ('one', 'https://www.linkedin.com/in/c/', 'skipped_dedup'),
          ('one', 'https://linkedin.com/in/d', 'failed_other'),
          ('one', 'https://linkedin.com/in/e', 'dry_run');
    """)
    conn.commit()
    conn.close()

    db.close_db()
    db.init_db(path)
    inferred = db.get_account_contact("one", "linkedin.com/in/a")
    assert inferred["last_observed_status"] == "legacy_unverified"
    assert inferred["has_successful_send"] is False
    assert db.is_already_contacted("linkedin.com/in/a", set(), "one")
    assert db.get_account_contact("one", "linkedin.com/in/b")
    assert db.get_account_contact("two", "linkedin.com/in/b")
    assert db.get_account_contact("one", "linkedin.com/in/c")["last_observed_status"] == "sent"
    assert db.get_account_contact("one", "linkedin.com/in/d")["last_observed_status"] is None
    assert db.get_account_contact("one", "linkedin.com/in/e") is None
    assert db.is_already_contacted("https://linkedin.com/in/c", set(), "new-account")
    legacy = next(row for row in db.list_contacts("one") if row["linkedin_url"] == "https://linkedin.com/in/c")
    assert legacy["full_name"] == "Legacy WWW Person"
    assert db._conn().execute("PRAGMA user_version").fetchone()[0] == 6
    db.close_db()


def test_failed_migration_rolls_back_version_and_closes_connection(tmp_path, monkeypatch):
    path = tmp_path / "failed-migration.sqlite"

    def fail_after_version_change(conn):
        conn.execute("PRAGMA user_version = 99")
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(db, "_migrate_account_contacts", fail_after_version_change)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        db.init_db(path)

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    conn.close()
    assert db._CONN is None
