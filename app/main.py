"""FastAPI app: REST routes, WebSocket live feed, and static dashboard serving."""

from __future__ import annotations

import asyncio
import uuid

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import csv_ingest, db, voice as voicelib, campaigns, crm, analytics, export
from .ai import GeminiClient
from .models import (
    ActionType,
    AIGenerateRequest,
    AIReviewRequest,
    AITailorRequest,
    ControlResponse,
    EnqueueRequest,
    EnqueueResponse,
    PreviewResponse,
    ResolveNamesRequest,
    StartRequest,
    TemplateCreate,
    TemplateModel,
    TemplateUpdate,
    TrainVoiceRequest,
    UrlsPreviewRequest,
)
from .orchestrator import Orchestrator
from .settings import load_settings

settings = load_settings()
db.init_db(settings.data_dir / "outbound.db")
db.seed_templates(settings.templates, default_action=ActionType.CONNECT_NOTE.value)
db.seed_operators(settings.operators)

app = FastAPI(title="LinkBound · LinkedIn Outbound", version="2.0.0")
orchestrator = Orchestrator(settings)
gemini = GeminiClient(settings.ai)
orchestrator.gemini = gemini  # used for optional per-profile AI personalization

app.include_router(campaigns.router)
app.include_router(crm.router)
app.include_router(analytics.router)
app.include_router(export.router)

STATIC_DIR = settings.root / "static"

import json
from datetime import datetime, timezone

# In-memory store of parsed uploads, keyed by upload_id.
_UPLOADS: dict[str, dict] = {}

class OrchestratorManager:
    def __init__(self, settings):
        self.settings = settings
        self.orchestrators: dict[str, Orchestrator] = {}

    def get(self, operator: str) -> Orchestrator:
        if operator not in self.orchestrators:
            o = Orchestrator(self.settings)
            o.gemini = gemini
            self.orchestrators[operator] = o
        return self.orchestrators[operator]

    def snapshot(self) -> dict:
        # Return the snapshot of the first busy orchestrator, or default
        for o in self.orchestrators.values():
            if o.is_busy():
                return o.snapshot()
        if self.orchestrators:
            return next(iter(self.orchestrators.values())).snapshot()
        # default empty
        return {"state": "idle", "totals": {}, "current": {}}

manager = OrchestratorManager(settings)

async def _scheduler_loop():
    """Background loop that polls for scheduled campaigns and executes them."""
    while True:
        try:
            now = datetime.now(timezone.utc).isoformat()
            with db._LOCK:
                cur = db._conn().execute("SELECT * FROM campaigns WHERE status='scheduled'")
                campaigns = [dict(r) for r in cur.fetchall()]
                
            for c in campaigns:
                try:
                    s_json = json.loads(c.get("scheduling_json") or "{}")
                    start_time = s_json.get("start_time")
                    if start_time and start_time <= now:
                        operator = c["operator"]
                        orch = manager.get(operator)
                        if not orch.is_busy():
                            # Mock jobs for a campaign since we don't have CSV links directly in campaigns yet
                            # In a real app, we'd fetch the contacts associated with this campaign.
                            # For now, we just mark it as running and then finished.
                            with db._LOCK:
                                conn = db._conn()
                                conn.execute("UPDATE campaigns SET status='running' WHERE id=?", (c["id"],))
                                conn.commit()
                except Exception as e:
                    print(f"Scheduler error for campaign {c['id']}: {e}")
        except Exception as e:
            print(f"Scheduler loop error: {e}")
        await asyncio.sleep(60)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(_scheduler_loop())


def _parse_action(raw: str) -> ActionType:
    try:
        return ActionType((raw or "").strip() or settings.behavior.default_action)
    except ValueError:
        return ActionType.AUTO


def _resolve_default_template(template_id: int | None, inline_body: str) -> tuple[int | None, str, str]:
    """Decide the default template for rows without their own template column."""
    if inline_body and inline_body.strip():
        return (None, "_inline", inline_body)
    if template_id:
        t = db.get_template(template_id)
        if t:
            return (t["id"], t["name"], t["body"])
    return (None, "", "")


def _make_resolver(default: tuple[int | None, str, str]):
    templates = db.list_templates()
    by_name = {t["name"].lower(): t for t in templates}

    def resolve(row_template: str | None) -> tuple[int | None, str, str]:
        if row_template:
            t = by_name.get(row_template.strip().lower())
            if t:
                return (t["id"], t["name"], t["body"])
        return default

    return resolve, {t["name"] for t in templates}


