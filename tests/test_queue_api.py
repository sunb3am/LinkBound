import asyncio
from datetime import datetime, timedelta, timezone
import importlib

import httpx

from app import db
from app.models import QueueCampaignRequest


def test_queue_persists_sendable_rows_and_original_source_when_sends_are_off(tmp_path, monkeypatch):
    db.close_db()
    monkeypatch.setenv("LINKBOUND_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LINKBOUND_ALLOW_LIVE_SENDS", "false")
    monkeypatch.setenv("LINKBOUND_REQUIRE_TAILSCALE_AUTH", "false")
    main = importlib.reload(importlib.import_module("app.main"))
    start = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT09:00")
    main._UPLOADS["preview-1"] = {
        "operator": "me", "action": "connect_note", "csv_name": "source.csv",
        "source_bytes": b"original csv bytes",
        "jobs": [
            {"row_index": 0, "linkedin_url": "https://linkedin.com/in/ada", "precomputed_status": "queued"},
            {"row_index": 1, "linkedin_url": "https://linkedin.com/in/grace", "precomputed_status": "needs_attention", "issues": ["missing name"]},
            {"row_index": 2, "linkedin_url": "https://www.linkedin.com/in/ada/", "precomputed_status": "queued"},
        ],
    }
    req = QueueCampaignRequest(
        upload_id="preview-1", operator="me", name="Autumn recruiting",
        start_at_local=start, timezone="UTC", daily_chunk=25,
    )
    try:
        response = asyncio.run(main.queue_campaign(req))
        assert response["queued"] == 1
        assert response["excluded"] == 2
        assert response["live_sends_enabled"] is False
        targets = db.list_campaign_targets(response["campaign_id"])
        assert len(targets) == 1
        assert targets[0]["state"] == "queued"
        row = db._conn().execute("SELECT source_bytes, validation_json FROM campaigns WHERE id=?", (response["campaign_id"],)).fetchone()
        assert row["source_bytes"] == b"original csv bytes"
        assert "missing name" in row["validation_json"]
        assert "duplicate_in_upload" in row["validation_json"]

        async def check_routes():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                listing = await client.get("/api/queue", params={"operator": "me"})
                assert listing.status_code == 200
                assert listing.json()["campaigns"][0]["queued"] == 1
                other = await client.get(f"/api/queue/{response['campaign_id']}", params={"operator": "other"})
                assert other.status_code == 404
                source = await client.get(f"/api/queue/{response['campaign_id']}/source", params={"operator": "me"})
                assert source.content == b"original csv bytes"
                paused = await client.post(f"/api/queue/{response['campaign_id']}/pause", params={"operator": "me"})
                assert paused.json()["status"] == "paused"
                resumed = await client.post(f"/api/queue/{response['campaign_id']}/resume", params={"operator": "me"})
                assert resumed.json()["status"] == "queued"
                generic_delete = await client.delete(f"/api/campaigns/{response['campaign_id']}")
                assert generic_delete.status_code == 409
                cancelled = await client.post(f"/api/queue/{response['campaign_id']}/cancel", params={"operator": "me"})
                assert cancelled.json()["status"] == "cancelled"

        asyncio.run(check_routes())
    finally:
        db.close_db()
