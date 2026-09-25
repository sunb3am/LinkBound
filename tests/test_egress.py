"""Exit-node selection must fail before a LinkedIn browser starts."""

import pytest
import asyncio
import threading
from types import SimpleNamespace

from app.egress import EgressError, ExitNodeController, parse_exit_nodes, _routed_through_tailscale
from app.runner import LinkedInRunner
from app.orchestrator import Orchestrator
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


def test_tailscale_switch_uses_peer_ip_while_audit_keeps_node_id():
    selected = [""]
    switches = []

    def switch(identifier):
        switches.append(identifier)
        selected[0] = "node-a" if identifier == "100.101.102.103" else ""

    controller = ExitNodeController(
        status=lambda: _status(selected=selected[0] == "node-a"),
        selected=lambda: selected[0], switch=switch,
        public_ip=lambda: "198.51.100.42", routed=lambda: True,
    )
    observation = controller.begin("node-a")
    assert switches[0] == "100.101.102.103"
    assert observation["node_id"] == "node-a"
    controller.end()


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
        status=lambda: _status(selected=bool(switched and switched[-1] == "100.101.102.103")),
        selected=lambda: "node-a" if switched and switched[-1] == "100.101.102.103" else "",
        switch=switched.append,
        public_ip=lambda: "198.51.100.42",
        routed=lambda: True,
    )
    observation = controller.begin("node-a")
    assert observation == {"node_id": "node-a", "node_name": "home-laptop", "public_ip": "198.51.100.42"}
    controller.end()
    assert switched == ["100.101.102.103", ""]


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
    assert switched == ["100.101.102.103", ""]


def test_failed_preflight_cleanup_retains_route_lease():
    switched = []

    def switch(node_id):
        switched.append(node_id)
        raise EgressError("switch failed after route change")

    controller = ExitNodeController(
        status=lambda: _status(), selected=lambda: "", switch=switch,
        public_ip=lambda: "", routed=lambda: False,
    )
    with pytest.raises(EgressError, match="cleanup"):
        controller.begin("node-a")
    assert switched == ["100.101.102.103", ""]
    assert controller.active_node == "node-a"


def test_missing_public_ip_clears_route_and_blocks_task():
    switched = []
    controller = ExitNodeController(
        status=lambda: _status(selected=True),
        selected=lambda: "node-a" if switched and switched[-1] == "100.101.102.103" else "",
        switch=switched.append,
        public_ip=lambda: "",
        routed=lambda: True,
    )
    with pytest.raises(EgressError, match="public IP"):
        controller.begin("node-a")
    assert switched == ["100.101.102.103", ""]


def test_system_route_must_use_tailscale_interface():
    switched = []
    controller = ExitNodeController(
        status=lambda: _status(selected=True),
        selected=lambda: "node-a" if switched and switched[-1] == "100.101.102.103" else "",
        switch=switched.append,
        public_ip=lambda: "198.51.100.42",
        routed=lambda: False,
    )
    with pytest.raises(EgressError, match="route"):
        controller.begin("node-a")
    assert switched == ["100.101.102.103", ""]


def test_existing_route_is_cleared_before_new_browser_lease():
    current = ["old-node"]
    switched = []

    def switch(node_id):
        switched.append(node_id)
        current[0] = "node-a" if node_id == "100.101.102.103" else ""

    controller = ExitNodeController(
        status=lambda: _status(selected=current[0] == "node-a"),
        selected=lambda: current[0], switch=switch,
        public_ip=lambda: "198.51.100.42", routed=lambda: True,
    )
    controller.begin("node-a")
    assert switched[:2] == ["", "100.101.102.103"]


def test_ipv6_direct_route_is_not_accepted(monkeypatch):
    def fake_run(args, **_kwargs):
        route = "1.1.1.1 dev tailscale0 src 100.103.144.62" if "-4" in args else \
            "2606:4700:4700::1111 dev eth0 src 2600:3c01::1"
        return SimpleNamespace(stdout=route)

    monkeypatch.setattr("app.egress.subprocess.run", fake_run)
    assert _routed_through_tailscale() is False


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

        def end(self):
            pass

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


def test_failed_cancel_cleanup_sets_shared_fault(monkeypatch):
    launched = []

    class FailedController:
        def begin(self, _node_id):
            raise EgressError("switch failed")

        def end(self):
            raise EgressError("cleanup failed")

    monkeypatch.setattr("app.runner.ExitNodeController", FailedController)
    monkeypatch.setattr("app.runner.async_playwright", lambda: launched.append(True))
    settings = SimpleNamespace(browser=SimpleNamespace(), require_exit_node=True)
    runner = LinkedInRunner(settings, "me")
    runner.exit_node_id = "node-a"
    with pytest.raises(EgressError, match="cleanup failed"):
        asyncio.run(runner.start())
    assert settings.egress_cleanup_fault == "cleanup failed"
    assert runner._egress_controller is not None
    assert launched == []


