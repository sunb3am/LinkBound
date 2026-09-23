"""Account-scoped observations from the headed LinkedIn inbox browser.

Only a stable thread or message key may enter the canonical tables. A selector
failure belongs in sync coverage, not in a guessed identity or a fake empty inbox.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from . import db

_KEY_LIMIT = 512
_MAX_FILE_BYTES = 25 * 1024 * 1024


def _key(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > _KEY_LIMIT:
        raise ValueError(f"{label} must be a stable, nonempty key of at most {_KEY_LIMIT} characters")
    return value


def _operator_exists(conn, operator: str) -> bool:
    return conn.execute("SELECT 1 FROM operators WHERE key=?", (operator,)).fetchone() is not None


def start_sync_run(operator: str, *, expected_sections: tuple[str, ...], mode: str = "inventory") -> int:
    if mode not in {"inventory", "full"}:
        raise ValueError("Unknown sync mode")
    if not expected_sections or len(set(expected_sections)) != len(expected_sections) or any(
        not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", section) for section in expected_sections
    ):
        raise ValueError("A sync run needs distinct expected inbox sections")
    with db._LOCK:
        conn = db._conn()
        if not _operator_exists(conn, operator):
            raise ValueError("Unknown operator")
        cur = conn.execute(
            """INSERT INTO sync_runs(operator, mode, status, started_at, expected_sections_json)
               VALUES (?, ?, 'running', ?, ?)""",
            (operator, mode, db._now(), json.dumps(expected_sections)),
        )
        conn.commit()
        return int(cur.lastrowid)


def mark_interrupted_runs() -> int:
    """A process restart must never leave a past scan looking current."""
    with db._LOCK:
        conn = db._conn()
        cur = conn.execute(
            "UPDATE sync_runs SET status='interrupted', finished_at=?, error='App restarted during sync' "
            "WHERE status='running'",
            (db._now(),),
        )
        conn.commit()
        return cur.rowcount


def record_inventory_section(
    run_id: int, section: str, items: list[dict[str, Any]], *,
    complete: bool, error: str = "", observed_at: str | None = None,
) -> dict[str, int]:
    """Upsert browser-visible rows and retain the section's coverage result.

    A row without a stable thread key is counted unresolved and never merged
    by a person's name or preview text. This is deliberately conservative.
    """
    section = (section or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", section):
        raise ValueError("Invalid inbox section")
    seen_at = observed_at or db._now()
    counts = {"observed": len(items), "stored": 0, "unresolved": 0}
    with db._LOCK:
        conn = db._conn()
        run = conn.execute("SELECT * FROM sync_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["status"] != "running":
            raise ValueError("Sync run is not active")
        try:
            conn.execute("BEGIN")
            for item in items:
                thread_key = (item.get("thread_key") or "").strip()
                if not thread_key or len(thread_key) > _KEY_LIMIT:
                    counts["unresolved"] += 1
                    continue
                profile = db.normalize_url(item.get("contact_url") or "") or None
                if profile and not profile.startswith("https://linkedin.com/in/"):
                    profile = None
                match = "unmatched"
                if profile and conn.execute(
                    "SELECT 1 FROM account_contacts WHERE operator=? AND linkedin_url=?",
                    (run["operator"], profile),
                ).fetchone():
                    match = "matched"
                unread = item.get("linkedin_unread")
                if unread is not None and type(unread) is not bool:
                    raise ValueError("linkedin_unread must be true, false, or unknown")
                unread_value = None if unread is None else int(unread)
                conn.execute(
                    """INSERT INTO conversations
                       (operator, thread_key, contact_url, participant_name, section,
                        preview_text, linkedin_unread, match_state,
                        first_observed_at, last_observed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(operator, thread_key) DO UPDATE SET
                         contact_url=COALESCE(conversations.contact_url, excluded.contact_url),
                         participant_name=COALESCE(NULLIF(excluded.participant_name,''), conversations.participant_name),
                         section=excluded.section,
                         preview_text=excluded.preview_text,
                         linkedin_unread=excluded.linkedin_unread,
                         match_state=CASE
                           WHEN conversations.contact_url IS NOT NULL AND excluded.contact_url IS NOT NULL
                                AND conversations.contact_url<>excluded.contact_url THEN 'ambiguous'
                           WHEN conversations.match_state='matched' OR excluded.match_state='matched'
                                THEN 'matched'
                           ELSE conversations.match_state END,
                         reviewed_at=CASE WHEN conversations.preview_text<>excluded.preview_text
                           THEN NULL ELSE conversations.reviewed_at END,
                         last_observed_at=excluded.last_observed_at""",
                    (run["operator"], thread_key, profile,
                     (item.get("participant_name") or "").strip()[:300], section,
                     (item.get("preview_text") or "").strip()[:2000], unread_value,
                     match, seen_at, seen_at),
                )
                counts["stored"] += 1
            coverage = json.loads(run["coverage_json"] or "{}")
            coverage[section] = {
                "status": "complete" if complete and not error and not counts["unresolved"] else "incomplete",
                "observed": counts["observed"],
                "stored": counts["stored"],
                "unresolved": counts["unresolved"],
                "observed_at": seen_at,
                "error": error[:1000],
            }
            conn.execute("UPDATE sync_runs SET coverage_json=? WHERE id=?", (json.dumps(coverage), run_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return counts


def finish_sync_run(run_id: int, *, error: str = "") -> dict[str, Any]:
    with db._LOCK:
        conn = db._conn()
        run = conn.execute("SELECT * FROM sync_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["status"] != "running":
            raise ValueError("Sync run is not active")
        coverage = json.loads(run["coverage_json"] or "{}")
        expected_sections = json.loads(run["expected_sections_json"])
        status = "complete" if coverage and not error and all(
            value["status"] == "complete" for value in coverage.values()
        ) and all(section in coverage for section in expected_sections) else "partial"
        conn.execute(
            "UPDATE sync_runs SET status=?, finished_at=?, error=? WHERE id=?",
            (status, db._now(), error[:1000], run_id),
        )
        conn.commit()
        return {**dict(run), "status": status, "coverage": coverage, "error": error[:1000]}


def list_sync_runs(operator: str, limit: int = 20) -> list[dict[str, Any]]:
    with db._LOCK:
        rows = db._conn().execute(
            "SELECT * FROM sync_runs WHERE operator=? ORDER BY id DESC LIMIT ?",
            (operator, min(max(limit, 1), 100)),
        ).fetchall()
    results = []
    for row in rows:
        expected = json.loads(row["expected_sections_json"] or "[]")
        coverage = json.loads(row["coverage_json"] or "{}")
        for section in expected:
            coverage.setdefault(section, {"status": "not_scanned", "observed": 0,
                                          "stored": 0, "unresolved": 0})
        results.append({**dict(row), "expected_sections": expected, "coverage": coverage})
    return results


def list_conversations(operator: str, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    with db._LOCK:
        rows = db._conn().execute(
            """SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count,
                      (SELECT COUNT(*) FROM attachments a JOIN messages m ON m.id=a.message_id
                       WHERE m.conversation_id=c.id AND a.status='saved') AS file_count
               FROM conversations c WHERE c.operator=?
               ORDER BY c.last_observed_at DESC, c.id DESC LIMIT ? OFFSET ?""",
            (operator, min(max(limit, 1), 500), max(offset, 0)),
        ).fetchall()
    return [dict(row) for row in rows]


def count_conversations(operator: str) -> int:
    with db._LOCK:
        row = db._conn().execute(
            "SELECT COUNT(*) AS count FROM conversations WHERE operator=?", (operator,)
        ).fetchone()
        return int(row["count"])


def get_conversation(operator: str, conversation_id: int) -> dict[str, Any] | None:
    with db._LOCK:
        conn = db._conn()
        row = conn.execute(
            "SELECT * FROM conversations WHERE operator=? AND id=?", (operator, conversation_id)
        ).fetchone()
        if not row:
            return None
        messages = [dict(r) for r in conn.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,)
        ).fetchall()]
        for message in messages:
            message["attachments"] = [{k: v for k, v in dict(r).items() if k != "relative_path"}
                                      for r in conn.execute(
                "SELECT * FROM attachments WHERE message_id=? ORDER BY id", (message["id"],)
            ).fetchall()]
    return {**dict(row), "messages": messages}


def mark_reviewed(operator: str, conversation_id: int) -> bool:
    with db._LOCK:
        conn = db._conn()
        cur = conn.execute(
            "UPDATE conversations SET reviewed_at=? WHERE operator=? AND id=?",
            (db._now(), operator, conversation_id),
        )
        conn.commit()
        return cur.rowcount > 0


def link_contact(operator: str, conversation_id: int, contact_url: str) -> bool:
    url = db.normalize_url(contact_url)
    if not url.startswith("https://linkedin.com/in/"):
        raise ValueError("A LinkedIn profile URL is required")
    with db._LOCK:
        conn = db._conn()
        conversation = conn.execute(
            "SELECT * FROM conversations WHERE operator=? AND id=?", (operator, conversation_id)
        ).fetchone()
        if not conversation:
            return False
        try:
            conn.execute("BEGIN")
            now = db._now()
            conn.execute(
                """INSERT INTO account_contacts(operator, linkedin_url, first_seen_at)
                   VALUES (?, ?, ?) ON CONFLICT(operator, linkedin_url) DO NOTHING""",
                (operator, url, now),
            )
            if not conn.execute(
                "SELECT 1 FROM contacts WHERE normalized_linkedin_url=?", (url,)
            ).fetchone():
                db._upsert_profile_details(
                    conn, linkedin_url=url, full_name=conversation["participant_name"],
                    first_name="", company_csv="", headline="", now=now,
                )
            conn.execute(
                "UPDATE conversations SET contact_url=?, match_state='matched' WHERE operator=? AND id=?",
                (url, operator, conversation_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return True


def record_connection_observation(
    operator: str, contact_url: str, *, source: str, observed_at: str | None = None,
) -> int:
    """Record positive first-degree evidence, never inferred from an absent invite."""
    if source not in {"profile_first_degree", "invitation_accepted"}:
        raise ValueError("Connection evidence must come from a supported visible source")
    url = db.normalize_url(contact_url)
    if not url.startswith("https://linkedin.com/in/"):
        raise ValueError("A LinkedIn profile URL is required")
    now = observed_at or db._now()
    with db._LOCK:
        conn = db._conn()
        if not _operator_exists(conn, operator):
            raise ValueError("Unknown operator")
        conn.execute(
            """INSERT INTO relationship_observations
               (operator, contact_url, fact, source, first_observed_at, last_observed_at)
               VALUES (?, ?, 'connected', ?, ?, ?)
               ON CONFLICT(operator, contact_url, fact, source) DO UPDATE SET
                 last_observed_at=excluded.last_observed_at""",
            (operator, url, source, now, now),
        )
        row = conn.execute(
            """SELECT id FROM relationship_observations
               WHERE operator=? AND contact_url=? AND fact='connected' AND source=?""",
            (operator, url, source),
        ).fetchone()
        conn.commit()
        return int(row["id"])


def record_message(
    operator: str, conversation_id: int, *, source_key: str, direction: str,
    body: str, source_at: str | None = None, observed_at: str | None = None,
) -> int:
    source_key = _key(source_key, "Message source key")
    if direction not in {"inbound", "outbound", "unknown"}:
        raise ValueError("Invalid message direction")
    now = observed_at or db._now()
    with db._LOCK:
        conn = db._conn()
        if not conn.execute("SELECT 1 FROM conversations WHERE id=? AND operator=?",
                            (conversation_id, operator)).fetchone():
            raise ValueError("Conversation is not on this account")
        conn.execute(
            """INSERT INTO messages(conversation_id, source_key, direction, body, source_at,
                                    first_observed_at, last_observed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(conversation_id, source_key) DO UPDATE SET
                 last_observed_at=excluded.last_observed_at""",
            (conversation_id, source_key, direction, body, source_at, now, now),
        )
        row = conn.execute(
            "SELECT id, direction, body, source_at FROM messages WHERE conversation_id=? AND source_key=?",
            (conversation_id, source_key),
        ).fetchone()
        if row["direction"] != direction or row["body"] != body or row["source_at"] != source_at:
            conn.rollback()
            raise ValueError("Message source key conflicts with different content")
        conn.commit()
        return int(row["id"])


def save_attachment(
    operator: str, message_id: int, *, source_key: str, filename: str,
    mime_type: str, data: bytes, data_dir: Path, observed_at: str | None = None,
) -> int:
    source_key = _key(source_key, "Attachment source key")
    if len(data) > _MAX_FILE_BYTES:
        raise ValueError("Attachment exceeds 25 MiB")
    with db._LOCK:
        conn = db._conn()
        if not conn.execute(
            """SELECT 1 FROM messages m JOIN conversations c ON c.id=m.conversation_id
                 WHERE m.id=? AND c.operator=?""", (message_id, operator)
        ).fetchone():
            raise ValueError("Message is not on this account")
    digest = hashlib.sha256(data).hexdigest()
    with db._LOCK:
        existing = db._conn().execute(
            "SELECT sha256 FROM attachments WHERE message_id=? AND source_key=?",
            (message_id, source_key),
        ).fetchone()
        if existing and existing["sha256"] != digest:
            raise ValueError("Attachment source key conflicts with different bytes")
    safe_operator = re.fullmatch(r"[a-z0-9_]{1,64}", operator)
    if not safe_operator:
        raise ValueError("Invalid account key")
    relative = Path("inbound_files") / operator / digest[:2] / digest
    destination = (data_dir / relative).resolve()
    if not destination.is_relative_to(data_dir.resolve()):
        raise ValueError("Invalid attachment location")
    destination.parent.mkdir(parents=True, exist_ok=True)
    created = False
    if not destination.exists():
        temporary = destination.with_name(f"{digest}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, destination)
            created = True
        finally:
            temporary.unlink(missing_ok=True)
    elif hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        raise ValueError("Stored attachment checksum mismatch")
    now = observed_at or db._now()
    try:
        with db._LOCK:
            conn = db._conn()
            existing = conn.execute(
                "SELECT sha256 FROM attachments WHERE message_id=? AND source_key=?",
                (message_id, source_key),
            ).fetchone()
            if existing and existing["sha256"] != digest:
                raise ValueError("Attachment source key conflicts with different bytes")
            try:
                conn.execute(
                    """INSERT INTO attachments(message_id, source_key, filename, mime_type, size_bytes,
                                               sha256, relative_path, status, observed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'saved', ?)
                       ON CONFLICT(message_id, source_key) DO UPDATE SET observed_at=excluded.observed_at""",
                    (message_id, source_key, filename.replace("\\", "/").split("/")[-1][:255],
                     mime_type[:120], len(data), digest, relative.as_posix(), now),
                )
                row = conn.execute(
                    "SELECT id FROM attachments WHERE message_id=? AND source_key=?", (message_id, source_key)
                ).fetchone()
                conn.commit()
                return int(row["id"])
            except Exception:
                conn.rollback()
                raise
    except Exception:
        if created:
            with db._LOCK:
                referenced = db._conn().execute(
                    "SELECT 1 FROM attachments WHERE sha256=? AND status='saved' LIMIT 1",
                    (digest,),
                ).fetchone()
            if not referenced:
                destination.unlink(missing_ok=True)
        raise


def attachment_file(operator: str, attachment_id: int, data_dir: Path) -> tuple[Path, dict[str, Any]] | None:
    with db._LOCK:
        row = db._conn().execute(
            """SELECT a.* FROM attachments a JOIN messages m ON m.id=a.message_id
                 JOIN conversations c ON c.id=m.conversation_id
                WHERE a.id=? AND c.operator=? AND a.status='saved'""",
            (attachment_id, operator),
        ).fetchone()
    if not row:
        return None
    path = (data_dir / row["relative_path"]).resolve()
    if not path.is_relative_to((data_dir / "inbound_files").resolve()):
        raise ValueError("Invalid stored attachment path")
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
        raise ValueError("Stored attachment is missing or corrupt")
    return path, dict(row)
