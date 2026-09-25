"""SQLite persistence for outbound history, queued campaigns, and inbox observations.

Thread-safe via a module-level lock; the orchestrator runs on the event loop
while FastAPI handlers also read. Every access is guarded by ``_LOCK``.

Migrations advance PRAGMA user_version. Account-scoped facts remain distinct
from the shared contact profile and immutable outbound request history.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .linkedin_urls import canonical_profile_url
from .models import SENT_STATUSES, TERMINAL_CONTACTED

_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None
_DB_PATH: Path | None = None
LEGACY_UNVERIFIED = "legacy_unverified"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_url(url: str) -> str:
    """Normalize a LinkedIn profile URL for stable dedup keys."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parts.path or "").rstrip("/").lower()
    return f"https://{host}{path}" if host else path


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db(db_path: Path) -> None:
    """Open the database and create/upgrade tables if needed. Idempotent."""
    global _CONN, _DB_PATH
    with _LOCK:
        if _CONN is not None:
            return
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _DB_PATH = db_path
        _CONN = sqlite3.connect(str(db_path), check_same_thread=False)
        _CONN.row_factory = sqlite3.Row
        _CONN.execute("PRAGMA busy_timeout = 5000")
        _CONN.execute("PRAGMA foreign_keys = ON")
        _CONN.executescript(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                linkedin_url   TEXT PRIMARY KEY,
                normalized_linkedin_url TEXT,
                full_name      TEXT,
                first_name     TEXT,
                company_csv    TEXT,
                last_status    TEXT,
                template_used  TEXT,
                message_sent   TEXT,
                operator       TEXT,
                first_seen_at  TEXT,
                last_action_at TEXT
            );

            CREATE TABLE IF NOT EXISTS batches (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id    TEXT,
                name         TEXT,
                operator     TEXT,
                action       TEXT,
                dry_run      INTEGER DEFAULT 0,
                total        INTEGER DEFAULT 0,
                sent         INTEGER DEFAULT 0,
                skipped      INTEGER DEFAULT 0,
                failed       INTEGER DEFAULT 0,
                flagged      INTEGER DEFAULT 0,
                status       TEXT,
                started_at   TEXT,
                finished_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS outbound_requests (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id         TEXT,
                batch_id          INTEGER,
                operator          TEXT,
                linkedin_url      TEXT,
                normalized_linkedin_url TEXT,
                full_name         TEXT,
                first_name        TEXT,
                company_csv       TEXT,
                role              TEXT,
                email             TEXT,
                action_requested  TEXT,
                action_executed   TEXT,
                template_id       INTEGER,
                template_name     TEXT,
                message_rendered  TEXT,
                status            TEXT,
                detail            TEXT,
                decision_trace    TEXT,
                screenshot_path   TEXT,
                created_at        TEXT,
                completed_at      TEXT,
                FOREIGN KEY(batch_id) REFERENCES batches(id)
            );

            CREATE TABLE IF NOT EXISTS templates (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT UNIQUE,
                body        TEXT,
                action      TEXT,
                tags        TEXT,
                created_at  TEXT,
                updated_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                goal TEXT,
                template_id INTEGER,
                action TEXT,
                voice TEXT,
                operator TEXT,
                scheduling_json TEXT,
                safety_json TEXT,
                status TEXT DEFAULT 'draft',
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS contact_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_url TEXT,
                tag TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS contact_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_url TEXT,
                note TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS voice_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                description TEXT,
                system_prompt TEXT,
                examples_json TEXT,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS operators (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE,
                label TEXT,
                profile_dir TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS account_contacts (
                operator TEXT NOT NULL,
                linkedin_url TEXT NOT NULL,
                last_observed_status TEXT,
                degree TEXT,
                last_action_type TEXT,
                first_seen_at TEXT,
                last_observed_at TEXT,
                PRIMARY KEY (operator, linkedin_url)
            );

            CREATE INDEX IF NOT EXISTS idx_requests_batch ON outbound_requests(batch_id);
            CREATE INDEX IF NOT EXISTS idx_requests_created ON outbound_requests(created_at);
            """
        )
        try:
            # DDL bootstrap above is idempotent. Column changes, data backfill,
            # indexes, and user_version advance commit as one migration unit.
            _CONN.execute("BEGIN")
            _ensure_column(_CONN, "contacts", "degree", "degree TEXT")
            _ensure_column(_CONN, "contacts", "last_action_type", "last_action_type TEXT")
            _ensure_column(_CONN, "contacts", "headline", "headline TEXT")
            _ensure_column(_CONN, "contacts", "normalized_linkedin_url", "normalized_linkedin_url TEXT")
            _ensure_column(_CONN, "outbound_requests", "headline", "headline TEXT")
            _ensure_column(_CONN, "outbound_requests", "normalized_linkedin_url", "normalized_linkedin_url TEXT")
            _migrate_account_contacts(_CONN)
            _migrate_campaign_queue(_CONN)
            _migrate_outreach_uncertainties(_CONN)
            _migrate_campaign_pause_reason(_CONN)
            _migrate_inbound_sync(_CONN)
            _migrate_inbound_read_state(_CONN)
            _migrate_inbox_open_intents(_CONN)
            _migrate_operator_identity(_CONN)
            _migrate_exit_node_settings(_CONN)
            _CONN.execute("CREATE INDEX IF NOT EXISTS idx_requests_normalized_status ON outbound_requests(normalized_linkedin_url, status)")
            _CONN.execute("CREATE INDEX IF NOT EXISTS idx_contacts_normalized_url ON contacts(normalized_linkedin_url)")
            _CONN.commit()
        except Exception:
            _CONN.rollback()
            _CONN.close()
            _CONN = None
            _DB_PATH = None
            raise


def _migrate_account_contacts(conn: sqlite3.Connection) -> None:
    """Versioned, conservative backfill from request attribution and legacy rows."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= 2:
        return
    now = _now()
    # Clear statuses that version 1 may have copied from later failures/skips.
    # The immutable request history below reconstructs the latest positive fact.
    for row in conn.execute("SELECT operator, linkedin_url, last_observed_status FROM account_contacts").fetchall():
        if row[2] not in TERMINAL_CONTACTED:
            conn.execute(
                "UPDATE account_contacts SET last_observed_status=NULL, last_observed_at=NULL WHERE operator=? AND linkedin_url=?",
                (row[0], row[1]),
            )
    history = conn.execute(
        "SELECT operator, linkedin_url, status, created_at, completed_at FROM outbound_requests ORDER BY id"
    ).fetchall()
    # A legacy profile row is attributable only when it names an operator and
    # request history does not show a conflicting operator for that URL.
    request_owners: dict[str, set[str]] = {}
    for owner, request_url in conn.execute("SELECT operator, linkedin_url FROM outbound_requests"):
        normalized = normalize_url(request_url or "")
        if normalized and (owner or "").strip():
            request_owners.setdefault(normalized, set()).add(owner.strip())
    for row in conn.execute("SELECT rowid AS db_rowid, * FROM contacts").fetchall():
        contact = dict(row)
        operator = (contact.get("operator") or "").strip()
        url = normalize_url(contact.get("linkedin_url") or "")
        if not url:
            continue
        conn.execute("UPDATE contacts SET normalized_linkedin_url=? WHERE rowid=?", (url, row["db_rowid"]))
        if not operator:
            continue
        owners = request_owners.get(url, set())
        if owners - {operator}:
            continue
        # A mutable legacy summary is not proof that a send occurred.
        legacy_status = (
            LEGACY_UNVERIFIED if contact.get("last_status") in TERMINAL_CONTACTED else None
        )
        conn.execute(
            """INSERT INTO account_contacts
                   (operator, linkedin_url, last_observed_status, degree, last_action_type,
                    first_seen_at, last_observed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(operator, linkedin_url) DO UPDATE SET
                   last_observed_status=COALESCE(account_contacts.last_observed_status, excluded.last_observed_status),
                   last_observed_at=COALESCE(account_contacts.last_observed_at, excluded.last_observed_at)""",
            (operator, url, legacy_status, contact.get("degree"),
             contact.get("last_action_type"), contact.get("first_seen_at") or now,
             contact.get("last_action_at") if legacy_status else None),
        )
    # Request rows are the durable attempt ledger. Create rows for failed/skipped
    # attempts, but refresh relationship status only for positive observations.
    for row in history:
        operator, url = (row[0] or "").strip(), normalize_url(row[1] or "")
        if not operator or not url or row[2] == "dry_run":
            continue
        observed_at = row[4] or row[3] or now
        positive_status = row[2] if row[2] in TERMINAL_CONTACTED else None
        conn.execute(
            """INSERT INTO account_contacts
                   (operator, linkedin_url, last_observed_status, first_seen_at, last_observed_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(operator, linkedin_url) DO UPDATE SET
                   last_observed_status=COALESCE(excluded.last_observed_status, account_contacts.last_observed_status),
                   last_observed_at=COALESCE(excluded.last_observed_at, account_contacts.last_observed_at)""",
            (operator, url, positive_status, row[3] or observed_at,
             observed_at if positive_status else None),
        )
    for row in conn.execute("SELECT rowid AS db_rowid, linkedin_url FROM outbound_requests").fetchall():
        normalized = normalize_url(row["linkedin_url"] or "")
        if normalized:
            conn.execute("UPDATE outbound_requests SET normalized_linkedin_url=? WHERE rowid=?",
                (normalized, row["db_rowid"]))
    conn.execute("PRAGMA user_version = 2")


def _migrate_campaign_queue(conn: sqlite3.Connection) -> None:
    """Add durable queue metadata and targets as schema version 3."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= 3:
        return
    _ensure_column(conn, "campaigns", "timezone", "timezone TEXT")
    _ensure_column(conn, "campaigns", "daily_chunk", "daily_chunk INTEGER")
    _ensure_column(conn, "campaigns", "start_at_utc", "start_at_utc TEXT")
    _ensure_column(conn, "campaigns", "last_run_local_date", "last_run_local_date TEXT")
    _ensure_column(conn, "campaigns", "source_name", "source_name TEXT")
    _ensure_column(conn, "campaigns", "source_bytes", "source_bytes BLOB")
    _ensure_column(conn, "campaigns", "validation_json", "validation_json TEXT")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS campaign_targets (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
               operator TEXT NOT NULL,
               ordinal INTEGER NOT NULL,
               linkedin_url TEXT NOT NULL,
               normalized_linkedin_url TEXT NOT NULL,
               job_json TEXT NOT NULL,
               state TEXT NOT NULL,
               available_at_utc TEXT NOT NULL,
               claimed_at_utc TEXT,
               completed_at_utc TEXT,
               batch_id INTEGER,
               request_id INTEGER,
               detail TEXT,
               UNIQUE(campaign_id, normalized_linkedin_url)
           )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_campaign_targets_due ON campaign_targets(state, available_at_utc, campaign_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_campaign_targets_campaign ON campaign_targets(campaign_id, ordinal)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_campaign_targets_batch ON campaign_targets(batch_id)")
    conn.execute("PRAGMA user_version = 3")


def _migrate_outreach_uncertainties(conn: sqlite3.Connection) -> None:
    """Version 4 records possible browser actions before they can be retried."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= 4:
        return
    conn.execute(
        """CREATE TABLE outreach_uncertainties (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               operator TEXT NOT NULL,
               normalized_linkedin_url TEXT NOT NULL,
               batch_id INTEGER,
               target_id INTEGER UNIQUE REFERENCES campaign_targets(id),
               request_id INTEGER UNIQUE REFERENCES outbound_requests(id),
               detected_at TEXT NOT NULL,
               verdict TEXT CHECK(verdict IN ('sent', 'not_sent', 'recorded')),
               reviewed_at TEXT,
               note TEXT NOT NULL DEFAULT ''
           )"""
    )
    conn.execute(
        "CREATE INDEX idx_uncertainties_profile ON outreach_uncertainties(normalized_linkedin_url, verdict)"
    )
    conn.execute("PRAGMA user_version = 4")


def _migrate_campaign_pause_reason(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= 5:
        return
    _ensure_column(conn, "campaigns", "pause_reason", "pause_reason TEXT")
    conn.execute("PRAGMA user_version = 5")


def _migrate_inbound_sync(conn: sqlite3.Connection) -> None:
    """Add durable, account-scoped inbox observations as schema version 6."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= 6:
        return
    statements = (
        """CREATE TABLE sync_runs (
            id INTEGER PRIMARY KEY,
            operator TEXT NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            expected_sections_json TEXT NOT NULL DEFAULT '[]',
            coverage_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT ''
        )""",

        """CREATE TABLE conversations (
            id INTEGER PRIMARY KEY,
            operator TEXT NOT NULL,
            thread_key TEXT NOT NULL,
            contact_url TEXT,
            participant_name TEXT NOT NULL DEFAULT '',
            section TEXT NOT NULL DEFAULT '',
            preview_text TEXT NOT NULL DEFAULT '',
            linkedin_unread INTEGER,
            match_state TEXT NOT NULL DEFAULT 'unmatched',
            first_observed_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            reviewed_at TEXT,
            UNIQUE(operator, thread_key)
        )""",

        """CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id),
            source_key TEXT NOT NULL,
            direction TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            source_at TEXT,
            first_observed_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            UNIQUE(conversation_id, source_key)
        )""",

        """CREATE TABLE attachments (
            id INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL REFERENCES messages(id),
            source_key TEXT NOT NULL,
            filename TEXT NOT NULL DEFAULT '',
            mime_type TEXT NOT NULL DEFAULT '',
            size_bytes INTEGER,
            sha256 TEXT,
            relative_path TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            observed_at TEXT NOT NULL,
            UNIQUE(message_id, source_key)
        )""",

        """CREATE TABLE relationship_observations (
            id INTEGER PRIMARY KEY,
            operator TEXT NOT NULL,
            contact_url TEXT NOT NULL,
            fact TEXT NOT NULL,
            source TEXT NOT NULL,
            first_observed_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            UNIQUE(operator, contact_url, fact, source)
        )""",

        "CREATE INDEX idx_sync_runs_operator_latest ON sync_runs(operator, started_at DESC, id DESC)",
        "CREATE INDEX idx_conversations_operator_latest ON conversations(operator, last_observed_at DESC, id DESC)",
        "CREATE INDEX idx_messages_conversation_latest ON messages(conversation_id, source_at DESC, id DESC)",
        "CREATE INDEX idx_attachments_message_latest ON attachments(message_id, observed_at DESC, id DESC)",
        "CREATE INDEX idx_relationship_observations_operator_latest ON relationship_observations(operator, last_observed_at DESC, id DESC)",
    )
    for statement in statements:
        conn.execute(statement)
    conn.execute("PRAGMA user_version = 6")


def _migrate_inbound_read_state(conn: sqlite3.Connection) -> None:
    """Keep the pre-open unread state and its restoration result per scan."""
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) >= 7:
        return
    conn.execute("""CREATE TABLE conversation_scan_observations (
        run_id INTEGER NOT NULL REFERENCES sync_runs(id),
        conversation_id INTEGER NOT NULL REFERENCES conversations(id),
        linkedin_unread_before_open INTEGER,
        restore_status TEXT NOT NULL,
        restored_at TEXT,
        error TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (run_id, conversation_id)
    )""")
    conn.execute("CREATE INDEX idx_scan_observations_conversation_latest "
                 "ON conversation_scan_observations(conversation_id, run_id DESC)")
    conn.execute("PRAGMA user_version = 7")


def _migrate_inbox_open_intents(conn: sqlite3.Connection) -> None:
    """Version 8 retains an unread baseline before a thread URL is available."""
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) >= 8:
        return
    conn.execute("""CREATE TABLE inbox_open_intents (
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL REFERENCES sync_runs(id),
        operator TEXT NOT NULL,
        section TEXT NOT NULL,
        participant_name TEXT NOT NULL,
        preview_text TEXT NOT NULL,
        thread_key TEXT,
        restore_status TEXT NOT NULL DEFAULT 'pending',
        restored_at TEXT,
        error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(run_id, section, participant_name, preview_text)
    )""")
    conn.execute("CREATE INDEX idx_open_intents_pending ON inbox_open_intents(operator, restore_status, id)")
    conn.execute("PRAGMA user_version = 8")


def _migrate_operator_identity(conn: sqlite3.Connection) -> None:
    """Version 9 binds inbound scans to the signed-in LinkedIn account."""
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) >= 9:
        return
    _ensure_column(conn, "operators", "linkedin_self_url", "linkedin_self_url TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_operators_self_url ON operators(linkedin_self_url) "
                 "WHERE linkedin_self_url IS NOT NULL")
    conn.execute("PRAGMA user_version = 9")


def _migrate_exit_node_settings(conn: sqlite3.Connection) -> None:
    """Version 10 stores the operator-selected unattended route."""
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) >= 10:
        return
    conn.execute("""CREATE TABLE IF NOT EXISTS runtime_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    _ensure_column(conn, "batches", "exit_node_id", "exit_node_id TEXT")
    _ensure_column(conn, "batches", "egress_ipv4", "egress_ipv4 TEXT")
    _ensure_column(conn, "sync_runs", "exit_node_id", "exit_node_id TEXT")
    _ensure_column(conn, "sync_runs", "egress_ipv4", "egress_ipv4 TEXT")
    conn.execute("PRAGMA user_version = 10")


def get_default_exit_node_id() -> str:
    with _LOCK:
        row = _conn().execute(
            "SELECT value FROM runtime_settings WHERE key='default_exit_node_id'"
        ).fetchone()
        return str(row[0]) if row else ""


def set_default_exit_node_id(node_id: str) -> None:
    with _LOCK:
        _conn().execute(
            """INSERT INTO runtime_settings (key, value, updated_at)
               VALUES ('default_exit_node_id', ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                             updated_at=excluded.updated_at""",
            (node_id, _now()),
        )
        _conn().commit()


def record_batch_egress(batch_id: int, node_id: str, ipv4: str) -> None:
    with _LOCK:
        _conn().execute(
            "UPDATE batches SET exit_node_id=?, egress_ipv4=? WHERE id=?",
            (node_id, ipv4, batch_id),
        )
        _conn().commit()


def close_db() -> None:
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
            _CONN = None


def _conn() -> sqlite3.Connection:
    if _CONN is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _CONN


# ---- contacts -------------------------------------------------------------

def get_contact(linkedin_url: str) -> dict[str, Any] | None:
    key = normalize_url(linkedin_url)
    with _LOCK:
        cur = _conn().execute(
            "SELECT * FROM contacts WHERE normalized_linkedin_url = ? OR linkedin_url = ? LIMIT 1",
            (key, key),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_account_contact(operator: str, linkedin_url: str) -> dict[str, Any] | None:
    """Return one account-scoped relationship, with confirmed send history attached."""
    key = normalize_url(linkedin_url)
    with _LOCK:
        conn = _conn()
        row = conn.execute(
            "SELECT * FROM account_contacts WHERE operator=? AND linkedin_url=?",
            (operator, key),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["has_successful_send"] = _has_successful_send(conn, key, operator)
        return result


def list_account_contacts(operator: str, search: str = "", limit: int = 500) -> list[dict[str, Any]]:
    """List relationships for exactly one sender account."""
    with _LOCK:
        if search:
            like = f"%{search.lower()}%"
            rows = _conn().execute(
                """SELECT ac.*, c.full_name, c.first_name, c.company_csv, c.headline
                     FROM account_contacts ac LEFT JOIN contacts c ON c.rowid=(
                       SELECT p.rowid FROM contacts p
                        WHERE p.normalized_linkedin_url=ac.linkedin_url
                        ORDER BY COALESCE(p.last_action_at, p.first_seen_at, '') DESC, p.rowid DESC LIMIT 1)
                    WHERE ac.operator=? AND (lower(COALESCE(c.full_name,'')) LIKE ?
                       OR lower(COALESCE(c.company_csv,'')) LIKE ? OR lower(ac.linkedin_url) LIKE ?)
                    ORDER BY ac.last_observed_at DESC LIMIT ?""",
                (operator, like, like, like, limit),
            ).fetchall()
        else:
            rows = _conn().execute(
                """SELECT ac.*, c.full_name, c.first_name, c.company_csv, c.headline
                     FROM account_contacts ac LEFT JOIN contacts c ON c.rowid=(
                       SELECT p.rowid FROM contacts p
                        WHERE p.normalized_linkedin_url=ac.linkedin_url
                        ORDER BY COALESCE(p.last_action_at, p.first_seen_at, '') DESC, p.rowid DESC LIMIT 1)
                    WHERE ac.operator=? ORDER BY ac.last_observed_at DESC LIMIT ?""",
                (operator, limit),
            ).fetchall()
        results = [dict(row) for row in rows]
        invited = {
            row["normalized_linkedin_url"]: row["observed_at"]
            for row in _conn().execute(
                """SELECT normalized_linkedin_url, MAX(COALESCE(completed_at, created_at)) AS observed_at
                   FROM outbound_requests WHERE operator=? AND status='sent'
                     AND action_executed IN ('connect', 'connect_note')
                   GROUP BY normalized_linkedin_url""", (operator,)
            ).fetchall() if row["normalized_linkedin_url"]
        }
        connected = {
            row["contact_url"]: row["observed_at"]
            for row in _conn().execute(
                """SELECT contact_url, MIN(first_observed_at) AS observed_at
                   FROM relationship_observations WHERE operator=? AND fact='connected'
                   GROUP BY contact_url""", (operator,)
            ).fetchall()
        }
        replied = {
            row["contact_url"]: row["observed_at"]
            for row in _conn().execute(
                """SELECT c.contact_url, MAX(COALESCE(m.source_at, m.first_observed_at)) AS observed_at
                   FROM conversations c JOIN messages m ON m.conversation_id=c.id
                   WHERE c.operator=? AND c.contact_url IS NOT NULL AND m.direction='inbound'
                   GROUP BY c.contact_url""", (operator,)
            ).fetchall()
        }
        received_files = {
            row["contact_url"]: row["observed_at"]
            for row in _conn().execute(
                """SELECT c.contact_url, MAX(a.observed_at) AS observed_at
                   FROM conversations c JOIN messages m ON m.conversation_id=c.id
                   JOIN attachments a ON a.message_id=m.id
                   WHERE c.operator=? AND c.contact_url IS NOT NULL AND m.direction='inbound'
                     AND a.status='saved'
                   GROUP BY c.contact_url""", (operator,)
            ).fetchall()
        }
        for result in results:
            result["has_successful_send"] = _has_successful_send(
                _conn(), result["linkedin_url"], result["operator"]
            )
            url = result["linkedin_url"]
            result["invited_at"] = invited.get(url)
            result["connected_at"] = connected.get(url)
            result["accepted_at"] = connected.get(url) if url in invited else None
            result["replied_at"] = replied.get(url)
            result["file_received_at"] = received_files.get(url)
        return results


def _has_successful_send(conn: sqlite3.Connection, key: str, operator: str | None = None) -> bool:
    placeholders = ",".join("?" for _ in SENT_STATUSES)
    sql = f"SELECT 1 FROM outbound_requests WHERE normalized_linkedin_url=? AND status IN ({placeholders})"
    args: tuple[Any, ...] = (key, *SENT_STATUSES)
    if operator is not None:
        # The caller may want account-only history. Suppression below defaults
        # to all accounts because confirmed send history is global policy.
        sql += " AND operator=?"
        args += (operator,)
    return conn.execute(sql + " LIMIT 1", args).fetchone() is not None


def is_already_contacted(
    linkedin_url: str, contacted_statuses: set[str], operator: str,
    *, exclude_queue_target_id: int | None = None,
) -> bool:
    """Suppress confirmed or unresolved outreach before any browser action."""
    return should_suppress_contact(
        linkedin_url, contacted_statuses, operator,
        exclude_queue_target_id=exclude_queue_target_id,
    )


def should_suppress_contact(
    linkedin_url: str, contacted_statuses: set[str], operator: str,
    *, global_suppression: bool = True, allow_override: bool = False,
    exclude_queue_target_id: int | None = None,
) -> bool:
    """Policy hook for global confirmed-send suppression and future reviewed overrides."""
    key = normalize_url(linkedin_url)
    if not key:
        return False
    with _LOCK:
        conn = _conn()
        if not allow_override and global_suppression and _has_successful_send(conn, key):
            return True
        if not allow_override and global_suppression:
            unresolved = conn.execute(
                """SELECT 1 FROM outreach_uncertainties
                    WHERE normalized_linkedin_url=? AND (verdict IS NULL OR verdict='sent')
                    LIMIT 1""",
                (key,),
            ).fetchone()
            if unresolved:
                return True
            queued = conn.execute(
                """SELECT 1 FROM campaign_targets
                    WHERE normalized_linkedin_url=? AND state IN ('queued', 'sending', 'uncertain')
                      AND (? IS NULL OR id<>?) LIMIT 1""",
                (key, exclude_queue_target_id, exclude_queue_target_id),
            ).fetchone()
            if queued:
                return True
        row = conn.execute(
            "SELECT last_observed_status FROM account_contacts WHERE operator=? AND linkedin_url=?",
            (operator, key),
        ).fetchone()
        return bool(row and row["last_observed_status"] in (contacted_statuses | {LEGACY_UNVERIFIED}))


def _upsert_profile_details(conn: sqlite3.Connection, *, linkedin_url: str,
    full_name: str, first_name: str, company_csv: str, headline: str, now: str) -> None:
    """Refresh shared person details without treating legacy outreach fields as truth."""
    existing = conn.execute(
        "SELECT rowid FROM contacts WHERE normalized_linkedin_url = ? OR linkedin_url = ? ORDER BY rowid DESC LIMIT 1",
        (normalize_url(linkedin_url), linkedin_url),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE contacts SET
                   full_name=COALESCE(NULLIF(?, ''), full_name),
                   first_name=COALESCE(NULLIF(?, ''), first_name),
                   company_csv=COALESCE(NULLIF(?, ''), company_csv),
                   headline=COALESCE(NULLIF(?, ''), headline),
                   normalized_linkedin_url=? WHERE rowid=?""",
            (full_name, first_name, company_csv, headline, normalize_url(linkedin_url), existing["rowid"]),
        )
    else:
        conn.execute(
            """INSERT INTO contacts
                   (linkedin_url, normalized_linkedin_url, full_name, first_name, company_csv, headline, first_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (linkedin_url, normalize_url(linkedin_url), full_name, first_name, company_csv, headline, now),
        )
def list_contacts(operator: str, search: str = "", limit: int = 500) -> list[dict[str, Any]]:
    """Public contact listing is account-scoped; profile details are joined as attributes."""
    return list_account_contacts(operator=operator, search=search, limit=limit)


def count_sent_today(operator: str) -> int:
    """Count confirmed and possibly sent actions since UTC midnight today."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return count_sent_since(operator, f"{today}T00:00:00+00:00")


def count_sent_since(operator: str, since_utc: str) -> int:
    """Count confirmed sends and possibly sent queued targets in a rolling window."""
    placeholders = ",".join("?" for _ in SENT_STATUSES)
    with _LOCK:
        row = _conn().execute(
            f"""SELECT COUNT(*) AS c FROM outbound_requests
                 WHERE operator=? AND status IN ({placeholders}) AND created_at>?""",
            (operator, *SENT_STATUSES, since_utc),
        ).fetchone()
        pending = _conn().execute(
            """SELECT COUNT(*) AS c FROM campaign_targets
                WHERE operator=? AND state='sending' AND claimed_at_utc>?""",
            (operator, since_utc),
        ).fetchone()
        uncertain = _conn().execute(
            """SELECT COUNT(*) AS c FROM outreach_uncertainties
                WHERE operator=? AND detected_at>?
                  AND (verdict IS NULL OR verdict='sent')""",
            (operator, since_utc),
        ).fetchone()
        return int(row["c"]) + int(pending["c"]) + int(uncertain["c"])


# ---- batches --------------------------------------------------------------

def create_batch(operator: str, name: str, action: str, dry_run: bool, total: int) -> tuple[int, str]:
    with _LOCK:
        conn = _conn()
        cur = conn.execute(
            """
            INSERT INTO batches (name, operator, action, dry_run, total, status, started_at)
            VALUES (?, ?, ?, ?, ?, 'running', ?)
            """,
            (name, operator, action, 1 if dry_run else 0, total, _now()),
        )
        batch_id = int(cur.lastrowid)
        public_id = f"B{batch_id:04d}"
        conn.execute("UPDATE batches SET public_id=? WHERE id=?", (public_id, batch_id))
        conn.commit()
        return batch_id, public_id


def update_batch_counts(batch_id: int, sent: int, skipped: int, failed: int, flagged: int) -> None:
    with _LOCK:
        conn = _conn()
        conn.execute(
            "UPDATE batches SET sent=?, skipped=?, failed=?, flagged=? WHERE id=?",
            (sent, skipped, failed, flagged, batch_id),
        )
        conn.commit()


def finalize_batch(batch_id: int, status: str) -> None:
    with _LOCK:
        conn = _conn()
        conn.execute(
            "UPDATE batches SET status=?, finished_at=? WHERE id=?",
            (status, _now(), batch_id),
        )
        conn.commit()


def list_batches(operator: str, limit: int = 50) -> list[dict[str, Any]]:
    with _LOCK:
        cur = _conn().execute(
            "SELECT * FROM batches WHERE operator=? ORDER BY id DESC LIMIT ?",
            (operator, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def get_batch(batch_id: int) -> dict[str, Any] | None:
    with _LOCK:
        row = _conn().execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        return dict(row) if row else None


# ---- outbound requests (audit trail) --------------------------------------

def _insert_request(conn: sqlite3.Connection, **data: Any) -> tuple[int, str]:
    now = _now()
    trace_json = json.dumps(data.get("decision_trace") or [])
    cur = conn.execute(
            """
            INSERT INTO outbound_requests
                (batch_id, operator, linkedin_url, normalized_linkedin_url, full_name, first_name, company_csv,
                 role, email, action_requested, action_executed, template_id,
                 template_name, message_rendered, status, detail, decision_trace,
                 screenshot_path, headline, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (data["batch_id"], data["operator"], data["linkedin_url"], normalize_url(data["linkedin_url"]),
             data["full_name"], data["first_name"], data["company_csv"], data["role"],
             data["email"], data["action_requested"], data["action_executed"],
             data["template_id"], data["template_name"], data["message_rendered"],
             data["status"], data.get("detail", ""), trace_json,
             data.get("screenshot_path", ""), data.get("headline", ""), now, now),
        )
    req_id = int(cur.lastrowid)
    public_id = f"OBR-{req_id:06d}"
    conn.execute("UPDATE outbound_requests SET public_id=? WHERE id=?", (public_id, req_id))
    return req_id, public_id


def record_outcome(
    *, batch_id: int, operator: str, linkedin_url: str, full_name: str,
    first_name: str, company_csv: str, role: str, email: str,
    action_requested: str, action_executed: str, template_id: int | None,
    template_name: str, message_rendered: str, status: str, detail: str = "",
    decision_trace: list[Any] | None = None, screenshot_path: str = "",
    headline: str = "", degree: str = "", queue_target_id: int | None = None,
    uncertainty_id: int | None = None,
) -> tuple[int, str]:
    """Atomically append an attempt and refresh account/profile observations."""
    key = normalize_url(linkedin_url)
    now = _now()
    with _LOCK:
        conn = _conn()
        try:
            conn.execute("BEGIN")
            result = _insert_request(conn, batch_id=batch_id, operator=operator,
                linkedin_url=key, full_name=full_name, first_name=first_name,
                company_csv=company_csv, role=role, email=email,
                action_requested=action_requested, action_executed=action_executed,
                template_id=template_id, template_name=template_name,
                message_rendered=message_rendered, status=status, detail=detail,
                decision_trace=decision_trace, screenshot_path=screenshot_path, headline=headline)
            if status != "dry_run":
                positive_status = status if status in TERMINAL_CONTACTED else None
                conn.execute(
                    """INSERT INTO account_contacts
                           (operator, linkedin_url, last_observed_status, degree,
                            last_action_type, first_seen_at, last_observed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(operator, linkedin_url) DO UPDATE SET
                           last_observed_status=COALESCE(excluded.last_observed_status, account_contacts.last_observed_status),
                           degree=CASE WHEN excluded.last_observed_status IS NULL OR excluded.degree='' THEN account_contacts.degree ELSE excluded.degree END,
                           last_action_type=CASE WHEN excluded.last_observed_status IS NULL THEN account_contacts.last_action_type ELSE excluded.last_action_type END,
                           last_observed_at=COALESCE(excluded.last_observed_at, account_contacts.last_observed_at)""",
                    (operator, key, positive_status, degree, action_executed, now,
                     now if positive_status else None),
                )
                _upsert_profile_details(conn, linkedin_url=key, full_name=full_name,
                    first_name=first_name, company_csv=company_csv, headline=headline, now=now)
            if queue_target_id is not None:
                if status in SENT_STATUSES:
                    target_state = "sent"
                elif status in {"skipped_dedup", "pending", "already_connected"}:
                    target_state = "skipped"
                elif status in {"failed_other", "dry_run"}:
                    target_state = "uncertain"
                else:
                    target_state = "failed"
                updated = conn.execute(
                    """UPDATE campaign_targets
                          SET state=?, completed_at_utc=?, request_id=?, detail=?
                        WHERE id=? AND state='sending' AND batch_id=?
                          AND operator=? AND normalized_linkedin_url=?""",
                    (target_state, now, result[0], detail, queue_target_id,
                     batch_id, operator, key),
                )
                if updated.rowcount != 1:
                    raise ValueError("Queued target claim is missing or belongs to another run")
                if status == "failed_other":
                    conn.execute(
                        """INSERT INTO outreach_uncertainties
                               (operator, normalized_linkedin_url, batch_id, target_id,
                                request_id, detected_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (operator, key, batch_id, queue_target_id, result[0], now),
                    )
            if uncertainty_id is not None:
                verdict = None if status == "failed_other" else "recorded"
                updated = conn.execute(
                    """UPDATE outreach_uncertainties
                          SET request_id=?, verdict=?, reviewed_at=CASE WHEN ? IS NULL THEN NULL ELSE ? END
                        WHERE id=? AND operator=? AND batch_id=? AND verdict IS NULL""",
                    (result[0], verdict, verdict, now, uncertainty_id, operator, batch_id),
                )
                if updated.rowcount != 1:
                    raise ValueError("Immediate browser attempt marker is missing")
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def list_requests(batch_id: int) -> list[dict[str, Any]]:
    with _LOCK:
        cur = _conn().execute(
            "SELECT * FROM outbound_requests WHERE batch_id = ? ORDER BY id ASC", (batch_id,)
        )
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            try:
                d["decision_trace"] = json.loads(d.get("decision_trace") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["decision_trace"] = []
            rows.append(d)
        return rows


def list_contact_timeline(operator: str, linkedin_url: str) -> list[dict[str, Any]]:
    """Return account-scoped outbound history plus shared tags and notes for a profile."""
    key = normalize_url(linkedin_url)
    with _LOCK:
        conn = _conn()
        events: list[dict[str, Any]] = []
        requests = conn.execute(
            """SELECT * FROM outbound_requests
                WHERE operator=? AND normalized_linkedin_url=?
                ORDER BY COALESCE(completed_at, created_at) DESC, id DESC""",
            (operator, key),
        ).fetchall()
        for row in requests:
            event = dict(row)
            try:
                event["decision_trace"] = json.loads(event.get("decision_trace") or "[]")
            except (json.JSONDecodeError, TypeError):
                event["decision_trace"] = []
            event.update(type="outbound_request", timestamp=event.get("completed_at") or event.get("created_at"))
            events.append(event)

        # These are profile annotations, not outreach history, so they remain
        # shared across sender accounts while outbound rows are account-scoped.
        for row in conn.execute("SELECT id, contact_url, tag, created_at FROM contact_tags"):
            if normalize_url(row["contact_url"] or "") == key:
                events.append({"type": "tag", "id": row["id"], "tag": row["tag"], "timestamp": row["created_at"]})
        for row in conn.execute("SELECT id, contact_url, note, created_at FROM contact_notes"):
            if normalize_url(row["contact_url"] or "") == key:
                events.append({"type": "note", "id": row["id"], "note": row["note"], "timestamp": row["created_at"]})
        events.sort(key=lambda item: item.get("timestamp") or "", reverse=True)
        return events


def dashboard_metrics(operator: str) -> dict[str, Any]:
    """Return outbound metrics for a single sender account."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    sent = tuple(SENT_STATUSES)
    placeholders = ",".join("?" for _ in sent)
    with _LOCK:
        conn = _conn()
        total = conn.execute(
            f"SELECT COUNT(DISTINCT normalized_linkedin_url) AS c FROM outbound_requests WHERE operator=? AND status IN ({placeholders})",
            (operator, *sent),
        ).fetchone()["c"]
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM campaigns WHERE operator=? AND status='active'",
            (operator,),
        ).fetchone()["c"]
        today_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM outbound_requests WHERE operator=? AND status IN ({placeholders}) AND substr(created_at, 1, 10)=?",
            (operator, *sent, today),
        ).fetchone()["c"]
        over_time = [dict(row) for row in conn.execute(
            f"""SELECT substr(created_at, 1, 10) AS date, COUNT(*) AS count
                   FROM outbound_requests WHERE operator=? AND status IN ({placeholders})
                     AND created_at>=? GROUP BY date ORDER BY date""",
            (operator, *sent, since),
        )]
        breakdown = [dict(row) for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM outbound_requests WHERE operator=? GROUP BY status",
            (operator,),
        )]
        templates = [dict(row) for row in conn.execute(
            f"""SELECT template_name, COUNT(*) AS count FROM outbound_requests
                   WHERE operator=? AND status IN ({placeholders})
                   GROUP BY template_name ORDER BY count DESC LIMIT 10""",
            (operator, *sent),
        )]
    return {
        "total_contacted": total,
        "active_campaigns": active,
        "sent_today": today_count,
        "sends_over_time": over_time,
        "status_breakdown": breakdown,
        "template_performance": templates,
    }


# ---- templates ------------------------------------------------------------

def seed_templates(defaults: dict[str, str], default_action: str = "connect_note") -> None:
    """Seed the templates table from templates.yaml the first time only."""
    with _LOCK:
        conn = _conn()
        count = conn.execute("SELECT COUNT(*) AS c FROM templates").fetchone()["c"]
        if count:
            return
        now = _now()
        for name, body in defaults.items():
            conn.execute(
                """
                INSERT OR IGNORE INTO templates (name, body, action, tags, created_at, updated_at)
                VALUES (?, ?, ?, '', ?, ?)
                """,
                (str(name), str(body), default_action, now, now),
            )
        conn.commit()


def list_templates() -> list[dict[str, Any]]:
    with _LOCK:
        cur = _conn().execute("SELECT * FROM templates ORDER BY name ASC")
        return [dict(r) for r in cur.fetchall()]


def get_template(template_id: int) -> dict[str, Any] | None:
    with _LOCK:
        row = _conn().execute("SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()
        return dict(row) if row else None


def get_template_by_name(name: str) -> dict[str, Any] | None:
    with _LOCK:
        row = _conn().execute(
            "SELECT * FROM templates WHERE lower(name) = lower(?)", (name,)
        ).fetchone()
        return dict(row) if row else None


def create_template(name: str, body: str, action: str, tags: str = "") -> int:
    now = _now()
    with _LOCK:
        conn = _conn()
        cur = conn.execute(
            """
            INSERT INTO templates (name, body, action, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, body, action, tags, now, now),
        )
        conn.commit()
        return int(cur.lastrowid)


def update_template(template_id: int, fields: dict[str, Any]) -> bool:
    allowed = {"name", "body", "action", "tags"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return False
    sets["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in sets)
    with _LOCK:
        conn = _conn()
        cur = conn.execute(
            f"UPDATE templates SET {cols} WHERE id=?", (*sets.values(), template_id)
        )
        conn.commit()
        return cur.rowcount > 0


def delete_template(template_id: int) -> bool:
    with _LOCK:
        conn = _conn()
        cur = conn.execute("DELETE FROM templates WHERE id=?", (template_id,))
        conn.commit()
        return cur.rowcount > 0

# ---- operators ------------------------------------------------------------

def seed_operators(defaults: dict[str, Any]) -> None:
    with _LOCK:
        conn = _conn()
        now = _now()
        for key, op in defaults.items():
            conn.execute(
                """
                INSERT OR IGNORE INTO operators (key, label, profile_dir, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (op.key, op.label, op.profile_dir, now),
            )
        conn.commit()

def list_operators() -> list[dict[str, Any]]:
    with _LOCK:
        cur = _conn().execute("SELECT * FROM operators ORDER BY created_at ASC")
        return [dict(r) for r in cur.fetchall()]

def create_operator(key: str, label: str, profile_dir: str) -> None:
    now = _now()
    with _LOCK:
        conn = _conn()
        conn.execute(
            """
            INSERT INTO operators (key, label, profile_dir, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (key, label, profile_dir, now),
        )
        conn.commit()


def set_operator_self_profile_url(key: str, raw_url: str) -> str:
    """Bind one sender to the exact LinkedIn profile shown by its Me menu."""
    try:
        canonical = canonical_profile_url(raw_url)
    except ValueError as exc:
        raise ValueError("A LinkedIn /in/ profile URL is required") from exc
    path = urlsplit(canonical).path
    if not path.startswith("/in/") or path.count("/") != 2:
        raise ValueError("A LinkedIn /in/ profile URL is required")
    url = normalize_url(canonical)
    with _LOCK:
        conn = _conn()
        row = conn.execute(
            "SELECT linkedin_self_url FROM operators WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown sender account")
        if row["linkedin_self_url"] not in {None, url} and any(
            conn.execute(f"SELECT 1 FROM {table} WHERE operator=? LIMIT 1", (key,)).fetchone()
            for table in ("sync_runs", "outbound_requests", "account_contacts", "campaigns", "batches")
        ):
            raise ValueError("This account has history; review it before changing its identity")
        if conn.execute(
            "SELECT 1 FROM operators WHERE linkedin_self_url=? AND key<>?", (url, key)
        ).fetchone():
            raise ValueError("This LinkedIn profile is already bound to another sender account")
        conn.execute("UPDATE operators SET linkedin_self_url=? WHERE key=?", (url, key))
        conn.commit()
    return url

def delete_operator(key: str) -> bool:
    with _LOCK:
        conn = _conn()
        for table in (
            "batches", "outbound_requests", "account_contacts", "campaigns",
            "sync_runs", "conversations", "relationship_observations",
        ):
            if conn.execute(
                f"SELECT 1 FROM {table} WHERE operator=? LIMIT 1", (key,)
            ).fetchone():
                raise ValueError("This account has history and cannot be deleted.")
        cur = conn.execute("DELETE FROM operators WHERE key=?", (key,))
        conn.commit()
        return cur.rowcount > 0


# ---- durable outbound campaign queue -------------------------------------

def create_queued_campaign(
    operator: str, name: str, action: str, timezone: str, daily_chunk: int,
    start_at_utc: str | None, targets: list[dict[str, Any]], *,
    source_name: str = "", source_bytes: bytes = b"", validation_json: str = "[]",
    run_options_json: str = "{}",
) -> int:
    """Create a queued campaign and its scheduled targets atomically."""
    now = _now()
    with _LOCK:
        conn = _conn()
        try:
            conn.execute("BEGIN")
            if not targets:
                raise ValueError("A queued campaign needs at least one target")
            cur = conn.execute(
                """INSERT INTO campaigns
                       (name, action, operator, status, created_at, updated_at,
                        timezone, daily_chunk, start_at_utc, source_name,
                        source_bytes, validation_json, safety_json)
                   VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, action, operator, now, now, timezone, daily_chunk, start_at_utc,
                 source_name, source_bytes, validation_json, run_options_json),
            )
            campaign_id = int(cur.lastrowid)
            for ordinal, target in enumerate(targets):
                job = target["job"]
                linkedin_url = str(job.get("linkedin_url") or "").strip()
                normalized = normalize_url(linkedin_url)
                if not normalized:
                    raise ValueError("Each campaign target must include a LinkedIn profile URL.")
                if _has_successful_send(conn, normalized):
                    raise ValueError(f"Profile has a confirmed prior send: {linkedin_url}")
                relationship = conn.execute(
                    """SELECT last_observed_status FROM account_contacts
                        WHERE operator=? AND linkedin_url=?""",
                    (operator, normalized),
                ).fetchone()
                if relationship and relationship["last_observed_status"] in (TERMINAL_CONTACTED | {LEGACY_UNVERIFIED}):
                    raise ValueError(f"Profile is already contacted on this account: {linkedin_url}")
                uncertainty = conn.execute(
                    """SELECT 1 FROM outreach_uncertainties
                        WHERE normalized_linkedin_url=?
                          AND (verdict IS NULL OR verdict='sent') LIMIT 1""",
                    (normalized,),
                ).fetchone()
                if uncertainty:
                    raise ValueError(f"Profile has unresolved or confirmed outreach: {linkedin_url}")
                duplicate = conn.execute(
                    """SELECT 1 FROM campaign_targets
                        WHERE normalized_linkedin_url=?
                          AND state IN ('queued', 'sending', 'uncertain') LIMIT 1""",
                    (normalized,),
                ).fetchone()
                if duplicate:
                    raise ValueError(f"Profile has active or unresolved queued outreach: {linkedin_url}")
                conn.execute(
                    """INSERT INTO campaign_targets
                           (campaign_id, operator, ordinal, linkedin_url,
                            normalized_linkedin_url, job_json, state, available_at_utc)
                       VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)""",
                    (campaign_id, operator, ordinal, linkedin_url, normalized,
                     json.dumps(job, ensure_ascii=False), target["available_at_utc"]),
                )
            conn.commit()
            return campaign_id
        except Exception:
            conn.rollback()
            raise


def list_due_targets(now_utc: str, limit: int) -> list[dict[str, Any]]:
    """List available targets whose parent campaign is currently queued."""
    with _LOCK:
        rows = _conn().execute(
            """SELECT ct.*, c.name AS campaign_name, c.action AS campaign_action,
                      c.timezone AS campaign_timezone, c.daily_chunk AS campaign_daily_chunk,
                      c.safety_json AS campaign_run_options,
                      c.last_run_local_date AS campaign_last_run_local_date
                 FROM campaign_targets ct JOIN campaigns c ON c.id=ct.campaign_id
                WHERE c.status='queued' AND ct.state='queued' AND ct.available_at_utc<=?
                ORDER BY ct.available_at_utc, ct.campaign_id, ct.ordinal
                LIMIT ?""",
            (now_utc, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def list_due_campaign_heads(now_utc: str) -> list[dict[str, Any]]:
    """Return the earliest due target for each queued campaign.

    A large first campaign must not hide other due campaigns behind a row limit.
    """
    with _LOCK:
        rows = _conn().execute(
            """SELECT ct.*, c.name AS campaign_name, c.action AS campaign_action,
                      c.timezone AS campaign_timezone, c.daily_chunk AS campaign_daily_chunk,
                      c.safety_json AS campaign_run_options,
                      c.last_run_local_date AS campaign_last_run_local_date
                 FROM campaigns c JOIN campaign_targets ct ON ct.id=(
                      SELECT t.id FROM campaign_targets t
                       WHERE t.campaign_id=c.id AND t.state='queued'
                         AND t.available_at_utc<=?
                       ORDER BY t.available_at_utc, t.ordinal LIMIT 1)
                WHERE c.status='queued'
                ORDER BY ct.available_at_utc, ct.campaign_id""",
            (now_utc,),
        ).fetchall()
        return [dict(row) for row in rows]


def list_due_campaign_chunk(campaign_id: int, now_utc: str, limit: int) -> list[dict[str, Any]]:
    """Read only this campaign's next due targets after choosing a campaign."""
    with _LOCK:
        rows = _conn().execute(
            """SELECT * FROM campaign_targets
                WHERE campaign_id=? AND state='queued' AND available_at_utc<=?
                ORDER BY available_at_utc, ordinal LIMIT ?""",
            (campaign_id, now_utc, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def list_campaign_targets(campaign_id: int) -> list[dict[str, Any]]:
    with _LOCK:
        rows = _conn().execute(
            "SELECT * FROM campaign_targets WHERE campaign_id=? ORDER BY ordinal, id",
            (campaign_id,),
        ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            try:
                item["job"] = json.loads(item.pop("job_json"))
            except (json.JSONDecodeError, TypeError):
                item["job"] = {}
            results.append(item)
        return results


def list_queued_campaigns(operator: str) -> list[dict[str, Any]]:
    """Account-scoped queue overview without copying original source bytes."""
    with _LOCK:
        rows = _conn().execute(
            """SELECT c.id, c.name, c.operator, c.action, c.status, c.timezone,
                      c.daily_chunk, c.start_at_utc, c.source_name, c.pause_reason,
                      c.safety_json AS run_options_json, c.created_at,
                      c.updated_at, COUNT(t.id) AS total,
                      SUM(CASE WHEN t.state='queued' THEN 1 ELSE 0 END) AS queued,
                      SUM(CASE WHEN t.state='sending' THEN 1 ELSE 0 END) AS sending,
                      SUM(CASE WHEN t.state='sent' THEN 1 ELSE 0 END) AS sent,
                      SUM(CASE WHEN t.state='skipped' THEN 1 ELSE 0 END) AS skipped,
                      SUM(CASE WHEN t.state='failed' THEN 1 ELSE 0 END) AS failed,
                      SUM(CASE WHEN t.state='uncertain' THEN 1 ELSE 0 END) AS uncertain,
                      SUM(CASE WHEN t.state='cancelled' THEN 1 ELSE 0 END) AS cancelled,
                      MIN(CASE WHEN t.state='queued' THEN t.available_at_utc END) AS next_due_at_utc
                 FROM campaigns c JOIN campaign_targets t ON t.campaign_id=c.id
                WHERE c.operator=? GROUP BY c.id ORDER BY c.id DESC""",
            (operator,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            options = json.loads(item.pop("run_options_json") or "{}")
            item["exit_node_id"] = str(options.get("exit_node_id") or "")
            result.append(item)
        return result


def get_queued_campaign(campaign_id: int, operator: str) -> dict[str, Any] | None:
    with _LOCK:
        row = _conn().execute(
            """SELECT id, name, operator, action, status, timezone, daily_chunk,
                      start_at_utc, source_name, validation_json, pause_reason,
                      safety_json AS run_options_json,
                      created_at, updated_at
                 FROM campaigns WHERE id=? AND operator=?
                   AND EXISTS (SELECT 1 FROM campaign_targets WHERE campaign_id=?)""",
            (campaign_id, operator, campaign_id),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        options = json.loads(item.pop("run_options_json") or "{}")
        item["exit_node_id"] = str(options.get("exit_node_id") or "")
        return item


def set_campaign_exit_node_id(campaign_id: int, operator: str, node_id: str) -> bool:
    """Change the route for future chunks without touching a running target."""
    with _LOCK:
        conn = _conn()
        row = conn.execute(
            "SELECT status, safety_json AS run_options_json FROM campaigns WHERE id=? AND operator=?",
            (campaign_id, operator),
        ).fetchone()
        if row is None:
            return False
        if row["status"] not in {"queued", "paused"}:
            raise ValueError("Only queued or paused campaigns can change exit node")
        if conn.execute(
            "SELECT 1 FROM campaign_targets WHERE campaign_id=? AND state='sending' LIMIT 1",
            (campaign_id,),
        ).fetchone():
            raise ValueError("Wait for the active chunk to finish before changing exit node")
        options = json.loads(row["run_options_json"] or "{}")
        options["exit_node_id"] = node_id
        conn.execute(
            "UPDATE campaigns SET safety_json=?, updated_at=? WHERE id=? AND operator=?",
            (json.dumps(options), _now(), campaign_id, operator),
        )
        conn.commit()
        return True


def get_campaign_source(campaign_id: int, operator: str) -> tuple[str, bytes] | None:
    with _LOCK:
        row = _conn().execute(
            """SELECT source_name, source_bytes FROM campaigns
                WHERE id=? AND operator=?
                  AND EXISTS (SELECT 1 FROM campaign_targets WHERE campaign_id=?)""",
            (campaign_id, operator, campaign_id),
        ).fetchone()
        return (row["source_name"] or "source.csv", row["source_bytes"] or b"") if row else None


def claim_campaign_target(target_id: int, batch_id: int, now_utc: str) -> bool:
    """Atomically claim one queued target for a queued campaign."""
    with _LOCK:
        conn = _conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """UPDATE campaign_targets SET state='sending', batch_id=?, claimed_at_utc=?
                     WHERE id=? AND state='queued'
                       AND available_at_utc<=?
                       AND EXISTS (SELECT 1 FROM campaigns c
                                    WHERE c.id=campaign_targets.campaign_id AND c.status='queued')""",
                (batch_id, now_utc, target_id, now_utc),
            )
            conn.commit()
            return cur.rowcount == 1
        except Exception:
            conn.rollback()
            raise


def release_campaign_target(target_id: int, batch_id: int) -> bool:
    """Undo a claim before browser action when a time or budget gate closes."""
    with _LOCK:
        conn = _conn()
        updated = conn.execute(
            """UPDATE campaign_targets
                  SET state='queued', batch_id=NULL, claimed_at_utc=NULL
                WHERE id=? AND batch_id=? AND state='sending' AND request_id IS NULL""",
            (target_id, batch_id),
        )
        conn.commit()
        return updated.rowcount == 1


def begin_immediate_attempt(batch_id: int, operator: str, linkedin_url: str) -> int:
    """Persist a possible browser action before an immediate live run acts."""
    key = normalize_url(linkedin_url)
    with _LOCK:
        conn = _conn()
        cur = conn.execute(
            """INSERT INTO outreach_uncertainties
                   (operator, normalized_linkedin_url, batch_id, detected_at)
               VALUES (?, ?, ?, ?)""",
            (operator, key, batch_id, _now()),
        )
        conn.commit()
        return int(cur.lastrowid)


def mark_inflight_targets_uncertain(batch_id: int | None = None) -> int:
    """Mark interrupted sends uncertain after process restart; never requeue them."""
    now = _now()
    with _LOCK:
        conn = _conn()
        conn.execute(
            """INSERT INTO outreach_uncertainties
                   (operator, normalized_linkedin_url, batch_id, target_id,
                    request_id, detected_at)
               SELECT operator, normalized_linkedin_url, batch_id, id,
                      request_id, COALESCE(claimed_at_utc, ?)
                 FROM campaign_targets
                WHERE state='sending' AND (? IS NULL OR batch_id=?)
                  AND NOT EXISTS (
                      SELECT 1 FROM outreach_uncertainties u WHERE u.target_id=campaign_targets.id)""",
            (now, batch_id, batch_id),
        )
        conn.execute(
            """UPDATE campaigns SET status='paused',
                   pause_reason='Interrupted browser action requires review', updated_at=?
                 WHERE status='queued' AND id IN (
                     SELECT campaign_id FROM campaign_targets
                      WHERE state='sending' AND (? IS NULL OR batch_id=?))""",
            (now, batch_id, batch_id),
        )
        cur = conn.execute(
            """UPDATE campaign_targets
                  SET state='uncertain', completed_at_utc=?,
                      detail=CASE WHEN COALESCE(detail, '')='' THEN 'Process restarted while send was in flight.'
                                  ELSE detail || char(10) || 'Process restarted while send was in flight.' END
                WHERE state='sending' AND (? IS NULL OR batch_id=?)""",
            (now, batch_id, batch_id),
        )
        if batch_id is None:
            conn.execute(
                """UPDATE batches SET status='interrupted', finished_at=?
                     WHERE status='running'""",
                (now,),
            )
        conn.commit()
        return cur.rowcount


def list_unresolved_outreach(operator: str) -> list[dict[str, Any]]:
    """Account-scoped browser outcomes needing a human check."""
    with _LOCK:
        rows = _conn().execute(
            """SELECT u.id, u.operator, u.normalized_linkedin_url AS linkedin_url,
                      u.detected_at, u.target_id, u.request_id,
                      COALESCE(ct.detail, r.detail, 'Browser action interrupted') AS detail,
                      c.name AS campaign_name
                 FROM outreach_uncertainties u
                 LEFT JOIN campaign_targets ct ON ct.id=u.target_id
                 LEFT JOIN campaigns c ON c.id=ct.campaign_id
                 LEFT JOIN outbound_requests r ON r.id=u.request_id
                 LEFT JOIN batches b ON b.id=u.batch_id
                WHERE u.operator=? AND u.verdict IS NULL
                  AND (u.target_id IS NOT NULL OR b.status<>'running')
                ORDER BY u.detected_at DESC, u.id DESC""",
            (operator,),
        ).fetchall()
        return [dict(row) for row in rows]


def review_outreach_uncertainty(
    uncertainty_id: int, operator: str, verdict: str, note: str,
) -> bool:
    """Record a human verdict; never silently requeue an uncertain target."""
    if verdict not in {"sent", "not_sent"} or not note.strip():
        raise ValueError("A sent or not_sent verdict and review note are required")
    with _LOCK:
        conn = _conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT u.id, u.target_id, u.normalized_linkedin_url
                     FROM outreach_uncertainties u
                     LEFT JOIN batches b ON b.id=u.batch_id
                    WHERE u.id=? AND u.operator=? AND u.verdict IS NULL
                      AND (u.target_id IS NOT NULL OR b.status<>'running')""",
                (uncertainty_id, operator),
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            if row["target_id"] is not None:
                updated = conn.execute(
                    """UPDATE campaign_targets SET state=?, completed_at_utc=?,
                           detail=COALESCE(detail, '') || char(10) || 'Reviewed: ' || ?
                        WHERE id=? AND state='uncertain'""",
                    ("sent" if verdict == "sent" else "failed", _now(), note.strip(), row["target_id"]),
                )
                if updated.rowcount != 1:
                    raise ValueError("Queued target is no longer uncertain")
            if verdict == "sent":
                now = _now()
                conn.execute(
                    """INSERT INTO account_contacts
                           (operator, linkedin_url, last_observed_status, degree,
                            last_action_type, first_seen_at, last_observed_at)
                       VALUES (?, ?, 'sent', '', 'manual_reconciled', ?, ?)
                       ON CONFLICT(operator, linkedin_url) DO UPDATE SET
                           last_observed_status='sent',
                           last_action_type='manual_reconciled',
                           last_observed_at=excluded.last_observed_at""",
                    (operator, row["normalized_linkedin_url"], now, now),
                )
                _upsert_profile_details(
                    conn, linkedin_url=row["normalized_linkedin_url"],
                    full_name="", first_name="", company_csv="", headline="", now=now,
                )
            conn.execute(
                """UPDATE outreach_uncertainties
                      SET verdict=?, reviewed_at=?, note=? WHERE id=?""",
                (verdict, _now(), note.strip(), uncertainty_id),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def reserve_campaign_day(campaign_id: int, local_date: str) -> bool:
    """Permit at most one scheduled chunk from a campaign on a local date."""
    with _LOCK:
        conn = _conn()
        updated = conn.execute(
            """UPDATE campaigns SET last_run_local_date=?, updated_at=?
                 WHERE id=? AND status='queued'
                   AND (last_run_local_date IS NULL OR last_run_local_date<>?)""",
            (local_date, _now(), campaign_id, local_date),
        )
        conn.commit()
        return updated.rowcount == 1


def release_campaign_day_if_unstarted(campaign_id: int, local_date: str, batch_id: int) -> bool:
    """Return a daily slot if the browser never reached any target in this run."""
    with _LOCK:
        conn = _conn()
        updated = conn.execute(
            """UPDATE campaigns SET last_run_local_date=NULL, updated_at=?
                 WHERE id=? AND last_run_local_date=?
                   AND NOT EXISTS (
                       SELECT 1 FROM campaign_targets
                        WHERE campaign_id=? AND batch_id=?)""",
            (_now(), campaign_id, local_date, campaign_id, batch_id),
        )
        conn.commit()
        return updated.rowcount == 1


def campaign_status(campaign_id: int) -> str | None:
    with _LOCK:
        row = _conn().execute("SELECT status FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        return row["status"] if row else None


def set_campaign_status(campaign_id: int, new_status: str, reason: str | None = None) -> bool:
    """Apply supported queue state transitions; cancellation only cancels queued targets."""
    transitions = {"queued": {"paused", "cancelled"}, "paused": {"queued", "cancelled"}}
    with _LOCK:
        conn = _conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if row is None:
                conn.rollback()
                return False
            old_status = row["status"]
            if new_status == old_status:
                conn.commit()
                return True
            if new_status not in transitions.get(old_status, set()):
                raise ValueError(f"Invalid campaign status transition: {old_status} -> {new_status}")
            conn.execute(
                "UPDATE campaigns SET status=?, pause_reason=?, updated_at=? WHERE id=?",
                (new_status, (reason or "Paused by operator") if new_status == "paused" else None,
                 _now(), campaign_id),
            )
            if new_status == "cancelled":
                conn.execute(
                    """UPDATE campaign_targets SET state='cancelled', completed_at_utc=?
                         WHERE campaign_id=? AND state='queued'""",
                    (_now(), campaign_id),
                )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def finish_campaign_if_drained(campaign_id: int) -> bool:
    """Close a queued campaign only after all its targets have terminal states."""
    with _LOCK:
        conn = _conn()
        updated = conn.execute(
            """UPDATE campaigns SET status='finished', updated_at=?
                 WHERE id=? AND status='queued'
                   AND NOT EXISTS (
                       SELECT 1 FROM campaign_targets
                        WHERE campaign_id=? AND state IN ('queued', 'sending')
                   )""",
            (_now(), campaign_id, campaign_id),
        )
        conn.commit()
        return updated.rowcount == 1
