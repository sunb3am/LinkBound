"""Private, account-scoped inbound CRM routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import db, inbound_export, inbound_store
from .settings import load_settings

router = APIRouter(prefix="/api/inbound", tags=["inbound"])


def _account(operator: str) -> str:
    if operator not in {row["key"] for row in db.list_operators()}:
        raise HTTPException(400, "Unknown operator")
    return operator


class LinkContact(BaseModel):
    contact_url: str


@router.get("/runs")
async def runs(operator: str, limit: int = 20):
    return {"runs": inbound_store.list_sync_runs(_account(operator), limit)}


@router.get("/conversations")
async def conversations(operator: str, limit: int = 100, offset: int = 0):
    account = _account(operator)
    return {
        "conversations": inbound_store.list_conversations(account, limit, offset),
        "total": inbound_store.count_conversations(account),
    }


@router.get("/conversations/{conversation_id}")
async def conversation(conversation_id: int, operator: str):
    row = inbound_store.get_conversation(_account(operator), conversation_id)
    if row is None:
        raise HTTPException(404, "Conversation not found on this account")
    return {"conversation": row}


@router.post("/conversations/{conversation_id}/review")
async def review_conversation(conversation_id: int, operator: str):
    if not inbound_store.mark_reviewed(_account(operator), conversation_id):
        raise HTTPException(404, "Conversation not found on this account")
    return {"ok": True}


@router.post("/conversations/{conversation_id}/link")
async def link_conversation(conversation_id: int, body: LinkContact, operator: str):
    try:
        linked = inbound_store.link_contact(_account(operator), conversation_id, body.contact_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not linked:
        raise HTTPException(404, "Conversation not found on this account")
    return {"ok": True}


@router.get("/files/{attachment_id}")
async def download_file(attachment_id: int, operator: str):
    try:
        result = inbound_store.attachment_file(
            _account(operator), attachment_id, load_settings().data_dir
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "File not found on this account")
    path, record = result
    return FileResponse(path, filename=record["filename"] or "attachment",
                        media_type="application/octet-stream")


@router.get("/export")
async def export_inbound(operator: str):
    account = _account(operator)
    try:
        path = await run_in_threadpool(
            inbound_export.build_export, account, load_settings().data_dir
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return FileResponse(
        path, filename=f"linkbound-inbound-{account}.zip",
        media_type="application/zip",
        background=BackgroundTask(lambda: path.unlink(missing_ok=True)),
    )
