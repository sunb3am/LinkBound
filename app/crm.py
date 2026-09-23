from __future__ import annotations

from typing import Any
from fastapi import APIRouter, HTTPException

from . import db
from .models import ContactNoteCreate, ContactTagCreate

router = APIRouter(prefix="/api/crm", tags=["crm"])

@router.get("/contacts/{linkedin_url_enc}/timeline")
async def contact_timeline(linkedin_url_enc: str, operator: str):
    """Get one sender's outbound history and shared person annotations."""
    url = db.normalize_url(linkedin_url_enc)
    if operator not in {op["key"] for op in db.list_operators()}:
        raise HTTPException(400, "Unknown operator.")
    events = db.list_contact_timeline(operator, url)
    return {
        "requests": [item for item in events if item["type"] == "outbound_request"],
        "tags": [item for item in events if item["type"] == "tag"],
        "notes": [item for item in events if item["type"] == "note"],
    }

@router.post("/contacts/{linkedin_url_enc}/tags")
async def add_tag(linkedin_url_enc: str, body: ContactTagCreate):
    url = db.normalize_url(linkedin_url_enc)
    now = db._now()
    with db._LOCK:
        conn = db._conn()
        conn.execute(
            "INSERT INTO contact_tags (contact_url, tag, created_at) VALUES (?, ?, ?)",
            (url, body.tag, now)
        )
        conn.commit()
    return {"ok": True, "tag": body.tag}

@router.delete("/contacts/{linkedin_url_enc}/tags/{tag_name}")
async def remove_tag(linkedin_url_enc: str, tag_name: str):
    url = db.normalize_url(linkedin_url_enc)
    with db._LOCK:
        conn = db._conn()
        conn.execute(
            "DELETE FROM contact_tags WHERE contact_url = ? AND tag = ?",
            (url, tag_name)
        )
        conn.commit()
    return {"ok": True}

@router.post("/contacts/{linkedin_url_enc}/notes")
async def add_note(linkedin_url_enc: str, body: ContactNoteCreate):
    url = db.normalize_url(linkedin_url_enc)
    now = db._now()
    with db._LOCK:
        conn = db._conn()
        cur = conn.execute(
            "INSERT INTO contact_notes (contact_url, note, created_at) VALUES (?, ?, ?)",
            (url, body.note, now)
        )
        note_id = cur.lastrowid
        row = conn.execute("SELECT * FROM contact_notes WHERE id=?", (note_id,)).fetchone()
        conn.commit()
    return dict(row)

@router.delete("/contacts/{linkedin_url_enc}/notes/{note_id}")
async def delete_note(linkedin_url_enc: str, note_id: int):
    with db._LOCK:
        conn = db._conn()
        conn.execute("DELETE FROM contact_notes WHERE id = ?", (note_id,))
        conn.commit()
    return {"ok": True}
