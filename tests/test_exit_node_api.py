import asyncio
import importlib

import httpx

from app import db


def _request(app, method, path, body=None):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.request(method, path, json=body)
    return asyncio.run(run())


def test_exit_node_default_and_campaign_change_are_scoped(tmp_path, monkeypatch):
    db.close_db()
    monkeypatch.setenv("LINKBOUND_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LINKBOUND_REQUIRE_TAILSCALE_AUTH", "false")
    main = importlib.reload(importlib.import_module("app.main"))
    nodes = [
        {"id": "node-a", "name": "home-laptop", "ip": "100.101.102.103", "online": True},
        {"id": "node-b", "name": "phone", "ip": "100.101.102.104", "online": False},
    ]
    monkeypatch.setattr(main, "available_exit_nodes", lambda: nodes)
    try:
        db.init_db(tmp_path / "outbound.db")
        response = _request(main.app, "GET", "/api/exit-nodes")
        assert response.status_code == 200
        assert response.json()["default_node_id"] == ""
        assert response.json()["nodes"] == nodes

        assert _request(main.app, "PUT", "/api/exit-nodes/default",
                        {"node_id": "node-b"}).status_code == 409
        assert _request(main.app, "PUT", "/api/exit-nodes/default",
                        {"node_id": "node-a"}).status_code == 200
        assert db.get_default_exit_node_id() == "node-a"

        campaign_id = db.create_queued_campaign(
            "me", "Candidates", "connect_note", "UTC", 1,
            "2026-09-25T10:00:00+00:00",
            [{"job": {"linkedin_url": "https://linkedin.com/in/person"},
              "available_at_utc": "2026-09-25T10:00:00+00:00"}],
        )
        response = _request(main.app, "PUT", f"/api/queue/{campaign_id}/exit-node?operator=me",
                            {"node_id": "node-a"})
        assert response.status_code == 200
        assert db.get_queued_campaign(campaign_id, "me")["exit_node_id"] == "node-a"
        assert _request(main.app, "PUT", f"/api/queue/{campaign_id}/exit-node?operator=other",
                        {"node_id": "node-a"}).status_code == 404
    finally:
        db.close_db()
