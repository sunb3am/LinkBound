import asyncio
import importlib

import httpx

from app import db


def test_private_mode_guards_dashboard_api_and_static(tmp_path, monkeypatch):
    db.close_db()
    monkeypatch.setenv("LINKBOUND_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LINKBOUND_REQUIRE_TAILSCALE_AUTH", "true")
    monkeypatch.setenv("LINKBOUND_TAILSCALE_ALLOWED_USERS", "owner@example.com")
    monkeypatch.setenv("LINKBOUND_ALLOW_LIVE_SENDS", "false")
    main = importlib.reload(importlib.import_module("app.main"))

    async def check():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for path in ("/", "/api/config", "/static/app.js", "/api/v1/health",
                         "/api/inbound/runs?operator=me",
                         "/api/inbound/conversations?operator=me",
                         "/api/inbound/export?operator=me"):
                assert (await client.get(path)).status_code == 403
                permitted = await client.get(
                    path, headers={"Tailscale-User-Login": "owner@example.com"}
                )
                assert permitted.status_code == 200

            blocked_send = await client.post(
                "/api/start",
                headers={"Tailscale-User-Login": "owner@example.com"},
                json={"upload_id": "irrelevant", "operator": "me", "dry_run": False},
            )
            assert blocked_send.status_code == 409
            assert blocked_send.json()["detail"] == "Live sends are disabled on this host."

    try:
        asyncio.run(check())
    finally:
        db.close_db()