def _build_preview_response(operator: str, action: ActionType, preview_rows, jobs, notes,
                            csv_name: str) -> PreviewResponse:
    upload_id = uuid.uuid4().hex
    _UPLOADS[upload_id] = {"operator": operator, "csv_name": csv_name, "jobs": jobs}
    sendable = sum(1 for j in jobs if j["precomputed_status"] == "queued")
    already = sum(1 for j in jobs if j["precomputed_status"] == "skipped_dedup")
    attention = sum(1 for j in jobs if j["precomputed_status"] == "needs_attention")
    return PreviewResponse(
        upload_id=upload_id,
        operator=operator,
        action=action.value,
        total=len(jobs),
        sendable=sendable,
        already_contacted=already,
        needs_attention=attention,
        rows=preview_rows,
        available_templates=[t["name"] for t in db.list_templates()],
        notes=notes,
    )


# ---- config / health ------------------------------------------------------

@app.get("/api/config")
async def get_config():
    ops = db.list_operators()
    return {
        "operators": [{"key": op["key"], "label": op["label"]} for op in ops],
        "templates": [t["name"] for t in db.list_templates()],
        "actions": [a.value for a in ActionType],
        "default_action": settings.behavior.default_action,
        "behavior": {
            "inmail_enabled": settings.behavior.inmail_enabled,
            "message_if_connected": settings.behavior.message_if_connected,
            "allow_noteless_fallback": settings.behavior.allow_noteless_fallback,
        },
        "safety": {
            "daily_cap": settings.safety.daily_cap,
            "min_delay_seconds": settings.safety.min_delay_seconds,
            "max_delay_seconds": settings.safety.max_delay_seconds,
        },
        "ai": {
            "configured": gemini.configured,
            "enabled": settings.ai.enabled,
            "model": settings.ai.model,
            "voices": voicelib.available_voices(),
        },
    }


@app.get("/api/ai/status")
async def ai_status():
    return {
        "configured": gemini.configured,
        "enabled": settings.ai.enabled,
        "available": gemini.available,
        "model": settings.ai.model,
        "model_reasoning": settings.ai.model_reasoning,
        "model_fast": settings.ai.model_fast,
    }


# ---- templates library ----------------------------------------------------

@app.get("/api/templates")
async def get_templates():
    return {"templates": db.list_templates()}


@app.post("/api/templates", response_model=TemplateModel)
async def create_template(body: TemplateCreate):
    if not body.name.strip() or not body.body.strip():
        raise HTTPException(400, "Template name and body are required.")
    if db.get_template_by_name(body.name):
        raise HTTPException(409, f"A template named '{body.name}' already exists.")
    tid = db.create_template(body.name.strip(), body.body, body.action, body.tags)
    return TemplateModel(**db.get_template(tid))


@app.put("/api/templates/{template_id}", response_model=TemplateModel)
async def update_template(template_id: int, body: TemplateUpdate):
    if not db.get_template(template_id):
        raise HTTPException(404, "Template not found.")
    db.update_template(template_id, body.model_dump(exclude_none=True))
    return TemplateModel(**db.get_template(template_id))


@app.delete("/api/templates/{template_id}")
async def delete_template(template_id: int):
    if not db.delete_template(template_id):
        raise HTTPException(404, "Template not found.")
    return {"ok": True}

# ---- operators library ----------------------------------------------------

from pydantic import BaseModel

class OperatorCreate(BaseModel):
    name: str

@app.get("/api/operators")
async def get_operators():
    return {"operators": db.list_operators()}

@app.post("/api/operators")
async def create_operator(body: OperatorCreate):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Operator name is required.")
    key = name.lower().replace(" ", "_").replace("-", "_")
    
    # Check if exists
    ops = db.list_operators()
    if any(o["key"] == key for o in ops):
        raise HTTPException(409, "Operator already exists.")
        
    db.create_operator(key, name, f"profiles/{key}")
    # Also add it dynamically to settings so current run validates
    from .settings import OperatorConfig
    settings.operators[key] = OperatorConfig(key=key, label=name, profile_dir=f"profiles/{key}")
    
    return {"key": key, "label": name}

@app.delete("/api/operators/{key}")
async def delete_operator(key: str):
    if not db.delete_operator(key):
        raise HTTPException(404, "Operator not found.")
    if key in settings.operators:
        del settings.operators[key]
    return {"ok": True}

# ---- preview --------------------------------------------------------------

