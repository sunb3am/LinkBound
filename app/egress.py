"""Fail-closed Tailscale exit-node selection for hosted LinkedIn browser work."""

from __future__ import annotations

import ipaddress
import json
import subprocess
import time
import urllib.request
from typing import Callable


class EgressError(RuntimeError):
    """The selected route cannot safely carry a LinkedIn browser task."""


def parse_exit_nodes(status: dict) -> list[dict]:
    """Return approved exit nodes without exposing unrelated tailnet peers."""
    peers = status.get("Peer") or {}
    nodes = []
    for peer in peers.values():
        if not peer.get("ExitNodeOption"):
            continue
        ips = peer.get("TailscaleIPs") or []
        nodes.append({
            "id": str(peer.get("ID") or ""),
            "name": str(peer.get("HostName") or "Unknown device"),
            "ip": next((ip for ip in ips if ":" not in ip), ""),
            "online": bool(peer.get("Online")),
        })
    return sorted((node for node in nodes if node["id"]), key=lambda item: item["name"].casefold())


def _run_json(*args: str) -> dict:
    try:
        result = subprocess.run(
            ["tailscale", *args], capture_output=True, text=True,
            check=True, timeout=10,
        )
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise EgressError(f"Tailscale status unavailable: {type(exc).__name__}") from exc


def available_exit_nodes() -> list[dict]:
    return parse_exit_nodes(_run_json("status", "--json"))


def _selected_node() -> str:
    return str(_run_json("debug", "prefs").get("ExitNodeID") or "")


def _switch_node(node_id: str) -> None:
    try:
        subprocess.run(
            ["tailscale", "set", f"--exit-node={node_id}"],
            capture_output=True, text=True, check=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EgressError(f"Cannot select Tailscale exit node: {type(exc).__name__}") from exc


def _public_ipv4() -> str:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(
            "https://api.ipify.org", headers={"Cache-Control": "no-store"}
        )
        with opener.open(request, timeout=8) as response:
            address = response.read(64).decode("ascii").strip()
        return str(ipaddress.IPv4Address(address))
    except (OSError, ValueError, UnicodeError) as exc:
        raise EgressError("Cannot verify public IP through the selected exit node") from exc


def _routed_through_tailscale() -> bool:
    try:
        result = subprocess.run(
            ["ip", "-4", "route", "get", "1.1.1.1"],
            capture_output=True, text=True, check=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EgressError("Cannot verify LinkBound's internet route") from exc
    return " dev tailscale0 " in f" {result.stdout.strip()} "


class ExitNodeController:
    """Own one route lease while the serialized browser runner is alive."""

    def __init__(
        self,
        *,
        status: Callable[[], dict] | None = None,
        selected: Callable[[], str] | None = None,
        switch: Callable[[str], None] | None = None,
        public_ip: Callable[[], str] | None = None,
        routed: Callable[[], bool] | None = None,
    ) -> None:
        self.status = status or (lambda: _run_json("status", "--json"))
        self.selected = selected or _selected_node
        self.switch = switch or _switch_node
        self.public_ip = public_ip or _public_ipv4
        self.routed = routed or _routed_through_tailscale
        self.active_node = ""
        self.observation: dict | None = None

    def begin(self, node_id: str) -> dict:
        if not node_id:
            raise EgressError("Select an exit node before LinkedIn browser work")
        nodes = {node["id"]: node for node in parse_exit_nodes(self.status())}
        node = nodes.get(node_id)
        if node is None:
            raise EgressError("Selected exit node is not approved or no longer exists")
        if not node["online"]:
            raise EgressError("Selected exit node is offline")
        self.switch(node_id)
        try:
            # `tailscale set` normally applies immediately. Allow a short status lag.
            for attempt in range(3):
                status = self.status()
                peers = status.get("Peer") or {}
                peer = next((p for p in peers.values() if p.get("ID") == node_id), None)
                if (self.selected() == node_id and peer and peer.get("Online")
                        and peer.get("ExitNode")):
                    if not self.routed():
                        raise EgressError("Internet route is not using the selected exit node")
                    address = self.public_ip()
                    if not address:
                        raise EgressError("Cannot verify public IP through the selected exit node")
                    self.active_node = node_id
                    self.observation = {
                        "node_id": node_id,
                        "node_name": node["name"],
                        "public_ip": address,
                    }
                    return dict(self.observation)
                if attempt < 2:
                    time.sleep(0.5)
            raise EgressError("Tailscale route did not select the requested exit node")
        except Exception:
            try:
                self.switch("")
            finally:
                self.active_node = ""
                self.observation = None
            raise

    def check(self) -> None:
        if not self.active_node:
            raise EgressError("No verified exit-node route is active")
        status = self.status()
        peers = status.get("Peer") or {}
        peer = next((p for p in peers.values() if p.get("ID") == self.active_node), None)
        if (self.selected() != self.active_node or not peer or not peer.get("Online")
                or not peer.get("ExitNode") or not self.routed()):
            raise EgressError("Selected exit-node route changed or became unavailable")

    def end(self) -> None:
        if not self.active_node:
            return
        try:
            self.switch("")
        finally:
            self.active_node = ""
            self.observation = None
