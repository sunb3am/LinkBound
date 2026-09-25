"""Exit-node selection must fail before a LinkedIn browser starts."""

import pytest
import asyncio
from types import SimpleNamespace

from app.egress import EgressError, ExitNodeController, parse_exit_nodes
from app.runner import LinkedInRunner
from app import db


def _status(*, online=True, selected=False):
    return {
        "Peer": {
            "node-a": {
                "ID": "node-a",
                "HostName": "home-laptop",
                "TailscaleIPs": ["100.101.102.103"],
                "ExitNodeOption": True,
                "ExitNode": selected,
                "Online": online,
            },
            "node-b": {
                "ID": "node-b",
                "HostName": "other-vps",
                "TailscaleIPs": ["100.103.104.105"],
                "ExitNodeOption": False,
                "Online": True,
            },
        }
    }


def test_inventory_excludes_non_exit_nodes():
    assert parse_exit_nodes(_status()) == [
        {"id": "node-a", "name": "home-laptop", "ip": "100.101.102.103", "online": True}
    ]


def test_offline_node_never_switches():
    switched = []
    controller = ExitNodeController(
        status=lambda: _status(online=False),
        selected=lambda: "",
        switch=switched.append,
        public_ip=lambda: "203.0.113.7",
    )
    with pytest.raises(EgressError, match="offline"):
        controller.begin("node-a")
    assert switched == []


def test_selected_node_and_public_ip_are_verified():
    switched = []
    controller = ExitNodeController(
        status=lambda: _status(selected=bool(switched and switched[-1] == "node-a")),
        selected=lambda: switched[-1] if switched else "",
        switch=switched.append,
        public_ip=lambda: "198.51.100.42",
        routed=lambda: True,
    )
    observation = controller.begin("node-a")
    assert observation == {"node_id": "node-a", "node_name": "home-laptop", "public_ip": "198.51.100.42"}
    controller.end()
    assert switched == ["node-a", ""]


def test_route_mismatch_clears_route_and_blocks_task():
    switched = []
    controller = ExitNodeController(
        status=lambda: _status(),
        selected=lambda: "",
        switch=switched.append,
        public_ip=lambda: "198.51.100.42",
        routed=lambda: False,
    )
    with pytest.raises(EgressError, match="route"):
        controller.begin("node-a")
    assert switched == ["node-a", ""]


def test_missing_public_ip_clears_route_and_blocks_task():
    switched = []
    controller = ExitNodeController(
        status=lambda: _status(selected=True),
        selected=lambda: "node-a",
        switch=switched.append,
        public_ip=lambda: "",
        routed=lambda: True,
    )
    with pytest.raises(EgressError, match="public IP"):
        controller.begin("node-a")
    assert switched == ["node-a", ""]


def test_system_route_must_use_tailscale_interface():
    switched = []
    controller = ExitNodeController(
        status=lambda: _status(selected=True),
        selected=lambda: "node-a",
        switch=switched.append,
        public_ip=lambda: "198.51.100.42",
        routed=lambda: False,
    )
    with pytest.raises(EgressError, match="route"):
        controller.begin("node-a")
    assert switched == ["node-a", ""]


def test_hosted_runner_refuses_to_launch_without_node(monkeypatch):
    launched = []
    monkeypatch.setattr("app.runner.async_playwright", lambda: launched.append(True))
    monkeypatch.setattr("app.runner.db.get_default_exit_node_id", lambda: "")
    settings = SimpleNamespace(browser=SimpleNamespace(), require_exit_node=True)
    runner = LinkedInRunner(settings, "me")
    with pytest.raises(EgressError, match="Select an exit node"):
        asyncio.run(runner.start())
    assert launched == []


def test_offline_override_blocks_before_playwright(monkeypatch):
    launched = []
    requested = []

    class OfflineController:
        def begin(self, node_id):
            requested.append(node_id)
            raise EgressError("Selected exit node is offline")

    monkeypatch.setattr("app.runner.async_playwright", lambda: launched.append(True))
    monkeypatch.setattr("app.runner.ExitNodeController", OfflineController)
    monkeypatch.setattr("app.runner.db.get_default_exit_node_id", lambda: "default-node")
    settings = SimpleNamespace(browser=SimpleNamespace(), require_exit_node=True)
    runner = LinkedInRunner(settings, "me")
    runner.exit_node_id = "task-node"
    with pytest.raises(EgressError, match="offline"):
        asyncio.run(runner.start())
    assert requested == ["task-node"]
    assert launched == []


def test_runner_close_clears_active_route():
    closed = []
    settings = SimpleNamespace(browser=SimpleNamespace(), require_exit_node=True)
    runner = LinkedInRunner(settings, "me")
    runner._egress_controller = SimpleNamespace(end=lambda: closed.append(True))
    asyncio.run(runner.close())
    assert closed == [True]


def test_default_exit_node_survives_database_restart(tmp_path):
    db.close_db()
    path = tmp_path / "egress.sqlite"
    try:
        db.init_db(path)
        assert db.get_default_exit_node_id() == ""
        db.set_default_exit_node_id("node-a")
        db.close_db()
        db.init_db(path)
        assert db.get_default_exit_node_id() == "node-a"
        assert db._conn().execute("PRAGMA user_version").fetchone()[0] == 10
    finally:
        db.close_db()


def test_queued_campaign_exit_node_can_change_before_next_chunk(tmp_path):
    db.close_db()
    try:
        db.init_db(tmp_path / "campaign.sqlite")
        campaign_id = db.create_queued_campaign(
            "me", "Candidate outreach", "connect_note", "UTC", 1,
            "2026-09-25T10:00:00+00:00",
            [{"job": {"linkedin_url": "https://linkedin.com/in/person"},
              "available_at_utc": "2026-09-25T10:00:00+00:00"}],
        )
        assert db.set_campaign_exit_node_id(campaign_id, "me", "node-a")
        assert db.get_queued_campaign(campaign_id, "me")["exit_node_id"] == "node-a"
        assert db.set_campaign_exit_node_id(campaign_id, "me", "node-b")
        assert db.list_queued_campaigns("me")[0]["exit_node_id"] == "node-b"
    finally:
        db.close_db()


def test_batch_records_actual_egress_after_browser_preflight(tmp_path):
    db.close_db()
    try:
        db.init_db(tmp_path / "audit.sqlite")
        batch_id, _ = db.create_batch("me", "Pilot", "connect", True, 1)
        db.record_batch_egress(batch_id, "node-a", "198.51.100.42")
        batch = db.get_batch(batch_id)
        assert batch["exit_node_id"] == "node-a"
        assert batch["egress_ipv4"] == "198.51.100.42"
    finally:
        db.close_db()