@app.post("/api/preview", response_model=PreviewResponse)
async def preview(
    operator: str = Form(...),
    action: str = Form(ActionType.AUTO.value),
    template_id: int | None = Form(None),
    message_template: str = Form(""),
    file: UploadFile = File(...),
):
    ops = {op["key"]: op for op in db.list_operators()}
    if operator not in ops:
        raise HTTPException(400, f"Unknown operator '{operator}'.")
    act = _parse_action(action)

    raw = await file.read()
    try:
        raw_rows = csv_ingest.parse_csv_bytes(raw)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not parse CSV: {exc}") from exc
    if not raw_rows:
        raise HTTPException(400, "CSV has no data rows.")

    default = _resolve_default_template(template_id, message_template)
    resolver, template_keys = _make_resolver(default)

    preview_rows, jobs, notes = csv_ingest.build_preview(
        settings, operator, raw_rows,
        action=act, resolve_template=resolver, template_keys=template_keys,
    )
    return _build_preview_response(operator, act, preview_rows, jobs, notes,
                                   file.filename or "upload.csv")


@app.post("/api/preview-urls", response_model=PreviewResponse)
async def preview_urls(req: UrlsPreviewRequest):
    ops = {op["key"]: op for op in db.list_operators()}
    if req.operator not in ops:
        raise HTTPException(400, f"Unknown operator '{req.operator}'.")
    if not req.urls_text.strip():
        raise HTTPException(400, "No URLs provided.")
    act = _parse_action(req.action)

    tid, tname, body = _resolve_default_template(req.template_id, req.message_template)
    if act in {ActionType.CONNECT_NOTE, ActionType.INMAIL, ActionType.MESSAGE} and not body.strip():
        raise HTTPException(400, "This action needs a message template (pick one or type one).")

    preview_rows, jobs, notes = csv_ingest.build_preview_from_urls(
        settings, req.operator, req.urls_text,
        action=act, template_id=tid, template_name=tname, template_body=body,
    )
    if not preview_rows:
        raise HTTPException(400, notes[0] if notes else "No valid LinkedIn URLs found.")
    return _build_preview_response(req.operator, act, preview_rows, jobs, notes, "direct_urls")


# ---- run control ----------------------------------------------------------

@app.post("/api/start", response_model=ControlResponse)
async def start(req: StartRequest, x_user_gemini_key: str | None = Header(None), x_user_gemini_model: str | None = Header(None)):
    upload = _UPLOADS.get(req.upload_id)
    if not upload:
        raise HTTPException(404, "Upload not found. Re-run the preview.")
    if upload["operator"] != req.operator:
        raise HTTPException(400, "Operator does not match the previewed batch.")
    orch = manager.get(req.operator)
    if orch.is_busy():
        raise HTTPException(409, "A run is already in progress for this operator.")

    if req.ai_personalize and not gemini.available:
        raise HTTPException(400, "AI personalization requested but AI is not enabled/configured.")

    await orch.start(
        upload["jobs"],
        req.operator,
        action=_parse_action(req.action).value,
        dry_run=req.dry_run,
        batch_name=req.batch_name,
        send_on_mismatch=req.send_on_mismatch,
        ai_personalize=req.ai_personalize,
        ai_voice=req.ai_voice,
        custom_gemini_key=x_user_gemini_key,
        custom_gemini_model=x_user_gemini_model,
    )
    return ControlResponse(state=orch.state.value,
                           message=("Dry run started." if req.dry_run else "Run started."))


# ---- name resolution + AI helpers -----------------------------------------

@app.post("/api/resolve-names")
async def resolve_names(req: ResolveNamesRequest):
    upload = _UPLOADS.get(req.upload_id)
    if not upload:
        raise HTTPException(404, "Upload not found. Re-run the preview.")
    if req.mode == "ai" and not gemini.available:
        raise HTTPException(400, "AI is not enabled/configured.")
    if orchestrator.is_busy():
        raise HTTPException(409, "Busy: a run or resolve is already in progress.")
    try:
        updated = await orchestrator.resolve_names(
            upload["jobs"], upload["operator"], mode=req.mode, gemini=gemini
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"updated": updated}


@app.post("/api/ai/generate")
async def ai_generate(req: AIGenerateRequest, x_user_gemini_key: str | None = Header(None), x_user_gemini_model: str | None = Header(None)):
    if not gemini.available:
        raise HTTPException(400, "AI is not enabled/configured.")
    sender = ""
    ops = {op["key"]: op for op in db.list_operators()}
    if req.operator and req.operator in ops:
        sender = (ops[req.operator]["label"] or "").split(" ")[0]
    try:
        text = await asyncio.to_thread(
            gemini.generate_template,
            goal=req.goal, audience=req.audience, tone=req.tone,
            max_chars=req.max_chars, existing=req.existing, sender=sender,
            voice=req.voice, api_key=x_user_gemini_key, model_override=x_user_gemini_model,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"AI error: {exc}") from exc
    return {"text": text}


