"""One-time, account-scoped import of Shubham's legacy LinkBound history.

Bundle on the old workstation, then inspect and apply on a stopped Linode app.
The source database is copied through SQLite's backup API and never modified.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile

from app import db

ACCOUNT = "me"
FORMAT = 1
LIVE_DB = Path("/var/lib/linkbound/data/outbound.db")
MAX_BUNDLE_BYTES = 512 * 1024 * 1024


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _member_path(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw)
    if (not raw or path.is_absolute() or "\\" in raw or
            any(part in {".", ".."} for part in path.parts) or
            not path.parts or ":" in path.parts[0]):
        raise ValueError(f"Unsafe bundle path: {raw!r}")
    return path


def _screenshot_member(raw: str) -> str:
    value = (raw or "").replace("\\", "/")
    if value.startswith("data/screenshots/"):
        value = value[len("data/screenshots/"):]
    elif value.startswith("screenshots/"):
        value = value[len("screenshots/"):]
    else:
        raise ValueError("Legacy screenshot path is outside data/screenshots")
    path = _member_path(value)
    return "screenshots/" + path.as_posix()


def _open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def bundle(source_db: Path, workspace: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError("Bundle destination already exists")
    shots_root = (workspace / "data" / "screenshots").resolve()
    with tempfile.TemporaryDirectory(prefix="linkbound-legacy-bundle-") as temp:
        source_copy = Path(temp) / "source.sqlite"
        copy = Path(temp) / "database.sqlite"
        with closing(_open_readonly(source_db)) as original, closing(sqlite3.connect(source_copy)) as target:
            original.backup(target)
        db.init_db(source_copy)
        db.close_db()
        db.init_db(copy)
        db.close_db()
        with closing(_open_readonly(source_copy)) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Legacy database failed integrity_check")
            rows = conn.execute(
                "SELECT screenshot_path FROM outbound_requests WHERE operator=? "
                "AND screenshot_path IS NOT NULL AND screenshot_path<>''", (ACCOUNT,)
            ).fetchall()
            request_count = conn.execute(
                "SELECT COUNT(*) FROM outbound_requests WHERE operator=?", (ACCOUNT,)
            ).fetchone()[0]
            with closing(sqlite3.connect(copy)) as scoped:
                scoped.execute("PRAGMA foreign_keys=ON")
                for table in ("batches", "outbound_requests", "account_contacts", "contacts"):
                    items = conn.execute(
                        f"SELECT * FROM {table} WHERE operator=?", (ACCOUNT,)
                    ).fetchall()
                    _copy_rows(conn, scoped, table, items)
                if scoped.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Scoped source history violates a foreign key")
                scoped.commit()
        files = {"database.sqlite": copy}
        for row in rows:
            member = _screenshot_member(row["screenshot_path"])
            file = shots_root.joinpath(*PurePosixPath(member).parts[1:])
            if (not file.resolve().is_relative_to(shots_root) or
                    not stat.S_ISREG(file.lstat().st_mode)):
                raise ValueError(f"Legacy screenshot is missing or unsafe: {member}")
            files[member] = file
        if sum(file.stat().st_size for file in files.values()) > MAX_BUNDLE_BYTES:
            raise ValueError("Legacy bundle exceeds the 512 MiB transfer limit")
        manifest = {
            "format": FORMAT, "account": ACCOUNT,
            "files": {name: _hash(file) for name, file in sorted(files.items())},
        }
        try:
            with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
                for name, file in sorted(files.items()):
                    archive.write(file, name)
                archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
            output.chmod(0o600)
        except Exception:
            output.unlink(missing_ok=True)
            raise
        return {"account": ACCOUNT, "requests": request_count,
                "screenshots": len(files) - 1, "bundle_bytes": output.stat().st_size}


def _extract_verified(archive_path: Path, destination: Path) -> tuple[Path, dict]:
    with ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or "manifest.json" not in names:
            raise ValueError("Bundle has duplicate names or no manifest")
        manifest = json.loads(archive.read("manifest.json"))
        if (manifest.get("format") != FORMAT or manifest.get("account") != ACCOUNT or
                not isinstance(manifest.get("files"), dict) or
                set(names) != set(manifest["files"]) | {"manifest.json"} or
                "database.sqlite" not in manifest["files"]):
            raise ValueError("Bundle manifest or account is invalid")
        if sum(info.file_size for info in archive.infolist()) > MAX_BUNDLE_BYTES:
            raise ValueError("Bundle expands beyond 512 MiB")
        for name, expected in manifest["files"].items():
            rel = _member_path(name)
            if (name != "database.sqlite" and
                    (len(rel.parts) < 2 or rel.parts[0] != "screenshots")):
                raise ValueError("Bundle contains an unexpected file")
            if not isinstance(expected, str) or len(expected) != 64:
                raise ValueError("Bundle checksum is invalid")
            target = destination.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            if _hash(target) != expected:
                raise ValueError(f"Bundle checksum mismatch: {name}")
    return destination / "database.sqlite", manifest


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _copy_rows(source: sqlite3.Connection, target: sqlite3.Connection, table: str,
               rows: list[sqlite3.Row], *, running_to_interrupted: bool = False,
               clear_template_id: bool = False) -> None:
    columns = _columns(target, table)
    if set(columns) != set(_columns(source, table)):
        raise ValueError(f"Source and destination {table} schemas differ")
    sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})"
    for row in rows:
        values = dict(row)
        if running_to_interrupted and values["status"] == "running":
            values["status"] = "interrupted"
        if clear_template_id:
            values["template_id"] = None
        target.execute(sql, [values[column] for column in columns])


def _profile_rows(source: sqlite3.Connection, contacts: list[sqlite3.Row],
                  requests: list[sqlite3.Row]) -> list[tuple]:
    latest_request: dict[str, sqlite3.Row] = {}
    for row in requests:
        key = row["normalized_linkedin_url"] or db.normalize_url(row["linkedin_url"] or "")
        if key:
            latest_request[key] = row
    legacy_profiles = {}
    for row in source.execute("SELECT * FROM contacts WHERE operator=?", (ACCOUNT,)):
        key = row["normalized_linkedin_url"] or db.normalize_url(row["linkedin_url"] or "")
        if key:
            legacy_profiles[key] = row
    result = []
    for item in contacts:
        url = item["linkedin_url"]
        request = latest_request.get(url)
        profile = legacy_profiles.get(url)
        def value(field: str):
            return ((profile[field] if profile and profile[field] else None) or
                    (request[field] if request and field in request.keys() else None))
        last_action = (request["completed_at"] or request["created_at"]) if request else None
        result.append((url, url, value("full_name"), value("first_name"),
                       value("company_csv"), value("headline"), ACCOUNT,
                       item["first_seen_at"], last_action))
    return result


def import_bundle(bundle_path: Path, target_db: Path, screenshots_dir: Path,
                  *, apply: bool = False) -> dict:
    if not target_db.is_file():
        raise FileNotFoundError("Hosted database does not exist")
    if apply and target_db.resolve() == LIVE_DB and subprocess.run(
        ["systemctl", "is-active", "--quiet", "linkbound-app"], check=False
    ).returncode == 0:
        raise ValueError("Stop linkbound-app before importing historical data")
    with tempfile.TemporaryDirectory(prefix="linkbound-legacy-import-") as temp:
        scratch = Path(temp)
        source_path, manifest = _extract_verified(bundle_path, scratch)
        db.init_db(source_path)
        db.close_db()
        target_connection = (sqlite3.connect(target_db) if apply else _open_readonly(target_db))
        with closing(_open_readonly(source_path)) as source, closing(target_connection) as target:
            target.row_factory = sqlite3.Row
            target.execute("PRAGMA foreign_keys=ON")
            if target.execute("PRAGMA user_version").fetchone()[0] != 9:
                raise ValueError("Hosted database must use schema 9")
            owner = target.execute(
                "SELECT linkedin_self_url FROM operators WHERE key=?", (ACCOUNT,)
            ).fetchone()
            if not owner or not owner["linkedin_self_url"]:
                raise ValueError("Shubham's hosted LinkedIn profile must be bound first")
            for table in ("batches", "outbound_requests", "contacts", "account_contacts"):
                if target.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                    raise ValueError(f"Hosted {table} is not empty; stop for manual reconciliation")
            batches = source.execute(
                "SELECT * FROM batches WHERE operator=? ORDER BY id", (ACCOUNT,)
            ).fetchall()
            requests = source.execute(
                "SELECT * FROM outbound_requests WHERE operator=? ORDER BY id", (ACCOUNT,)
            ).fetchall()
            contacts = source.execute(
                "SELECT * FROM account_contacts WHERE operator=? ORDER BY linkedin_url", (ACCOUNT,)
            ).fetchall()
            batch_ids = {row["id"] for row in batches}
            if any(row["batch_id"] is not None and row["batch_id"] not in batch_ids
                   for row in requests):
                raise ValueError("Shubham request refers to a batch owned by another account")
            expected_shots = {
                _screenshot_member(row["screenshot_path"])
                for row in requests if row["screenshot_path"]
            }
            if expected_shots != set(manifest["files"]) - {"database.sqlite"}:
                raise ValueError("Screenshot bundle differs from Shubham request history")
            profiles = _profile_rows(source, contacts, requests)
            summary = {
                "account": ACCOUNT, "batches": len(batches),
                "requests": len(requests),
                "confirmed_sends": sum(row["status"] == "sent" for row in requests),
                "contacts": len(contacts), "screenshots": len(expected_shots),
                "stale_running_batches": sum(row["status"] == "running" for row in batches),
                "applied": False,
            }
            if not apply:
                return summary
            shots_root = screenshots_dir.resolve()
            shots_root.mkdir(parents=True, exist_ok=True)
            created: list[Path] = []
            try:
                target.execute("BEGIN IMMEDIATE")
                _copy_rows(source, target, "batches", batches, running_to_interrupted=True)
                _copy_rows(source, target, "outbound_requests", requests, clear_template_id=True)
                _copy_rows(source, target, "account_contacts", contacts)
                target.executemany(
                    """INSERT INTO contacts
                       (linkedin_url, normalized_linkedin_url, full_name,
                        first_name, company_csv, headline, operator,
                        first_seen_at, last_action_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", profiles,
                )
                for member in sorted(expected_shots):
                    relative = PurePosixPath(member).parts[1:]
                    destination = shots_root.joinpath(*relative)
                    if not destination.resolve().is_relative_to(shots_root) or destination.exists():
                        raise ValueError("Screenshot destination is unsafe or already exists")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with scratch.joinpath(*PurePosixPath(member).parts).open("rb") as source_file, destination.open("xb") as output_file:
                        created.append(destination)
                        shutil.copyfileobj(source_file, output_file)
                    destination.chmod(0o600)
                if target.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Imported history violates a foreign key")
                target.commit()
            except Exception:
                target.rollback()
                for path in created:
                    path.unlink(missing_ok=True)
                raise
            summary["applied"] = True
            return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("bundle")
    create.add_argument("--source-db", required=True, type=Path)
    create.add_argument("--workspace", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)
    imp = commands.add_parser("import")
    imp.add_argument("--bundle", required=True, type=Path)
    imp.add_argument("--target-db", required=True, type=Path)
    imp.add_argument("--screenshots-dir", required=True, type=Path)
    imp.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.command == "bundle":
        result = bundle(args.source_db, args.workspace, args.output)
    else:
        result = import_bundle(args.bundle, args.target_db, args.screenshots_dir,
                               apply=args.apply)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
