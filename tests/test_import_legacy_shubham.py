"""Proof that a one-time legacy import stays account-scoped and reversible."""

from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
from zipfile import ZipFile

import pytest

from app import db
from scripts.import_legacy_shubham import bundle, import_bundle


def _database(path: Path) -> None:
    db.init_db(path)
    db.close_db()


def _source(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "old"
    database = workspace / "data" / "outbound.db"
    database.parent.mkdir(parents=True)
    _database(database)
    screenshot = workspace / "data" / "screenshots" / "proof.png"
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"Shubham screenshot proof")
    with closing(sqlite3.connect(database)) as conn:
        for batch_id, owner in ((1, "me"), (2, "yt")):
            conn.execute(
                "INSERT INTO batches (id, operator, public_id, status) VALUES (?,?,?,?)",
                (batch_id, owner, f"batch-{owner}", "running"),
            )
            conn.execute(
                "INSERT INTO outbound_requests "
                "(id, batch_id, operator, linkedin_url, status, template_id, screenshot_path) "
                "VALUES (?,?,?,?,?,?,?)",
                (batch_id, batch_id, owner, f"https://linkedin.com/in/{owner}",
                 "sent", 47, "screenshots/proof.png" if owner == "me" else None),
            )
            conn.execute(
                "INSERT INTO account_contacts "
                "(operator,linkedin_url,last_observed_status) VALUES (?,?,?)",
                (owner, f"https://linkedin.com/in/{owner}", "sent"),
            )
            conn.execute(
                "INSERT INTO contacts (linkedin_url,operator,full_name) VALUES (?,?,?)",
                (f"https://linkedin.com/in/{owner}", owner, owner),
            )
        conn.commit()
    return database, workspace, screenshot


def _target(tmp_path: Path) -> Path:
    target = tmp_path / "host.sqlite"
    _database(target)
    with closing(sqlite3.connect(target)) as conn:
        conn.execute(
            "INSERT INTO operators "
            "(key,label,profile_dir,linkedin_self_url) VALUES (?,?,?,?)",
            ("me", "Shubham", "me", "https://linkedin.com/in/shubham"),
        )
        conn.execute(
            "INSERT INTO conversations "
            "(operator,thread_key,participant_name,first_observed_at,last_observed_at) "
            "VALUES (?,?,?,?,?)",
            ("me", "existing", "Existing inbound", "2026-01-01", "2026-01-01"),
        )
        conn.commit()
    return target


def test_import_preserves_inbound_and_excludes_other_account(tmp_path: Path) -> None:
    source, workspace, screenshot = _source(tmp_path)
    archive = tmp_path / "scoped.zip"
    assert bundle(source, workspace, archive)["screenshots"] == 1
    with ZipFile(archive) as zipped:
        scoped = tmp_path / "scoped.sqlite"
        scoped.write_bytes(zipped.read("database.sqlite"))
    with closing(sqlite3.connect(scoped)) as conn:
        for table in ("batches", "outbound_requests", "contacts", "account_contacts"):
            assert conn.execute(f"SELECT DISTINCT operator FROM {table}").fetchall() == [("me",)]
        assert conn.execute("SELECT COUNT(*) FROM operators").fetchone()[0] == 0
    target = _target(tmp_path)
    shots = tmp_path / "host_screenshots"
    before = target.read_bytes()
    assert import_bundle(archive, target, shots)["applied"] is False
    assert target.read_bytes() == before
    assert not shots.exists()

    result = import_bundle(archive, target, shots, apply=True)
    assert result == {
        "account": "me", "batches": 1, "requests": 1,
        "confirmed_sends": 1, "contacts": 1, "screenshots": 1,
        "stale_running_batches": 1, "applied": True,
    }
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT status FROM batches").fetchone()[0] == "interrupted"
        assert conn.execute("SELECT template_id FROM outbound_requests").fetchone()[0] is None
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
        assert conn.execute("SELECT DISTINCT operator FROM account_contacts").fetchall() == [("me",)]
    assert hashlib.sha256((shots / "proof.png").read_bytes()).digest() == hashlib.sha256(screenshot.read_bytes()).digest()
    with pytest.raises(ValueError, match="not empty"):
        import_bundle(archive, target, shots, apply=True)


def test_dry_run_requires_existing_target_and_rejects_tampered_bundle(tmp_path: Path) -> None:
    source, workspace, _ = _source(tmp_path)
    archive = tmp_path / "scoped.zip"
    bundle(source, workspace, archive)
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(FileNotFoundError):
        import_bundle(archive, missing, tmp_path / "screenshots")
    assert not missing.exists()

    tampered = tmp_path / "tampered.zip"
    with ZipFile(archive) as original, ZipFile(tampered, "w") as changed:
        for name in original.namelist():
            changed.writestr(name, b"corrupt" if name == "screenshots/proof.png" else original.read(name))
    with pytest.raises(ValueError, match="checksum mismatch"):
        import_bundle(tampered, _target(tmp_path), tmp_path / "screenshots")
