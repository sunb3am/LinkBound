"""Async orchestrator: runs one batch at a time with pause/resume/stop and live
progress broadcasting over WebSocket.

Each processed profile produces an audited outbound_request row (with its own
public id and a decision trace) and a live "item" event carrying that trace.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import urllib.request
from datetime import datetime
from typing import Any

from . import db
from .models import (
    ActionType,
    ItemStatus,
    RunState,
    SENT_STATUSES,
)
from .names import slug_from_url, split_full_name
from .runner import LinkedInRunner, ProfileResult
from .safety import SafetyGovernor
from .settings import Settings
from .templating import referenced_variables, render

_NAME_VARS = {"first_name", "last_name", "full_name"}

_SKIPPED_STATUSES = {
    ItemStatus.ALREADY_CONNECTED,
    ItemStatus.PENDING_EXISTING,
    ItemStatus.SKIPPED_DEDUP,
    ItemStatus.NEEDS_ATTENTION,
    ItemStatus.OUT_OF_NETWORK,
    ItemStatus.CONNECT_UNAVAILABLE,
}


class Orchestrator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.state: RunState = RunState.IDLE
        self.operator: str = ""
        self.batch_id: int | None = None
        self.batch_public_id: str = ""
        self.batch_name: str = ""
        self.action: str = ActionType.AUTO.value
        self.dry_run: bool = False
        self.ai_personalize: bool = False
        self.ai_voice: str = "auto"
        self.webhook_url: str = ""
        self.gemini = None  # set by main after construction
        self.message: str = ""

        self._task: asyncio.Task | None = None
        self._runner: LinkedInRunner | None = None
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._stop_requested = False
        self._hard_stop = False
        self.resolving = False  # a name-resolution pre-pass is in progress

        self._subscribers: set[asyncio.Queue] = set()
        self._last_snapshot: dict[str, Any] = {}

        self.totals = {
            "total": 0, "sent": 0, "skipped": 0, "failed": 0,
            "flagged": 0, "dry": 0, "done": 0,
        }
        self.current: dict[str, Any] = {}

    # ---- subscription / broadcasting -------------------------------------

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=400)
        self._subscribers.add(q)
        if self._last_snapshot:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(self._last_snapshot)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _broadcast(self, event: dict[str, Any]) -> None:
        event = {**event, "ts": datetime.now().isoformat()}
        if event.get("type") in {"snapshot", "state"}:
            self._last_snapshot = event
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def snapshot(self) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "state": self.state.value,
            "operator": self.operator,
            "batch_id": self.batch_id,
            "batch_public_id": self.batch_public_id,
            "batch_name": self.batch_name,
            "action": self.action,
            "dry_run": self.dry_run,
            "ai_personalize": self.ai_personalize,
            "message": self.message,
            "totals": dict(self.totals),
            "current": dict(self.current),
        }

    def _emit_state(self, message: str = "") -> None:
        self.message = message
        self._broadcast(self.snapshot() | {"type": "state"})

    # ---- controls ---------------------------------------------------------

    def is_busy(self) -> bool:
        return self.resolving or self.state in {
            RunState.RUNNING, RunState.PAUSED, RunState.WAITING_LOGIN
        }

    # ---- name resolution pre-pass ----------------------------------------

    async def resolve_names(self, jobs: list[dict], operator: str, *, mode: str, gemini) -> list[dict]:
        """Fill accurate names into jobs (in place) and return updated row views.

        mode="page": visit each profile with the operator's session and read the
        real name (most accurate); falls back to AI slug cleanup if the page name
        is unavailable. mode="ai": no browser, just AI slug cleanup (fast).
        Only rows whose name was not operator-provided (name_source != "csv") are
        touched.
        """
        if self.is_busy():
            raise RuntimeError("Busy: a run or resolve is already in progress.")
        targets = [j for j in jobs if j.get("linkedin_url") and j.get("name_source") != "csv"]
        updated: list[dict] = []
        self.resolving = True
        try:
            self._broadcast({"type": "resolve_start", "mode": mode, "total": len(targets)})
            if mode == "ai":
                for i, job in enumerate(targets):
                    slug = slug_from_url(job["linkedin_url"])
                    try:
                        first, last, conf = gemini.cleanup_name(slug)
                    except Exception:  # noqa: BLE001
                        first = last = ""; conf = False
                    if conf and first:
                        self._apply_name(job, first, last, source="ai")
                        updated.append(self._row_view(job))
                    self._broadcast({"type": "resolve_progress", "done": i + 1, "total": len(targets)})
            else:
                runner = LinkedInRunner(self.settings, operator)
                await runner.start()
                await runner.open_feed()
                waited = 0
                while not await runner.logged_in_now() and waited < 150:
                    self._broadcast({"type": "resolve_login_wait"})
                    await asyncio.sleep(3); waited += 3
                if not await runner.logged_in_now():
                    await runner.close()
                    raise RuntimeError("Not logged into LinkedIn; cannot resolve from profiles.")
                try:
                    for i, job in enumerate(targets):
                        info = await runner.resolve_profile(job["linkedin_url"])
                        name = info.get("name", "")
                        headline = info.get("headline", "")
                        if not name and gemini and gemini.available:
                            try:
                                f, l, c = gemini.cleanup_name(slug_from_url(job["linkedin_url"]),
                                                              headline=headline)
                                if c:
                                    name = f"{f} {l}".strip()
                            except Exception:  # noqa: BLE001
                                pass
                        if name:
                            f, l = split_full_name(name)
                            self._apply_name(job, f, l, source="page", headline=headline)
                            updated.append(self._row_view(job))
                        self._broadcast({"type": "resolve_progress", "done": i + 1,
                                         "total": len(targets), "name": name})
                finally:
                    await runner.close()
            self._broadcast({"type": "resolve_done", "updated": len(updated)})
            return updated
        finally:
            self.resolving = False

    def _apply_name(self, job: dict, first: str, last: str, *, source: str, headline: str = "") -> None:
        job["first_name"] = first
        job["last_name"] = last
        job["full_name"] = f"{first} {last}".strip()
        job["name_source"] = source
        variables = dict(job.get("variables") or {})
        variables["first_name"] = first
        variables["last_name"] = last
        variables["full_name"] = job["full_name"]
        job["variables"] = variables
        if headline:
            job["headline"] = headline
        body = job.get("template_body") or ""
        if body:
            rendered, _issues = render(body, variables, note_limit=None)
            job["message"] = rendered

    @staticmethod
    def _row_view(job: dict) -> dict:
        return {
            "row_index": job["row_index"],
            "first_name": job["first_name"],
            "last_name": job["last_name"],
            "full_name": job["full_name"],
            "message": job["message"],
            "name_source": job["name_source"],
            "char_count": len(job["message"]),
            "headline": job.get("headline", ""),
        }

    async def start(
        self,
        jobs: list[dict],
        operator: str,
        *,
        action: str,
        dry_run: bool,
        batch_name: str,
        send_on_mismatch: bool,
        ai_personalize: bool = False,
        ai_voice: str = "auto",
        webhook_url: str = "",
        custom_gemini_key: str | None = None,
        custom_gemini_model: str | None = None,
    ) -> None:
        if self.is_busy():
            raise RuntimeError("A run is already in progress.")
        self.operator = operator
        self.action = action
        self.dry_run = dry_run
        self.ai_personalize = ai_personalize
        self.ai_voice = ai_voice
        self.webhook_url = webhook_url
        self.custom_gemini_key = custom_gemini_key
        self.custom_gemini_model = custom_gemini_model
        self._stop_requested = False
        self._hard_stop = False
        self._pause_event.set()
        self.totals = {
            "total": len(jobs), "sent": 0, "skipped": 0, "failed": 0,
            "flagged": 0, "dry": 0, "done": 0,
        }
        self.current = {}
        self.batch_name = batch_name or self._default_batch_name(len(jobs))
        self.state = RunState.RUNNING
        self._task = asyncio.create_task(
            self._run(jobs, action=action, dry_run=dry_run, send_on_mismatch=send_on_mismatch)
        )

    @staticmethod
    def _default_batch_name(count: int) -> str:
        ts = datetime.now()
        hour12 = ts.strftime("%I").lstrip("0") or "12"
        return f"{ts.strftime('%b %d, %Y')} - {hour12}:{ts.strftime('%M')} {ts.strftime('%p')} - {count} profiles"

    def pause(self) -> None:
        if self.state == RunState.RUNNING:
            self._pause_event.clear()
            self.state = RunState.PAUSED
            self._emit_state("Paused")

    def resume(self) -> None:
        if self.state == RunState.PAUSED:
            self._pause_event.set()
            self.state = RunState.RUNNING
            self._emit_state("Resumed")

    def stop(self) -> None:
        if self.is_busy():
            self._stop_requested = True
            self._pause_event.set()
            self._emit_state("Stopping after current profile...")

    def hard_stop(self) -> None:
        """Stop immediately: abandon the current profile and tear down the browser.

        Cancels the run task and force-closes the browser context so any in-flight
        Playwright call aborts at once (rather than finishing the current profile).
        """
        if not self.is_busy() and self._task is None:
            return
        self._stop_requested = True
        self._hard_stop = True
        self._pause_event.set()
        runner = self._runner
        if runner is not None:
            # Force the browser context shut so awaited page ops raise right away.
            asyncio.create_task(self._force_close(runner))
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._emit_state("Hard stop: aborting now.")

    @staticmethod
    async def _force_close(runner: LinkedInRunner) -> None:
        with contextlib.suppress(Exception):
            await runner.close()

    async def _wait_if_paused(self) -> None:
        await self._pause_event.wait()

    async def _interruptible_sleep(self, seconds: int) -> None:
        for _ in range(seconds):
            if self._stop_requested:
                return
            await self._wait_if_paused()
            await asyncio.sleep(1)

    # ---- the run loop -----------------------------------------------------

    async def _run(self, jobs: list[dict], *, action: str, dry_run: bool, send_on_mismatch: bool) -> None:
        governor = SafetyGovernor(self.settings.safety, self.operator)
        runner = LinkedInRunner(self.settings, self.operator)
        self._runner = runner
        self.batch_id, self.batch_public_id = db.create_batch(
            self.operator, self.batch_name, action, dry_run, len(jobs)
        )
        behavior = self.settings.behavior

        try:
            self._emit_state("Opening browser...")
            await runner.start()
            await runner.open_feed()

            if not await runner.logged_in_now():
                self.state = RunState.WAITING_LOGIN
                self._emit_state(
                    "Waiting for you to log into LinkedIn in the browser window. "
                    "Sign in and complete any 2FA, and the run starts automatically."
                )
                while not self._stop_requested:
                    await asyncio.sleep(3)
                    if await runner.logged_in_now():
                        break
                if self._stop_requested:
                    self.state = RunState.STOPPED
                    db.finalize_batch(self.batch_id, "stopped")
                    self._emit_state("Stopped before login completed.")
                    return
                self._emit_state("Logged in. Starting run.")

            # Fix: before sending, make sure names are accurate. If a job's message
            # uses a name variable and the name was only guessed from the URL (or
            # missing), auto-clean it with AI so we never send to a mangled name.
            await self._auto_resolve_names(jobs)

            self.state = RunState.RUNNING
            self._emit_state("Running" + (" (dry run)" if dry_run else ""))

            for job in jobs:
                if self._stop_requested:
                    break
                await self._wait_if_paused()
                if self._stop_requested:
                    break

                self.current = {
                    "linkedin_url": job["linkedin_url"],
                    "full_name": job.get("full_name", ""),
                    "company": job.get("company", ""),
                    "template": job.get("template", ""),
                }

                precomputed = job.get("precomputed_status", ItemStatus.QUEUED.value)
                if precomputed == ItemStatus.SKIPPED_DEDUP.value:
                    self._record(job, ItemStatus.SKIPPED_DEDUP, "previously contacted",
                                 trace=["skipped at preview: previously contacted"])
                    continue
                if precomputed == ItemStatus.NEEDS_ATTENTION.value:
                    detail = "; ".join(job.get("issues", [])) or "needs attention"
                    self._record(job, ItemStatus.NEEDS_ATTENTION, detail,
                                 trace=[f"skipped at preview: {detail}"])
                    continue

                # Safety gates only apply to live sends.
                if not dry_run:
                    if governor.daily_cap_reached():
                        self._emit_state(
                            f"Daily cap of {self.settings.safety.daily_cap} reached. Stopping."
                        )
                        break
                    if not governor.within_business_hours():
                        self._record(job, ItemStatus.NEEDS_ATTENTION, "outside business hours",
                                     trace=["skipped: outside configured business hours"])
                        continue

                self._broadcast({"type": "current", **self.current})

                job_action = self._resolve_action(job, action)
                try:
                    result = await runner.process(
                        job,
                        action=job_action,
                        dry_run=dry_run,
                        send_on_mismatch=send_on_mismatch,
                        inmail_enabled=behavior.inmail_enabled,
                        allow_noteless_fallback=behavior.allow_noteless_fallback,
                        message_if_connected=behavior.message_if_connected,
                        gemini=self.gemini,
                        ai_personalize=self.ai_personalize,
                        ai_voice=self.ai_voice,
                        custom_gemini_key=self.custom_gemini_key,
                        custom_gemini_model=self.custom_gemini_model,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._record(job, ItemStatus.FAILED_OTHER, f"error: {exc}",
                                 trace=[f"exception: {exc}"])
                    continue

                self._record(
                    job, result.status, result.detail,
                    screenshot=result.screenshot_path,
                    trace=result.trace,
                    action_executed=result.action_executed,
                    captured_name=result.captured_name,
                    degree=result.degree,
                    headline=result.headline,
                )

                if result.status == ItemStatus.FAILED_LIMIT and self.settings.safety.stop_on_limit_warning:
                    self._emit_state("LinkedIn limit warning. Stopping run to protect the account.")
                    break

                # Human-like delay only after a real send (never in dry run).
                if result.status.value in SENT_STATUSES and not dry_run and not self._stop_requested:
                    delay = governor.next_delay_seconds()
                    self._broadcast({"type": "waiting", "seconds": delay})
                    await self._interruptible_sleep(delay)

            final = "stopped" if self._stop_requested else "finished"
            self.state = RunState.STOPPED if self._stop_requested else RunState.FINISHED
            db.update_batch_counts(
                self.batch_id, self.totals["sent"], self.totals["skipped"],
                self.totals["failed"], self.totals["flagged"],
            )
            db.finalize_batch(self.batch_id, final)
            self._emit_state("Run stopped." if self._stop_requested else "Run complete.")
        except asyncio.CancelledError:
            # Hard stop: finalize as stopped and let the finally close the browser.
            self.state = RunState.STOPPED
            if self.batch_id:
                with contextlib.suppress(Exception):
                    db.update_batch_counts(
                        self.batch_id, self.totals["sent"], self.totals["skipped"],
                        self.totals["failed"], self.totals["flagged"],
                    )
                    db.finalize_batch(self.batch_id, "stopped")
            self._emit_state("Hard stopped.")
        finally:
            self._runner = None
            with contextlib.suppress(Exception):
                await asyncio.shield(self._force_close(runner))
            if self.webhook_url:
                await self._fire_webhook()

    async def _fire_webhook(self) -> None:
        """Best-effort POST of the batch summary to the configured webhook."""
        payload = {
            "event": "batch.finished",
            "batch_id": self.batch_id,
            "batch_public_id": self.batch_public_id,
            "batch_name": self.batch_name,
            "operator": self.operator,
            "action": self.action,
            "dry_run": self.dry_run,
            "state": self.state.value,
            "totals": dict(self.totals),
        }
        url = self.webhook_url

        def _post() -> None:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}, method="POST"
            )
            with contextlib.suppress(Exception):
                urllib.request.urlopen(req, timeout=10).read()

        with contextlib.suppress(Exception):
            await asyncio.to_thread(_post)

    async def _auto_resolve_names(self, jobs: list[dict]) -> None:
        """AI-clean names for rows whose message needs a name but only has a URL
        guess (or nothing). Best-effort; the live page capture is still the final
        authority at send time."""
        gemini = self.gemini
        if gemini is None or not getattr(gemini, "available", False):
            return
        targets = []
        for job in jobs:
            if job.get("name_source") == "csv":
                continue
            body = job.get("template_body") or ""
            if not body:
                continue
            if not (_NAME_VARS & referenced_variables(body)):
                continue
            if not job.get("linkedin_url"):
                continue
            targets.append(job)
        if not targets:
            return
        self._emit_state(f"Resolving {len(targets)} name(s) with AI before sending...")
        for job in targets:
            if self._stop_requested:
                break
            slug = slug_from_url(job["linkedin_url"])
            try:
                first, last, conf = await asyncio.to_thread(gemini.cleanup_name, slug)
            except Exception:  # noqa: BLE001
                continue
            if conf and first:
                self._apply_name(job, first, last, source="ai")

    @staticmethod
    def _resolve_action(job: dict, batch_action: str) -> ActionType:
        raw = (job.get("action") or batch_action or ActionType.AUTO.value).strip()
        try:
            return ActionType(raw)
        except ValueError:
            return ActionType.AUTO

    # ---- recording --------------------------------------------------------

    def _record(
        self, job: dict, status: ItemStatus, detail: str,
        *, screenshot: str = "", trace: list | None = None,
        action_executed: str = "", captured_name: str = "", degree: str = "",
        headline: str = "",
    ) -> None:
        self.totals["done"] += 1
        if status.value in SENT_STATUSES:
            self.totals["sent"] += 1
        elif status == ItemStatus.DRY_RUN:
            self.totals["dry"] += 1
        elif status == ItemStatus.MISMATCH_FLAGGED:
            self.totals["flagged"] += 1
        elif status in _SKIPPED_STATUSES:
            self.totals["skipped"] += 1
        else:
            self.totals["failed"] += 1

        full_name = captured_name or job.get("full_name", "")
        req_id, public_id = db.add_request(
            batch_id=self.batch_id or 0,
            operator=self.operator,
            linkedin_url=job["linkedin_url"],
            full_name=full_name,
            first_name=job.get("first_name", ""),
            company_csv=job.get("company", ""),
            role=job.get("role", ""),
            email=job.get("email", ""),
            action_requested=job.get("action") or self.action,
            action_executed=action_executed,
            template_id=job.get("template_id"),
            template_name=job.get("template", ""),
            message_rendered=job.get("message", ""),
            status=status.value,
            detail=detail,
            decision_trace=trace or [],
            screenshot_path=screenshot,
            headline=headline,
        )

        # Permanent contact memory (skip dry runs so they don't poison dedup).
        if status != ItemStatus.DRY_RUN:
            db.upsert_contact(
                linkedin_url=job["linkedin_url"],
                full_name=full_name,
                first_name=job.get("first_name", ""),
                company_csv=job.get("company", ""),
                last_status=status.value,
                template_used=job.get("template", ""),
                message_sent=job.get("message", "") if status.value in SENT_STATUSES else "",
                operator=self.operator,
                degree=degree,
                last_action_type=action_executed,
                headline=headline,
            )

        self._broadcast(
            {
                "type": "item",
                "request_id": public_id,
                "linkedin_url": job["linkedin_url"],
                "full_name": full_name,
                "company": job.get("company", ""),
                "template": job.get("template", ""),
                "action_executed": action_executed,
                "degree": degree,
                "status": status.value,
                "detail": detail,
                "screenshot_path": screenshot,
                "trace": trace or [],
                "totals": dict(self.totals),
            }
        )
