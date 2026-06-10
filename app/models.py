"""Pydantic schemas for API requests/responses and internal status enums."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class ActionType(str, Enum):
    """The outbound action requested for a profile (per batch, or per CSV row).

    AUTO lets the runner choose the safest correct action based on the detected
    page state (degree + available buttons). The explicit values force a single
    behavior and are used to guarantee we never silently escalate to InMail.
    """

    AUTO = "auto"
    CONNECT = "connect"             # connection request, no note
    CONNECT_NOTE = "connect_note"   # connection request with a personalized note
    INMAIL = "inmail"              # InMail to a non-connection (consumes a credit)
    MESSAGE = "message"            # direct message to an existing 1st-degree connection


# Actions that write a note/body and therefore need rendered message text.
ACTIONS_NEEDING_MESSAGE = {ActionType.CONNECT_NOTE, ActionType.INMAIL, ActionType.MESSAGE}

# Actions whose body is a connection-request note (subject to the 300-char cap).
NOTE_LIMITED_ACTIONS = {ActionType.CONNECT_NOTE}


class ItemStatus(str, Enum):
    """Outcome of processing a single profile."""

    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"  # connection request sent (with or without note)
    MESSAGE_SENT = "message_sent"  # direct DM to an existing 1st-degree connection
    INMAIL_SENT = "inmail_sent"  # InMail sent to a non-connection (explicit only)
    ALREADY_CONNECTED = "already_connected"
    PENDING_EXISTING = "pending"  # request already pending before this run
    SKIPPED_DEDUP = "skipped_dedup"  # contacted in a previous run
    MISMATCH_FLAGGED = "mismatch_flagged"  # company mismatch, awaiting operator
    NEEDS_ATTENTION = "needs_attention"  # template render problem
    OUT_OF_NETWORK = "out_of_network"  # cannot connect (out of network), InMail not enabled
    CONNECT_UNAVAILABLE = "connect_unavailable"  # requested connect but no Connect option exists
    DRY_RUN = "dry_run"  # detection + decision only; nothing was sent
    FAILED_EMAIL_REQUIRED = "failed_email_required"
    FAILED_NO_CONNECT = "failed_no_connect"
    FAILED_404 = "failed_404"
    FAILED_LIMIT = "failed_limit"  # LinkedIn weekly/daily limit hit
    FAILED_OTHER = "failed_other"


# Statuses that count as a successful or in-flight contact for dedup purposes.
TERMINAL_CONTACTED = {
    ItemStatus.SENT.value,
    ItemStatus.MESSAGE_SENT.value,
    ItemStatus.INMAIL_SENT.value,
    ItemStatus.ALREADY_CONNECTED.value,
    ItemStatus.PENDING_EXISTING.value,
}

# Statuses that represent a successful outbound send (for tallies/caps).
SENT_STATUSES = {
    ItemStatus.SENT.value,
    ItemStatus.MESSAGE_SENT.value,
    ItemStatus.INMAIL_SENT.value,
}


class RunState(str, Enum):
    IDLE = "idle"
    WAITING_LOGIN = "waiting_login"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    FINISHED = "finished"
    ERROR = "error"


# ---- preview --------------------------------------------------------------

class PreviewRow(BaseModel):
    row_index: int
    linkedin_url: str
    first_name: str
    last_name: str = ""
    full_name: str = ""
    company: str = ""
    role: str = ""
    email: str = ""
    action: str = ActionType.AUTO.value
    template: str = ""  # template name/id used (or "_inline")
    rendered_message: str = ""
    char_count: int = 0
    template_ok: bool = True
    name_source: str = ""  # "", "csv", "url_guess" (how first_name was derived)
    already_contacted: bool = False
    issues: list[str] = []


class PreviewResponse(BaseModel):
    upload_id: str
    operator: str
    action: str
    total: int
    sendable: int
    already_contacted: int
    needs_attention: int
    rows: list[PreviewRow]
    available_templates: list[str]
    notes: list[str] = []


class StartRequest(BaseModel):
    upload_id: str
    operator: str
    action: str = ActionType.AUTO.value
    dry_run: bool = False
    batch_name: str = ""  # optional friendly name; auto-generated if blank
    # Optional override: how to treat company mismatches detected on-page.
    send_on_mismatch: bool = False
    # Tailor each message with AI using the captured profile context.
    ai_personalize: bool = False
    ai_voice: str = "auto"  # professional | founder | casual | auto


class ResolveNamesRequest(BaseModel):
    upload_id: str
    mode: str = "page"  # "page" (visit profiles, accurate) | "ai" (slug cleanup, fast)


class AIGenerateRequest(BaseModel):
    goal: str
    audience: str = ""
    tone: str = ""
    max_chars: int | None = 300
    existing: str = ""       # if set, improve this draft
    operator: str = ""       # to seed {sender}
    voice: str = "auto"      # professional | founder | casual | auto


class AIReviewRequest(BaseModel):
    text: str

class TrainVoiceRequest(BaseModel):
    name: str
    examples: str


# ---- programmatic API (Phase 3) -------------------------------------------

class ProfileInput(BaseModel):
    linkedin_url: str
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    company: str = ""
    role: str = ""
    email: str = ""


class EnqueueRequest(BaseModel):
    operator: str
    profiles: list[ProfileInput]
    action: str = ActionType.AUTO.value
    template_id: int | None = None
    message_template: str = ""
    batch_name: str = ""
    dry_run: bool = False
    send_on_mismatch: bool = False
    ai_personalize: bool = False
    ai_voice: str = "auto"
    webhook_url: str = ""


class EnqueueResponse(BaseModel):
    batch_id: int
    batch_public_id: str
    operator: str
    action: str
    total: int
    sendable: int
    state: str


class AITailorRequest(BaseModel):
    base: str
    first_name: str = ""
    full_name: str = ""
    headline: str = ""
    company: str = ""
    role: str = ""
    location: str = ""
    tone: str = ""
    max_chars: int | None = 300
    operator: str = ""
    is_note: bool = True
    voice: str = "auto"


class UrlsPreviewRequest(BaseModel):
    operator: str
    action: str = ActionType.AUTO.value
    # One URL per line. Optional name/company after a comma or tab.
    urls_text: str
    # Either an inline template body, or a library template id (template_id wins
    # only when message_template is blank).
    message_template: str = ""
    template_id: int | None = None


class CsvPreviewExtras(BaseModel):
    """Non-file form fields are read directly in the endpoint; this documents them."""

    operator: str
    action: str = ActionType.AUTO.value
    template_id: int | None = None
    message_template: str = ""


class ControlResponse(BaseModel):
    state: str
    message: str = ""


class MismatchDecision(BaseModel):
    linkedin_url: str
    send_anyway: bool


# ---- templates library ----------------------------------------------------

class TemplateModel(BaseModel):
    id: int
    name: str
    body: str
    action: str = ActionType.CONNECT_NOTE.value
    tags: str = ""
    created_at: str = ""
    updated_at: str = ""


class TemplateCreate(BaseModel):
    name: str
    body: str
    action: str = ActionType.CONNECT_NOTE.value
    tags: str = ""


class TemplateUpdate(BaseModel):
    name: str | None = None
    body: str | None = None
    action: str | None = None
    tags: str | None = None


# ---- campaigns ------------------------------------------------------------

class CampaignModel(BaseModel):
    id: int
    name: str
    goal: str = ""
    template_id: int | None = None
    action: str = ActionType.AUTO.value
    voice: str = "auto"
    operator: str = ""
    scheduling_json: str = "{}"
    safety_json: str = "{}"
    status: str = "draft"
    created_at: str = ""
    updated_at: str = ""


class CampaignCreate(BaseModel):
    name: str
    goal: str = ""
    template_id: int | None = None
    action: str = ActionType.AUTO.value
    voice: str = "auto"
    operator: str = ""
    scheduling_json: str = "{}"
    safety_json: str = "{}"


class CampaignUpdate(BaseModel):
    name: str | None = None
    goal: str | None = None
    template_id: int | None = None
    action: str | None = None
    voice: str | None = None
    operator: str | None = None
    scheduling_json: str | None = None
    safety_json: str | None = None
    status: str | None = None


# ---- crm ------------------------------------------------------------------

class ContactTagCreate(BaseModel):
    tag: str


class ContactNoteCreate(BaseModel):
    note: str


# ---- voice profiles -------------------------------------------------------

class VoiceProfileModel(BaseModel):
    id: int
    name: str
    description: str = ""
    system_prompt: str = ""
    examples_json: str = "[]"
    created_at: str = ""
    updated_at: str = ""


class VoiceProfileCreate(BaseModel):
    name: str
    description: str = ""
    system_prompt: str = ""
    examples_json: str = "[]"