@app.post("/api/ai/tailor")
async def ai_tailor(req: AITailorRequest, x_user_gemini_key: str | None = Header(None), x_user_gemini_model: str | None = Header(None)):
    if not gemini.available:
        raise HTTPException(400, "AI is not enabled/configured.")
    sender = ""
    ops = {op["key"]: op for op in db.list_operators()}
    if req.operator and req.operator in ops:
        sender = (ops[req.operator]["label"] or "").split(" ")[0]
    context = {
        "first_name": req.first_name, "full_name": req.full_name,
        "headline": req.headline, "company": req.company,
        "role": req.role, "location": req.location,
    }
    try:
        text = await asyncio.to_thread(
            gemini.tailor_message,
            base=req.base, context=context, tone=req.tone,
            max_chars=req.max_chars, sender=sender, is_note=req.is_note,
            voice=req.voice, api_key=x_user_gemini_key, model_override=x_user_gemini_model,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"AI error: {exc}") from exc
    return {"text": text}


@app.post("/api/ai/review")
async def ai_review(req: AIReviewRequest, x_user_gemini_key: str | None = Header(None), x_user_gemini_model: str | None = Header(None)):
    if not gemini.available:
        raise HTTPException(400, "AI is not enabled/configured.")
    if not req.text.strip():
        raise HTTPException(400, "Nothing to review.")
    try:
        result = await asyncio.to_thread(gemini.review_message, req.text, x_user_gemini_key, x_user_gemini_model)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"AI error: {exc}") from exc
    return result


@app.post("/api/ai/train-voice")
async def ai_train_voice(req: TrainVoiceRequest, x_user_gemini_key: str | None = Header(None), x_user_gemini_model: str | None = Header(None)):
    if not gemini.available:
        raise HTTPException(400, "AI is not enabled/configured.")
    if not req.examples.strip():
        raise HTTPException(400, "Examples are required.")
        
    prompt = f"Analyze these outbound message examples and extract the exact writing style, tone, and rules into a highly specific system prompt fragment that I can use to generate more messages in exactly this voice. Return ONLY the system prompt text.\n\nExamples:\n{req.examples}"
    
    try:
        sys_prompt = await asyncio.to_thread(
            gemini.generate_text, prompt, api_key=x_user_gemini_key, model=x_user_gemini_model
        )
        
        now = db._now()
        with db._LOCK:
            conn = db._conn()
            conn.execute(
                "INSERT INTO voice_profiles (name, description, system_prompt, examples_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (req.name, "Custom trained voice", sys_prompt, "[]", now, now)
            )
            conn.commit()
            
    except Exception as exc:
        raise HTTPException(502, f"AI error: {exc}") from exc
        
    return {"system_prompt": sys_prompt}

@app.post("/api/pause", response_model=ControlResponse)
async def pause():
    state, msg = "paused", "All paused"
    for o in manager.orchestrators.values():
        o.pause()
        state, msg = o.state.value, o.message
    return ControlResponse(state=state, message=msg)

@app.post("/api/resume", response_model=ControlResponse)
async def resume():
    state, msg = "running", "All resumed"
    for o in manager.orchestrators.values():
        o.resume()
        state, msg = o.state.value, o.message
    return ControlResponse(state=state, message=msg)

@app.post("/api/stop", response_model=ControlResponse)
async def stop():
    state, msg = "stopped", "All stopped"
    for o in manager.orchestrators.values():
        o.stop()
        state, msg = o.state.value, o.message
    return ControlResponse(state=state, message=msg)

@app.post("/api/hard-stop", response_model=ControlResponse)
async def hard_stop():
    state, msg = "stopped", "All hard stopped"
    for o in manager.orchestrators.values():
        o.hard_stop()
        state, msg = o.state.value, o.message
    return ControlResponse(state=state, message=msg)

@app.get("/api/status")
async def status():
    return manager.snapshot()


# ---- history / analytics --------------------------------------------------

@app.get("/api/contacts")
async def contacts(search: str = "", limit: int = 500):
    return {"contacts": db.list_contacts(search=search, limit=limit)}


@app.get("/api/batches")
async def batches(limit: int = 50):
    return {"batches": db.list_batches(limit=limit)}


