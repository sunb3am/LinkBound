"""Own the single browser operation allowed by the one-process LinkBound app."""

from __future__ import annotations

import asyncio
from typing import Any

from .orchestrator import Orchestrator


class RunCoordinator:
    def __init__(self, settings: Any, gemini: Any, factory=Orchestrator):
        self.settings = settings
        self.gemini = gemini
        self.factory = factory
        self.orchestrators: dict[str, Orchestrator] = {}
        self._subscribers: set[asyncio.Queue] = set()

    def get(self, operator: str) -> Orchestrator:
        if operator not in self.orchestrators:
            orch = self.factory(self.settings)
            orch.gemini = self.gemini
            for queue in self._subscribers:
                orch._subscribers.add(queue)
            self.orchestrators[operator] = orch
        return self.orchestrators[operator]

    def active(self) -> Orchestrator | None:
        return next((o for o in self.orchestrators.values() if o.is_busy()), None)

    async def start(self, operator: str, jobs: list[dict], **kwargs: Any) -> Orchestrator:
        if not kwargs.get("dry_run", False) and not self.settings.allow_live_sends:
            raise RuntimeError("Live sends are disabled on this host.")
        if self.active() is not None:
            raise RuntimeError("A browser operation is already in progress.")
        orch = self.get(operator)
        await orch.start(jobs, operator, **kwargs)
        return orch

    async def resolve_names(self, operator: str, jobs: list[dict], *, mode: str) -> list[dict]:
        if self.active() is not None:
            raise RuntimeError("A browser operation is already in progress.")
        return await self.get(operator).resolve_names(
            jobs, operator, mode=mode, gemini=self.gemini
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
        if operator:
            return self.get(operator).snapshot()
        active = self.active()
        if active:
            return active.snapshot()
        if self.orchestrators:
            return next(iter(self.orchestrators.values())).snapshot()
        return {"state": "idle", "totals": {}, "current": {}}
