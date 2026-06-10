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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .models import SENT_STATUSES

_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None
_DB_PATH: Path | None = None


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
        _CONN.executescript(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                linkedin_url   TEXT PRIMARY KEY,
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

            CREATE INDEX IF NOT EXISTS idx_requests_batch ON outbound_requests(batch_id);
            CREATE INDEX IF NOT EXISTS idx_requests_created ON outbound_requests(created_at);
            """
        )
        # Migrate older rows / add enrichment columns.
        _ensure_column(_CONN, "contacts", "degree", "degree TEXT")
        _ensure_column(_CONN, "contacts", "last_action_type", "last_action_type TEXT")
        _ensure_column(_CONN, "contacts", "headline", "headline TEXT")
        _ensure_column(_CONN, "outbound_requests", "headline", "headline TEXT")
        _CONN.commit()


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
        cur = _conn().execute("SELECT * FROM contacts WHERE linkedin_url = ?", (key,))
        row = cur.fetchone()
        return dict(row) if row else None


def is_already_contacted(linkedin_url: str, contacted_statuses: set[str]) -> bool:
    contact = get_contact(linkedin_url)
    return bool(contact and contact.get("last_status") in contacted_statuses)


def upsert_contact(
    *,
    linkedin_url: str,
    full_name: str,
    first_name: str,
    company_csv: str,
    last_status: str,
    template_used: str,
    message_sent: str,
    operator: str,
    degree: str = "",
    last_action_type: str = "",
    headline: str = "",
) -> None:
    key = normalize_url(linkedin_url)
    now = _now()
    with _LOCK:
        conn = _conn()
        existing = conn.execute(
            "SELECT linkedin_url FROM contacts WHERE linkedin_url = ?", (key,)
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE contacts
                   SET full_name=?, first_name=?, company_csv=?, last_status=?,
                       template_used=?, message_sent=?, operator=?, degree=?,
                       last_action_type=?, headline=?, last_action_at=?
                 WHERE linkedin_url=?
                """,
                (full_name, first_name, company_csv, last_status, template_used,
                 message_sent, operator, degree, last_action_type, headline, now, key),
            )
        else:
            conn.execute(
                """
                INSERT INTO contacts
                    (linkedin_url, full_name, first_name, company_csv, last_status,
                     template_used, message_sent, operator, degree, last_action_type,
                     headline, first_seen_at, last_action_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (key, full_name, first_name, company_csv, last_status, template_used,
                 message_sent, operator, degree, last_action_type, headline, now, now),
            )
        conn.commit()


def list_contacts(search: str = "", limit: int = 500) -> list[dict[str, Any]]:
    with _LOCK:
        if search:
            like = f"%{search.lower()}%"
            cur = _conn().execute(
                """
                SELECT * FROM contacts
                 WHERE lower(full_name) LIKE ? OR lower(company_csv) LIKE ?
                       OR lower(linkedin_url) LIKE ?
                 ORDER BY last_action_at DESC LIMIT ?
                """,
                (like, like, like, limit),
            )
        else:
            cur = _conn().execute(
                "SELECT * FROM contacts ORDER BY last_action_at DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in cur.fetchall()]


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


def list_batches(limit: int = 50) -> list[dict[str, Any]]:
    with _LOCK:
        cur = _conn().execute("SELECT * FROM batches ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]


def get_batch(batch_id: int) -> dict[str, Any] | None:
    with _LOCK:
        row = _conn().execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        return dict(row) if row else None


# ---- outbound requests (audit trail) --------------------------------------

def add_request(
    *,
    batch_id: int,
    operator: str,
    linkedin_url: str,
    full_name: str,
    first_name: str,
    company_csv: str,
    role: str,
    email: str,
    action_requested: str,
    action_executed: str,
    template_id: int | None,
    template_name: str,
    message_rendered: str,
    status: str,
    detail: str = "",
    decision_trace: list[Any] | None = None,
    screenshot_path: str = "",
    headline: str = "",
) -> tuple[int, str]:
    now = _now()
    trace_json = json.dumps(decision_trace or [])
    with _LOCK:
        conn = _conn()
        cur = conn.execute(
            """
            INSERT INTO outbound_requests
                (batch_id, operator, linkedin_url, full_name, first_name, company_csv,
                 role, email, action_requested, action_executed, template_id,
                 template_name, message_rendered, status, detail, decision_trace,
                 screenshot_path, headline, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (batch_id, operator, normalize_url(linkedin_url), full_name, first_name,
             company_csv, role, email, action_requested, action_executed, template_id,
             template_name, message_rendered, status, detail, trace_json,
             screenshot_path, headline, now, now),
        )
        req_id = int(cur.lastrowid)
        public_id = f"OBR-{req_id:06d}"
        conn.execute("UPDATE outbound_requests SET public_id=? WHERE id=?", (public_id, req_id))
        conn.commit()
        return req_id, public_id


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
        cur = conn.execute("DELETE FROM operators WHERE key=?", (key,))
        conn.commit()
        return cur.rowcount > 0
