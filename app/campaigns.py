from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import db
from .models import CampaignCreate, CampaignModel, CampaignUpdate

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])

@router.get("", response_model=dict[str, list[CampaignModel]])
async def list_campaigns():
    with db._LOCK:
        cur = db._conn().execute("SELECT * FROM campaigns ORDER BY id DESC")
        campaigns = [dict(r) for r in cur.fetchall()]
    return {"campaigns": campaigns}

@router.post("", response_model=CampaignModel)
async def create_campaign(body: CampaignCreate):
    now = db._now()
    with db._LOCK:
        conn = db._conn()
        cur = conn.execute(
            """
            INSERT INTO campaigns (name, goal, template_id, action, voice, operator,
                                   scheduling_json, safety_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (body.name, body.goal, body.template_id, body.action, body.voice,
             body.operator, body.scheduling_json, body.safety_json, now, now)
        )
        campaign_id = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        conn.commit()
    return dict(row)

@router.get("/{campaign_id}", response_model=CampaignModel)
async def get_campaign(campaign_id: int):
    with db._LOCK:
        row = db._conn().execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Campaign not found")
    return dict(row)

@router.put("/{campaign_id}", response_model=CampaignModel)
async def update_campaign(campaign_id: int, body: CampaignUpdate):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return await get_campaign(campaign_id)
        
    fields["updated_at"] = db._now()
    cols = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [campaign_id]
    
    with db._LOCK:
        conn = db._conn()
        cur = conn.execute(f"UPDATE campaigns SET {cols} WHERE id=?", values)
        if cur.rowcount == 0:
            raise HTTPException(404, "Campaign not found")
        row = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        conn.commit()
    return dict(row)

@router.delete("/{campaign_id}")
async def delete_campaign(campaign_id: int):
    with db._LOCK:
        conn = db._conn()
        cur = conn.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Campaign not found")
        conn.commit()
    return {"ok": True}