def test_cancel_during_route_switch_waits_and_clears_before_return(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    cleared = threading.Event()
    launched = []

    class SlowController:
        def begin(self, _node_id):
            entered.set()
            assert release.wait(2)
            return {"node_id": "node-a", "node_name": "laptop", "public_ip": "198.51.100.42"}

        def end(self):
            cleared.set()

    monkeypatch.setattr("app.runner.ExitNodeController", SlowController)
    monkeypatch.setattr("app.runner.async_playwright", lambda: launched.append(True))
    settings = SimpleNamespace(browser=SimpleNamespace(), require_exit_node=True)
    runner = LinkedInRunner(settings, "me")
    runner.exit_node_id = "node-a"

    async def run():
        task = asyncio.create_task(runner.start())
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert cleared.is_set()
    assert launched == []


def test_watchdog_closes_browser_when_route_disappears():
    closed = []

    class LostController:
        def check(self):
            raise EgressError("Selected exit-node route changed")

    class Context:
        async def close(self):
            closed.append(True)

    settings = SimpleNamespace(browser=SimpleNamespace(), require_exit_node=True)
    runner = LinkedInRunner(settings, "me")
    runner._egress_controller = LostController()
    runner._context = Context()
    asyncio.run(runner._watch_egress(interval_seconds=0))
    assert closed == [True]
    with pytest.raises(EgressError, match="route changed"):
        asyncio.run(runner.verify_egress())


def test_runner_close_clears_active_route():
    closed = []
    settings = SimpleNamespace(browser=SimpleNamespace(), require_exit_node=True)
    runner = LinkedInRunner(settings, "me")
    runner._egress_controller = SimpleNamespace(end=lambda: closed.append(True))
    asyncio.run(runner.close())
    assert closed == [True]


def test_browser_close_failure_keeps_route_until_retry(monkeypatch):
    calls = []

    class Context:
        async def close(self):
            calls.append("browser")
            if calls.count("browser") == 1:
                raise RuntimeError("Chrome still open")

    settings = SimpleNamespace(browser=SimpleNamespace(), require_exit_node=True)
    runner = LinkedInRunner(settings, "me")
    runner._context = Context()
    runner._egress_controller = SimpleNamespace(end=lambda: calls.append("route"))
    with pytest.raises(EgressError, match="route was retained"):
        asyncio.run(runner.close())
    assert calls == ["browser"]
    assert settings.egress_cleanup_fault
    launched = []
    monkeypatch.setattr("app.runner.async_playwright", lambda: launched.append(True))
    another = LinkedInRunner(settings, "other-account")
    another.exit_node_id = "node-b"
    with pytest.raises(EgressError, match="cleanup failed"):
        asyncio.run(another.start())
    assert launched == []
    asyncio.run(runner.close())
    assert calls == ["browser", "browser", "route"]
    assert settings.egress_cleanup_fault == ""


def test_repeated_hard_stop_cannot_finish_before_browser_close():
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        class Runner:
            async def close(self):
                entered.set()
                await release.wait()

        task = asyncio.create_task(Orchestrator._close_runner_uninterruptibly(Runner()))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        await task

    asyncio.run(run())


def test_cancelled_inbound_close_waits_for_route_cleanup():
    entered = threading.Event()
    release = threading.Event()
    cleared = threading.Event()

    class SlowController:
        def end(self):
            entered.set()
            assert release.wait(2)
            cleared.set()

    settings = SimpleNamespace(browser=SimpleNamespace(), require_exit_node=True)
    runner = LinkedInRunner(settings, "me")
    runner._egress_controller = SlowController()

    async def run():
        task = asyncio.create_task(runner.close())
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
        release.set()
        await task

    asyncio.run(run())
    assert cleared.is_set()


def test_cancelled_browser_close_keeps_route_until_chrome_exits():
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        class Context:
            async def close(self):
                entered.set()
                await release.wait()
                calls.append("browser")

        settings = SimpleNamespace(browser=SimpleNamespace(), require_exit_node=True)
        runner = LinkedInRunner(settings, "me")
        runner._context = Context()
        runner._egress_controller = SimpleNamespace(end=lambda: calls.append("route"))
        task = asyncio.create_task(runner.close())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert calls == []
        release.set()
        await task
        assert calls == ["browser", "route"]

    asyncio.run(run())


def test_failed_route_cleanup_remains_retryable():
    calls = []
    current = ["node-a"]

    def switch(node_id):
        calls.append(node_id)
        if len(calls) == 1:
            raise EgressError("Cannot clear exit node")
        current[0] = ""

    controller = ExitNodeController(
        status=lambda: _status(selected=True), selected=lambda: current[0],
        switch=switch, public_ip=lambda: "198.51.100.42", routed=lambda: True,
    )
    controller.active_node = "node-a"
    controller.observation = {"node_id": "node-a"}
    with pytest.raises(EgressError, match="Cannot clear"):
        controller.end()
    assert controller.active_node == "node-a"
    controller.end()
    assert controller.active_node == ""
    assert calls == ["", ""]


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
        assert db._conn().execute("PRAGMA user_version").fetchone()[0] == 11
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
        db.record_batch_egress(batch_id, "node-a", "home-laptop", "198.51.100.42")
        batch = db.get_batch(batch_id)
        assert batch["exit_node_id"] == "node-a"
        assert batch["exit_node_name"] == "home-laptop"
        assert batch["egress_ipv4"] == "198.51.100.42"
    finally:
        db.close_db()


def test_v10_database_upgrades_node_name_columns(tmp_path):
    db.close_db()
    path = tmp_path / "upgrade.sqlite"
    try:
        db.init_db(path)
        conn = db._conn()
        conn.execute("ALTER TABLE batches DROP COLUMN exit_node_name")
        conn.execute("ALTER TABLE sync_runs DROP COLUMN exit_node_name")
        conn.execute("PRAGMA user_version = 10")
        conn.commit()
        db.close_db()
        db.init_db(path)
        assert db._conn().execute("PRAGMA user_version").fetchone()[0] == 11
        for table in ("batches", "sync_runs"):
            assert "exit_node_name" in {
                row[1] for row in db._conn().execute(f"PRAGMA table_info({table})")
            }
    finally:
        db.close_db()