@app.get("/api/batches/{batch_id}")
async def batch_detail(batch_id: int):
    batch = db.get_batch(batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found.")
    return {"batch": batch, "requests": db.list_requests(batch_id)}


# ---- screenshots ----------------------------------------------------------

@app.get("/api/screenshot")
async def screenshot(path: str):
    target = (settings.root / path).resolve()
    shots_dir = settings.screenshots_dir().resolve()
    if not str(target).startswith(str(shots_dir)) or not target.exists():
        raise HTTPException(404, "Screenshot not found.")
    return FileResponse(str(target))


# ---- websocket live feed --------------------------------------------------

@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    # Subscribe to all orchestrators (or the default one)
    # For a simple approach, we'll just subscribe to the default orchestrator 
    # created at boot time so the dashboard works.
    queue = orchestrator.subscribe()
    await websocket.send_json(manager.snapshot())
    try:
        while True:
            event = await queue.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        orchestrator.unsubscribe(queue)


# ---- programmatic API (Phase 3) -------------------------------------------

async def require_api_key(x_api_key: str = Header(default="")) -> str:
    """Authorize /api/v1/* access via the X-API-Key header."""
    if not settings.api.require_key:
        return "anonymous"
    if not settings.api.keys:
        raise HTTPException(503, "No API keys configured (set OUTBOUND_API_KEYS in .env).")
    if x_api_key not in settings.api.keys:
        raise HTTPException(401, "Invalid or missing X-API-Key.")
    return x_api_key


@app.get("/api/v1/health")
async def v1_health(_key: str = Depends(require_api_key)):
    return {"ok": True, "version": app.version, "busy": orchestrator.is_busy()}


@app.get("/api/v1/templates")
async def v1_templates(_key: str = Depends(require_api_key)):
    return {"templates": db.list_templates()}


@app.post("/api/v1/enqueue", response_model=EnqueueResponse)
async def v1_enqueue(req: EnqueueRequest, _key: str = Depends(require_api_key)):
    """Programmatically start an outbound batch. Designed for internal tools (e.g.
    the candidate-account engine) to drive outreach without the dashboard."""
    ops = {op["key"]: op for op in db.list_operators()}
    if req.operator not in ops:
        raise HTTPException(400, f"Unknown operator '{req.operator}'.")
    if not req.profiles:
        raise HTTPException(400, "No profiles provided.")
    if orchestrator.is_busy():
        raise HTTPException(409, "A run is already in progress.")
    act = _parse_action(req.action)
    tid, tname, body = _resolve_default_template(req.template_id, req.message_template)
    if act in {ActionType.CONNECT_NOTE, ActionType.INMAIL, ActionType.MESSAGE} and not body.strip():
        raise HTTPException(400, "This action needs a message template (template_id or message_template).")
    if req.ai_personalize and not gemini.available:
        raise HTTPException(400, "ai_personalize requested but AI is not enabled/configured.")

    profiles = [p.model_dump() for p in req.profiles]
    _preview, jobs, _notes = csv_ingest.build_jobs_from_profiles(
        settings, req.operator, profiles,
        action=act, template_id=tid, template_name=tname, template_body=body,
    )
    await orchestrator.start(
        jobs, req.operator,
        action=act.value, dry_run=req.dry_run, batch_name=req.batch_name,
        send_on_mismatch=req.send_on_mismatch, ai_personalize=req.ai_personalize,
        ai_voice=req.ai_voice,
        webhook_url=req.webhook_url or settings.api.default_webhook,
    )
    sendable = sum(1 for j in jobs if j["precomputed_status"] == "queued")
    return EnqueueResponse(
        batch_id=orchestrator.batch_id or 0,
        batch_public_id=orchestrator.batch_public_id,
        operator=req.operator,
        action=act.value,
        total=len(jobs),
        sendable=sendable,
        state=orchestrator.state.value,
    )


@app.get("/api/v1/batches/{batch_id}")
async def v1_batch(batch_id: int, _key: str = Depends(require_api_key)):
    batch = db.get_batch(batch_id)
    if not batch:
        raise HTTPException(404, "Batch not found.")
    return {"batch": batch, "requests": db.list_requests(batch_id)}


@app.get("/api/v1/status")
async def v1_status(_key: str = Depends(require_api_key)):
    return orchestrator.snapshot()


# ---- static dashboard -----------------------------------------------------

@app.get("/")
async def index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return JSONResponse({"error": "dashboard not built"}, status_code=500)
    return FileResponse(str(index_file))


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
