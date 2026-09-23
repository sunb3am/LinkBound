import asyncio

import pytest

from app.coordinator import RunCoordinator


class FakeOrchestrator:
    def __init__(self, settings):
        self.operator = ""
        self.busy = False
        self._subscribers = set()
        self.resolve_gate = asyncio.Event()
        self.gemini = None

    def is_busy(self):
        return self.busy

    async def start(self, jobs, operator, **kwargs):
        self.operator = operator
        self.busy = True

    async def resolve_names(self, jobs, operator, *, mode, gemini):
        self.operator = operator
        self.busy = True
        try:
            await self.resolve_gate.wait()
            return jobs
        finally:
            self.busy = False

    def snapshot(self):
        return {"state": "running" if self.busy else "idle", "operator": self.operator}

    def unsubscribe(self, queue):
        self._subscribers.discard(queue)


def test_one_coordinator_serializes_runs_and_page_resolution_across_accounts():
    asyncio.run(_exercise_coordinator())


async def _exercise_coordinator():
    coordinator = RunCoordinator(object(), object(), factory=FakeOrchestrator)
    first = await coordinator.start("sender-a", [], action="connect")

    with pytest.raises(RuntimeError, match="already in progress"):
        await coordinator.start("sender-b", [], action="connect")
    with pytest.raises(RuntimeError, match="already in progress"):
        await coordinator.resolve_names("sender-b", [], mode="page")

    first.busy = False
    resolving = asyncio.create_task(coordinator.resolve_names("sender-b", [{}], mode="page"))
    await asyncio.sleep(0)
    assert coordinator.snapshot()["operator"] == "sender-b"
    with pytest.raises(RuntimeError, match="already in progress"):
        await coordinator.start("sender-a", [], action="connect")
    coordinator.get("sender-b").resolve_gate.set()
    assert await resolving == [{}]
