"""Account-scoped ZIP export for stored LinkedIn inbox observations."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from . import db


def _safe_filename(value: str) -> str:
    leaf = (value or "attachment").replace("\\", "/").rsplit("/", 1)[-1]
    leaf = re.sub(r"[^A-Za-z0-9._-]+", "_", leaf).strip("._")
    if not leaf:
        leaf = "attachment"
    return leaf[:180]


def _csv_cell(value: Any) -> Any:
    """Keep LinkedIn-sourced text inert when the CSV is opened in a spreadsheet."""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _rows(operator: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                                   list[dict[str, Any]], list[dict[str, Any]]]:
    with db._LOCK:
        conn = db._conn()
        runs = [dict(row) for row in conn.execute(
            "SELECT * FROM sync_runs WHERE operator=? ORDER BY started_at, id", (operator,)
        ).fetchall()]
        conversations = [dict(row) for row in conn.execute(
            "SELECT * FROM conversations WHERE operator=? ORDER BY id", (operator,)
        ).fetchall()]
        messages = [dict(row) for row in conn.execute(
            """SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id
                 WHERE c.operator=? ORDER BY m.conversation_id, m.id""", (operator,)
        ).fetchall()]
        attachments = [dict(row) for row in conn.execute(
            """SELECT a.* FROM attachments a
                 JOIN messages m ON m.id=a.message_id
                 JOIN conversations c ON c.id=m.conversation_id
                 WHERE c.operator=? ORDER BY m.conversation_id, m.id, a.id""",
            (operator,),
        ).fetchall()]
    return runs, conversations, messages, attachments


def _attachment_path(data_dir: Path, row: dict[str, Any]) -> Path:
    data_root = data_dir.resolve()
    root = (data_root / "inbound_files").resolve()
    if not root.is_relative_to(data_root):
        raise ValueError("Inbound file storage resolves outside the data directory")
    relative_path = row.get("relative_path")
    if not relative_path:
        raise ValueError(f"Saved attachment {row['id']} has no stored path")
    path = (data_dir / relative_path).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Saved attachment {row['id']} is outside inbound storage")
    if not path.is_file():
        raise ValueError(f"Saved attachment {row['id']} is missing")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    if not row.get("sha256") or digest.hexdigest() != row["sha256"]:
        raise ValueError(f"Saved attachment {row['id']} checksum does not match")
    return path


def build_export(operator: str, data_dir: Path) -> Path:
    """Build a complete account-scoped ZIP and return its temporary path.

    File bytes are streamed into the archive by ``ZipFile.write``. Any invalid,
    missing, or changed saved file aborts the export and removes the partial ZIP.
    """
    data_dir = Path(data_dir).resolve()
    runs, conversations, messages, attachments = _rows(operator)

    exports_dir = data_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    exports_dir = exports_dir.resolve()
    if not exports_dir.is_relative_to(data_dir):
        raise ValueError("Export directory resolves outside the data directory")
    handle = tempfile.NamedTemporaryFile(
        prefix="linkbound-inbound-", suffix=".zip", dir=exports_dir, delete=False
    )
    export_path = Path(handle.name)
    handle.close()

    try:
        conversation_ids = {row["id"] for row in conversations}
        message_counts: dict[int, int] = {conversation_id: 0 for conversation_id in conversation_ids}
        message_to_conversation: dict[int, int] = {}
        for message in messages:
            message_to_conversation[message["id"]] = message["conversation_id"]
            message_counts[message["conversation_id"]] = message_counts.get(message["conversation_id"], 0) + 1

        file_counts: dict[int, int] = {conversation_id: 0 for conversation_id in conversation_ids}
        manifest_attachments: list[dict[str, Any]] = []
        saved_files: list[tuple[dict[str, Any], Path, str]] = []
        for attachment in attachments:
            item = {key: value for key, value in attachment.items() if key != "relative_path"}
            archive_path = None
            if attachment["status"] == "saved":
                source_path = _attachment_path(data_dir, attachment)
                archive_path = f"files/{attachment['id']}-{_safe_filename(attachment['filename'])}"
                saved_files.append((attachment, source_path, archive_path))
                conversation_id = message_to_conversation.get(attachment["message_id"])
                if conversation_id is not None:
                    file_counts[conversation_id] = file_counts.get(conversation_id, 0) + 1
            item["archive_path"] = archive_path
            manifest_attachments.append(item)

        contacts_buffer = io.StringIO(newline="")
        writer = csv.DictWriter(
            contacts_buffer,
            fieldnames=("id", "contact_url", "participant_name", "match_state",
                        "linkedin_unread", "reviewed_at", "last_observed_at",
                        "message_count", "file_count"),
        )
        writer.writeheader()
        for conversation in conversations:
            writer.writerow({key: _csv_cell(value) for key, value in {
                "id": conversation["id"],
                "contact_url": conversation["contact_url"] or "",
                "participant_name": conversation["participant_name"],
                "match_state": conversation["match_state"],
                "linkedin_unread": conversation["linkedin_unread"],
                "reviewed_at": conversation["reviewed_at"] or "",
                "last_observed_at": conversation["last_observed_at"],
                "message_count": message_counts.get(conversation["id"], 0),
                "file_count": file_counts.get(conversation["id"], 0),
            }.items()})

        exported_at = datetime.now(timezone.utc).isoformat()
        manifest = {
            "operator": operator,
            "exported_at": exported_at,
            "sync_runs": runs,
            "conversations": conversations,
            "messages": messages,
            "attachments": manifest_attachments,
        }
        with ZipFile(export_path, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr("contacts.csv", contacts_buffer.getvalue())
            for _attachment, source_path, member_path in saved_files:
                archive.write(source_path, member_path)
        # Verify the archive bytes, not only the source file checked before write.
        with ZipFile(export_path, "r") as archive:
            for attachment, _source_path, member_path in saved_files:
                digest = hashlib.sha256()
                with archive.open(member_path) as member:
                    for block in iter(lambda: member.read(1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest() != attachment["sha256"]:
                    raise ValueError(f"Exported attachment {attachment['id']} checksum does not match")
        return export_path
    except Exception:
        export_path.unlink(missing_ok=True)
        raise
