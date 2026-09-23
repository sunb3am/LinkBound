import asyncio
import importlib
from types import SimpleNamespace

import httpx

from app import db
from app.models import RunState


def test_routes_scope_history_and_share_start_coordinator(tmp_path, monkeypatch):
    db.close_db()
    monkeypatch.setenv("LINKBOUND_DATA_DIR", str(tmp_path))
    main = importlib.reload(importlib.import_module("app.main"))
    db.create_operator("sender-b", "Sender B", "profiles/sender-b")
    batch_a, _ = db.create_batch("me", "A", "connect", False, 1)
    batch_b, _ = db.create_batch("sender-b", "B", "connect", False, 1)
    for batch_id, operator in ((batch_a, "me"), (batch_b, "sender-b")):
        db.record_outcome(
            batch_id=batch_id, operator=operator,
            linkedin_url=f"linkedin.com/in/{operator}", full_name=operator,
            first_name=operator, company_csv="", role="", email="",
            action_requested="connect", action_executed="connect",
            template_id=None, template_name="", message_rendered="",
            status="sent",
        )

    calls = []

    class SpyCoordinator:
        async def start(self, operator, jobs, **kwargs):
            calls.append((operator, jobs))
            return SimpleNamespace(batch_id=42, batch_public_id="B0042", state=RunState.RUNNING)

    monkeypatch.setattr(main, "manager", SpyCoordinator())
    monkeypatch.setattr(main.settings.api, "require_key", False)
    main._UPLOADS["preview-a"] = {"operator": "me", "jobs": [{"precomputed_status": "queued"}]}

    async def check():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            contacts = await client.get("/api/contacts", params={"operator": "me"})
            assert contacts.status_code == 200
            assert {row["operator"] for row in contacts.json()["contacts"]} == {"me"}
            assert {row["linkedin_url"] for row in contacts.json()["contacts"]} == {
                "https://linkedin.com/in/me"
            }
            assert (await client.get("/api/contacts")).status_code == 422

            batches = await client.get("/api/batches", params={"operator": "sender-b"})
            assert [row["id"] for row in batches.json()["batches"]] == [batch_b]
            assert (await client.get(f"/api/batches/{batch_a}", params={"operator": "sender-b"})).status_code == 404

            analytics = await client.get("/api/analytics/dashboard", params={"operator": "me"})
            assert analytics.json()["total_contacted"] == 1
            assert analytics.json()["sent_today"] == 1

            dashboard = await client.post("/api/start", json={
                "upload_id": "preview-a", "operator": "me", "dry_run": True,
            })
            assert dashboard.status_code == 200
            programmatic = await client.post("/api/v1/enqueue", json={
                "operator": "me", "profiles": [{"linkedin_url": "linkedin.com/in/another"}],
                "action": "connect", "dry_run": True,
            })
            assert programmatic.status_code == 200
            assert programmatic.json()["batch_id"] == 42
            assert [item[0] for item in calls] == ["me", "me"]

    try:
        asyncio.run(check())
    finally:
        main._UPLOADS.pop("preview-a", None)
        db.close_db()
