import json
import sqlite3
from pathlib import Path

import pytest

from scripts.backup_state import restore_snapshot, snapshot_state, verify_snapshot


def _database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE records (value TEXT NOT NULL)")
        conn.execute("INSERT INTO records VALUES ('before snapshot')")


def test_snapshot_uses_valid_sqlite_backup_and_copies_attachments(tmp_path):
    database = tmp_path / "live.sqlite"
    attachments = tmp_path / "uploads"
    attachments.mkdir()
    (attachments / "nested").mkdir()
    (attachments / "nested" / "note.txt").write_text("private attachment", encoding="utf-8")
    _database(database)
    output = tmp_path / "snapshot"

    snapshot_state(database, output, attachments)
    verify_snapshot(output)

    with sqlite3.connect(output / "database.sqlite") as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("SELECT value FROM records").fetchone() == ("before snapshot",)
    assert (output / "attachments" / "nested" / "note.txt").read_text(encoding="utf-8") == "private attachment"
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert {entry["path"] for entry in manifest["files"]} == {
        "database.sqlite", "attachments/nested/note.txt"
    }


def test_snapshot_preserves_screenshots_and_uploaded_files(tmp_path):
    database = tmp_path / "live.sqlite"
    _database(database)
    screenshots = tmp_path / "screenshots"
    uploads = tmp_path / "uploads"
    screenshots.mkdir()
    uploads.mkdir()
    (screenshots / "proof.png").write_bytes(b"image")
    (uploads / "source.csv").write_text("name\nPerson\n", encoding="utf-8")

    output = tmp_path / "snapshot"
    snapshot_state(database, output, screenshots=screenshots, uploads=uploads)
    verify_snapshot(output)
    restored = tmp_path / "restored"
    restore_snapshot(output, restored)

    assert (restored / "screenshots" / "proof.png").read_bytes() == b"image"
    assert (restored / "uploads" / "source.csv").read_text(encoding="utf-8") == "name\nPerson\n"


def test_verify_rejects_tampering_and_unlisted_files(tmp_path):
    database = tmp_path / "live.sqlite"
    _database(database)
    output = tmp_path / "snapshot"
    snapshot_state(database, output)

    (output / "database.sqlite").write_bytes((output / "database.sqlite").read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        verify_snapshot(output)

    snapshot_state(database, tmp_path / "clean")
    (tmp_path / "clean" / "extra").write_text("unlisted", encoding="utf-8")
    with pytest.raises(ValueError, match="file set differs"):
        verify_snapshot(tmp_path / "clean")


def test_restore_requires_new_directory_and_does_not_overwrite(tmp_path):
    database = tmp_path / "live.sqlite"
    _database(database)
    snapshot = tmp_path / "snapshot"
    snapshot_state(database, snapshot)
    target = tmp_path / "restored"

    restore_snapshot(snapshot, target)
    with sqlite3.connect(target / "database.sqlite") as conn:
        assert conn.execute("SELECT value FROM records").fetchone() == ("before snapshot",)
    with pytest.raises(FileExistsError, match="must not already exist"):
        restore_snapshot(snapshot, target)
    assert (target / "database.sqlite").exists()


def test_snapshot_refuses_existing_destination(tmp_path):
    database = tmp_path / "live.sqlite"
    _database(database)
    output = tmp_path / "snapshot"
    output.mkdir()
    (output / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError):
        snapshot_state(database, output)
    assert (output / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_snapshot_and_restore_reject_nested_destinations(tmp_path):
    database = tmp_path / "live.sqlite"
    _database(database)
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    with pytest.raises(ValueError, match="outside attachments"):
        snapshot_state(database, attachments / "snapshot", attachments)

    snapshot = tmp_path / "snapshot"
    snapshot_state(database, snapshot)
    with pytest.raises(ValueError, match="outside the snapshot"):
        restore_snapshot(snapshot, snapshot / "restored")
