from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import db

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/dashboard")
async def get_dashboard_metrics(operator: str):
    if operator not in {op["key"] for op in db.list_operators()}:
        raise HTTPException(400, "Unknown operator.")
    return db.dashboard_metrics(operator)
