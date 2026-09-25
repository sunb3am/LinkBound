"""Own the single browser operation allowed by the one-process LinkBound app."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .orchestrator import Orchestrator


@dataclass
class _InboundActivity:
    operator: str
    started_at: str

    def snapshot(self) -> dict:
        return {"state": "syncing", "operator": self.operator,
                "started_at": self.started_at, "totals": {}, "current": {}}


class RunCoordinator:
    def __init__(self, settings: Any, gemini: Any, factory=Orchestrator):
        self.settings = settings
        self.gemini = gemini
        self.factory = factory
        self.orchestrators: dict[str, Orchestrator] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self._inbound_activity: _InboundActivity | None = None

    def get(self, operator: str) -> Orchestrator:
        if operator not in self.orchestrators:
            orch = self.factory(self.settings)
            orch.gemini = self.gemini
            for queue in self._subscribers:
                orch._subscribers.add(queue)
            self.orchestrators[operator] = orch
        return self.orchestrators[operator]

    def active(self) -> Orchestrator | _InboundActivity | None:
        return self._inbound_activity or next(
            (o for o in self.orchestrators.values() if o.is_busy()), None
        )

    def _assert_egress_healthy(self) -> None:
        if getattr(self.settings, "require_exit_node", False) and getattr(
            self.settings, "egress_cleanup_fault", ""
        ):
            raise RuntimeError("Exit-node cleanup failed; inspect and restart LinkBound before browser work")

    async def run_inbound(self, operator: str, operation) -> Any:
        """Give one no-send collector exclusive ownership of the browser profile."""
        self._assert_egress_healthy()
        if self.active() is not None:
            raise RuntimeError("A browser operation is already in progress.")
        self._inbound_activity = _InboundActivity(
            operator=operator, started_at=datetime.now(timezone.utc).isoformat()
        )
        try:
            return await operation()
        finally:
            self._inbound_activity = None

    async def start(self, operator: str, jobs: list[dict], **kwargs: Any) -> Orchestrator:
        self._assert_egress_healthy()
        if not kwargs.get("dry_run", False) and not self.settings.allow_live_sends:
            raise RuntimeError("Live sends are disabled on this host.")
        if self.active() is not None:
            raise RuntimeError("A browser operation is already in progress.")
        orch = self.get(operator)
        await orch.start(jobs, operator, **kwargs)
        return orch

    async def resolve_names(self, operator: str, jobs: list[dict], *, mode: str,
                            exit_node_id: str = "") -> list[dict]:
        if mode == "page":
            self._assert_egress_healthy()
        if self.active() is not None:
            raise RuntimeError("A browser operation is already in progress.")
        return await self.get(operator).resolve_names(
            jobs, operator, mode=mode, gemini=self.gemini,
            exit_node_id=exit_node_id,
        )

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=400)
        self._subscribers.add(queue)
        for orch in self.orchestrators.values():
            orch._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)
        for orch in self.orchestrators.values():
            orch.unsubscribe(queue)

    def snapshot(self, operator: str | None = None) -> dict:
        if self._inbound_activity and (operator is None or operator == self._inbound_activity.operator):
            return self._inbound_activity.snapshot()
        if operator:
            return self.get(operator).snapshot()
        active = self.active()
        if active:
            return active.snapshot()
        if self.orchestrators:
            return next(iter(self.orchestrators.values())).snapshot()
        return {"state": "idle", "totals": {}, "current": {}}
