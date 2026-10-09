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


def record_sync_egress(run_id: int, node_id: str, node_name: str, ipv4: str) -> None:
    with db._LOCK:
        db._conn().execute(
            "UPDATE sync_runs SET exit_node_id=?, exit_node_name=?, egress_ipv4=? WHERE id=?",
            (node_id, node_name, ipv4, run_id),
        )
        db._conn().commit()


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


def record_scan_observation(
    operator: str, run_id: int, thread_key: str, unread_before_open: bool | None,
) -> None:
    """Attach the pre-open unread baseline to a stable thread key."""
    thread_key = _key(thread_key, "thread_key")
    if unread_before_open is not None and type(unread_before_open) is not bool:
        raise ValueError("Unread state must be true, false, or unknown")
    status = "pending" if unread_before_open else (
        "not_needed" if unread_before_open is False else "unknown"
    )
    with db._LOCK:
        conn = db._conn()
        row = conn.execute(
            """SELECT c.id AS conversation_id FROM conversations c
               JOIN sync_runs r ON r.operator=c.operator
               WHERE r.id=? AND r.operator=? AND r.status='running' AND c.thread_key=?""",
            (run_id, operator, thread_key),
        ).fetchone()
        if row is None:
            raise ValueError("Active run and account-scoped conversation required")
        before = None if unread_before_open is None else int(unread_before_open)
        existing = conn.execute(
            "SELECT linkedin_unread_before_open FROM conversation_scan_observations "
            "WHERE run_id=? AND conversation_id=?", (run_id, row["conversation_id"]),
        ).fetchone()
        if existing is not None:
            if existing["linkedin_unread_before_open"] != before:
                raise ValueError("Unread baseline conflicts with this scan")
            return
        conn.execute(
            """INSERT INTO conversation_scan_observations
               (run_id, conversation_id, linkedin_unread_before_open, restore_status)
               VALUES (?, ?, ?, ?)""",
            (run_id, row["conversation_id"], before, status),
        )
        conn.commit()


