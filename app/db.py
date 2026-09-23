"""SQLite persistence: contact memory, batches, per-request audit trail, and the
template library.

Thread-safe via a module-level lock; the orchestrator runs on the event loop
while FastAPI handlers also read. Every access is guarded by ``_LOCK``.

Schema (v2):
  contacts            permanent dedup memory across all batches
  batches             one outbound run ("Jun 1 2026 - 2pm - 30 profiles")
  outbound_requests   one row per processed profile, with a unique public id and
                      a JSON decision_trace (the "thought trace")
  templates           the editable message-template library
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
        for result in results:
            result["has_successful_send"] = _has_successful_send(
                _conn(), result["linkedin_url"], result["operator"]
            )
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
    linkedin_url: str, contacted_statuses: set[str], operator: str
) -> bool:
    """Suppress any confirmed send globally, plus contacted observations on this account."""
    return should_suppress_contact(linkedin_url, contacted_statuses, operator)


def should_suppress_contact(
    linkedin_url: str, contacted_statuses: set[str], operator: str,
    *, global_suppression: bool = True, allow_override: bool = False,
) -> bool:
    """Policy hook for global confirmed-send suppression and future reviewed overrides."""
    key = normalize_url(linkedin_url)
    if not key:
        return False
    with _LOCK:
        conn = _conn()
        if not allow_override and global_suppression and _has_successful_send(conn, key):
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
    """Count successful sends by this operator since UTC midnight today."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    placeholders = ",".join("?" for _ in SENT_STATUSES)
    with _LOCK:
        cur = _conn().execute(
            f"""
            SELECT COUNT(*) AS c FROM outbound_requests
             WHERE status IN ({placeholders})
               AND substr(created_at, 1, 10) = ?
               AND operator = ?
            """,
            (*SENT_STATUSES, today, operator),
        )
        return int(cur.fetchone()["c"])


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
    headline: str = "", degree: str = "",
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

def delete_operator(key: str) -> bool:
    with _LOCK:
        conn = _conn()
        for table in ("batches", "outbound_requests", "account_contacts", "campaigns"):
            if conn.execute(
                f"SELECT 1 FROM {table} WHERE operator=? LIMIT 1", (key,)
            ).fetchone():
                raise ValueError("This account has history and cannot be deleted.")
        cur = conn.execute("DELETE FROM operators WHERE key=?", (key,))
        conn.commit()
        return cur.rowcount > 0
