from __future__ import annotations

from datetime import datetime, timedelta, timezone
from fastapi import APIRouter

from . import db

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

@router.get("/dashboard")
async def get_dashboard_metrics():
    with db._LOCK:
        # Total contacted (unique profiles sent to)
        cur = db._conn().execute("SELECT COUNT(DISTINCT linkedin_url) as c FROM outbound_requests WHERE status IN ('sent', 'message_sent', 'inmail_sent')")
        total_contacted = cur.fetchone()["c"]
        
        # Active campaigns
        cur = db._conn().execute("SELECT COUNT(*) as c FROM campaigns WHERE status = 'active'")
        active_campaigns = cur.fetchone()["c"]
        
        # Sent today
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cur = db._conn().execute("SELECT COUNT(*) as c FROM outbound_requests WHERE status IN ('sent', 'message_sent', 'inmail_sent') AND substr(created_at, 1, 10) = ?", (today,))
        sent_today = cur.fetchone()["c"]
        
        # Sends over the last 30 days
        thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        cur = db._conn().execute("""
            SELECT substr(created_at, 1, 10) as date, COUNT(*) as count 
            FROM outbound_requests 
            WHERE status IN ('sent', 'message_sent', 'inmail_sent') 
            AND created_at >= ?
            GROUP BY date ORDER BY date ASC
        """, (thirty_days_ago,))
        sends_over_time = [dict(r) for r in cur.fetchall()]
        
        # Status breakdown
        cur = db._conn().execute("SELECT status, COUNT(*) as count FROM outbound_requests GROUP BY status")
        status_breakdown = [dict(r) for r in cur.fetchall()]
        
        # Template performance
        cur = db._conn().execute("""
            SELECT template_name, COUNT(*) as count 
            FROM outbound_requests 
            WHERE status IN ('sent', 'message_sent', 'inmail_sent') 
            GROUP BY template_name 
            ORDER BY count DESC LIMIT 10
        """)
        template_performance = [dict(r) for r in cur.fetchall()]

    return {
        "total_contacted": total_contacted,
        "active_campaigns": active_campaigns,
        "sent_today": sent_today,
        "sends_over_time": sends_over_time,
        "status_breakdown": status_breakdown,
        "template_performance": template_performance,
    }