def record_unread_restore(
    operator: str, run_id: int, thread_key: str, status: str,
    linkedin_unread_after: bool | None, *, error: str = "",
) -> None:
    """Record the marker actually seen after restoration, including failure."""
    thread_key = _key(thread_key, "thread_key")
    if status not in {"not_needed", "restored", "failed", "unknown"}:
        raise ValueError("Invalid unread restoration status")
    if linkedin_unread_after is not None and type(linkedin_unread_after) is not bool:
        raise ValueError("Unread state must be true, false, or unknown")
    with db._LOCK:
        conn = db._conn()
        row = conn.execute(
            """SELECT c.id AS conversation_id, o.linkedin_unread_before_open
               FROM conversation_scan_observations o
               JOIN conversations c ON c.id=o.conversation_id
               JOIN sync_runs r ON r.id=o.run_id
               WHERE r.id=? AND r.operator=?
                 AND c.operator=? AND c.thread_key=?""",
            (run_id, operator, operator, thread_key),
        ).fetchone()
        if row is None:
            raise ValueError("Account-scoped run and unread baseline required")
        if status == "restored" and (row["linkedin_unread_before_open"] != 1 or linkedin_unread_after is not True):
            raise ValueError("Restored requires a verified unread marker")
        if status == "not_needed" and row["linkedin_unread_before_open"] != 0:
            raise ValueError("Not needed requires an initially read thread")
        after = None if linkedin_unread_after is None else int(linkedin_unread_after)
        conn.execute("BEGIN")
        try:
            conn.execute(
                """UPDATE conversation_scan_observations
                   SET restore_status=?, restored_at=?, error=?
                   WHERE run_id=? AND conversation_id=?""",
                (status, db._now() if status == "restored" else None,
                 error[:1000], run_id, row["conversation_id"]),
            )
            conn.execute(
                "UPDATE conversations SET linkedin_unread=? WHERE id=?",
                (after, row["conversation_id"]),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def pending_unread_restores(operator: str, run_id: int) -> list[dict[str, Any]]:
    """Return opened unread threads whose marker has not been verified."""
    with db._LOCK:
        rows = db._conn().execute(
            """SELECT c.thread_key,c.section,c.participant_name,c.preview_text
               FROM conversation_scan_observations o
               JOIN conversations c ON c.id=o.conversation_id
               JOIN sync_runs r ON r.id=o.run_id
               WHERE o.run_id=? AND r.operator=? AND c.operator=?
                 AND o.linkedin_unread_before_open=1
                 AND o.restore_status IN ('pending','failed','unknown')
               ORDER BY c.id""",
            (run_id, operator, operator),
        ).fetchall()
    return [dict(row) for row in rows]


def has_scan_observation(operator: str, run_id: int, thread_key: str) -> bool:
    with db._LOCK:
        return db._conn().execute(
            """SELECT 1 FROM conversation_scan_observations o
               JOIN conversations c ON c.id=o.conversation_id
               JOIN sync_runs r ON r.id=o.run_id
               WHERE o.run_id=? AND r.operator=? AND c.operator=? AND c.thread_key=?""",
            (run_id, operator, operator, thread_key),
        ).fetchone() is not None


def record_open_intent(
    operator: str, run_id: int, section: str, participant_name: str, preview_text: str,
) -> int:
    """Commit an unread row's identity before the browser can mark it read."""
    participant_name = participant_name.strip()[:300]
    preview_text = preview_text.strip()[:2000]
    if not participant_name and not preview_text:
        raise ValueError("Unread row has no recoverable visible identity")
    with db._LOCK:
        conn = db._conn()
        run = conn.execute(
            "SELECT operator, status, expected_sections_json FROM sync_runs WHERE id=?", (run_id,)
        ).fetchone()
        if not run or run["operator"] != operator or run["status"] != "running" or section not in json.loads(
            run["expected_sections_json"]
        ):
            raise ValueError("Active account-scoped sync section required")
        cur = conn.execute(
            """INSERT INTO inbox_open_intents
               (run_id, operator, section, participant_name, preview_text, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, operator, section, participant_name, preview_text, db._now()),
        )
        conn.commit()
        return int(cur.lastrowid)


def bind_open_intent(operator: str, intent_id: int, thread_key: str) -> None:
    thread_key = _key(thread_key, "thread_key")
    with db._LOCK:
        conn = db._conn()
        row = conn.execute(
            "SELECT thread_key FROM inbox_open_intents WHERE id=? AND operator=?",
            (intent_id, operator),
        ).fetchone()
        if row is None or row["thread_key"] not in {None, thread_key}:
            raise ValueError("Unread open intent does not match this account and thread")
        conn.execute(
            "UPDATE inbox_open_intents SET thread_key=? WHERE id=? AND operator=?",
            (thread_key, intent_id, operator),
        )
        conn.commit()


def record_open_intent_restore(
    operator: str, intent_id: int, status: str, *, error: str = "",
) -> None:
    if status not in {"restored", "failed", "unknown"}:
        raise ValueError("Invalid unread restoration status")
    with db._LOCK:
        conn = db._conn()
        cur = conn.execute(
            """UPDATE inbox_open_intents SET restore_status=?, restored_at=?, error=?
               WHERE id=? AND operator=?""",
            (status, db._now() if status == "restored" else None,
             error[:1000], intent_id, operator),
        )
        if cur.rowcount != 1:
            raise ValueError("Unread open intent does not belong to this account")
        conn.commit()


def pending_open_intents(operator: str, run_id: int | None = None) -> list[dict[str, Any]]:
    """Include interrupted and previous partial runs before any new opening."""
    with db._LOCK:
        rows = db._conn().execute(
            """SELECT * FROM inbox_open_intents
               WHERE operator=? AND restore_status IN ('pending','failed','unknown')
                 AND (? IS NULL OR run_id=?) ORDER BY id""",
            (operator, run_id, run_id),
        ).fetchall()
    return [dict(row) for row in rows]


def set_section_coverage(
    run_id: int, section: str, *, observed: int, stored: int,
    unresolved: int, complete: bool, error: str = "",
) -> None:
    """Finalize one folder after incremental thread observations."""
    if min(observed, stored, unresolved) < 0 or stored + unresolved > observed:
        raise ValueError("Invalid section counts")
    with db._LOCK:
        conn = db._conn()
        run = conn.execute("SELECT * FROM sync_runs WHERE id=? AND status='running'", (run_id,)).fetchone()
        if run is None or section not in json.loads(run["expected_sections_json"]):
            raise ValueError("Active run and expected section required")
        coverage = json.loads(run["coverage_json"] or "{}")
        coverage[section] = {
            "status": "complete" if complete and not error and not unresolved else "incomplete",
            "observed": observed, "stored": stored, "unresolved": unresolved,
            "observed_at": db._now(), "error": error[:1000],
        }
        conn.execute("UPDATE sync_runs SET coverage_json=? WHERE id=?", (json.dumps(coverage), run_id))
        conn.commit()


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
            """SELECT c.*,
                      (SELECT o.linkedin_unread_before_open FROM conversation_scan_observations o
                       WHERE o.conversation_id=c.id ORDER BY o.run_id DESC LIMIT 1) AS last_scan_unread_before_open,
                      (SELECT o.restore_status FROM conversation_scan_observations o
                       WHERE o.conversation_id=c.id ORDER BY o.run_id DESC LIMIT 1) AS last_scan_restore_status,
                      (SELECT o.error FROM conversation_scan_observations o
                       WHERE o.conversation_id=c.id ORDER BY o.run_id DESC LIMIT 1) AS last_scan_restore_error,
                      (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count,
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
            """SELECT c.*,
                      (SELECT o.linkedin_unread_before_open FROM conversation_scan_observations o
                       WHERE o.conversation_id=c.id ORDER BY o.run_id DESC LIMIT 1) AS last_scan_unread_before_open,
                      (SELECT o.restore_status FROM conversation_scan_observations o
                       WHERE o.conversation_id=c.id ORDER BY o.run_id DESC LIMIT 1) AS last_scan_restore_status,
                      (SELECT o.error FROM conversation_scan_observations o
                       WHERE o.conversation_id=c.id ORDER BY o.run_id DESC LIMIT 1) AS last_scan_restore_error
               FROM conversations c WHERE c.operator=? AND c.id=?""",
            (operator, conversation_id)
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
        if (row["body"] != body or
                (row["direction"] != direction and
                 row["direction"] != "unknown" and direction != "unknown") or
                (row["source_at"] and source_at and row["source_at"] != source_at)):
            conn.rollback()
            raise ValueError("Message source key conflicts with different content")
        resolved_direction = row["direction"] if direction == "unknown" else direction
        resolved_source_at = row["source_at"] or source_at
        if resolved_direction != row["direction"] or resolved_source_at != row["source_at"]:
            conn.execute(
                "UPDATE messages SET direction=?, source_at=? WHERE id=?",
                (resolved_direction, resolved_source_at, row["id"]),
            )
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
