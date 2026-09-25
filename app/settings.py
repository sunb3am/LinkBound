"""Loads config.yaml and templates.yaml into typed settings objects."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

try:  # optional dependency; .env is convenient but not required
    from dotenv import load_dotenv
except Exception:  # noqa: BLE001
    load_dotenv = None

# Project root is the linkedin-outbound/ folder (parent of app/).
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
TEMPLATES_PATH = ROOT / "templates.yaml"
DATA_DIR = ROOT / "data"

if load_dotenv is not None:
    # override=True so the project's .env wins over any stale GEMINI_API_KEY that
    # may already be exported in the OS environment.
    load_dotenv(ROOT / ".env", override=True)


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass
class OperatorConfig:
    key: str
    label: str
    profile_dir: str


@dataclass
class SafetyConfig:
    daily_cap: int = 22
    queue_weekly_cap: int = 100
    min_delay_seconds: int = 45
    max_delay_seconds: int = 90
    business_hours_only: bool = False
    business_hours_start: int = 9
    business_hours_end: int = 18
    stop_on_limit_warning: bool = True


@dataclass
class BrowserConfig:
    channel: str = "chrome"
    headless: bool = False
    chromium_sandbox: bool = False
    cdp_url: str = ""
    nav_timeout_ms: int = 30000


@dataclass
class BehaviorConfig:
    # For an existing 1st-degree connection, send the rendered message via the
    # Message composer (a direct message) rather than skipping.
    message_if_connected: bool = True
    # Allow InMail to non-connections when AUTO is used and no Connect exists.
    # Default OFF: InMail otherwise only happens for an explicit INMAIL action.
    inmail_enabled: bool = False
    # If a CONNECT_NOTE cannot reveal the note field, send a noteless invite
    # instead of flagging. Default OFF (we prefer to flag for review).
    allow_noteless_fallback: bool = False
    # Default action when a batch does not specify one.
    default_action: str = "auto"


@dataclass
class AIConfig:
    enabled: bool = False
    model: str = "gemini-3.5-flash"             # workhorse
    model_reasoning: str = "gemini-3.1-pro-preview"  # deeper reasoning
    model_fast: str = "gemini-3-flash-preview"  # fast/cheap preview
    api_key: str = ""                            # resolved from env at load time


@dataclass
class APIConfig:
    """Programmatic-access config for the pluggable interface (Phase 3).

    Keys come from the OUTBOUND_API_KEYS env var (comma-separated), never from
    config.yaml. When require_key is true, the /api/v1/* endpoints demand a valid
    X-API-Key header.
    """

    require_key: bool = True
    keys: list[str] = field(default_factory=list)
    default_webhook: str = ""


@dataclass
class InboundScheduleConfig:
    enabled: bool = False
    operator: str = "me"
    time_local: str = "18:00"
    timezone: str = "America/Los_Angeles"
    max_rows_per_folder: int = 2


@dataclass
class Settings:
    server: ServerConfig
    operators: dict[str, OperatorConfig]
    safety: SafetyConfig
    browser: BrowserConfig
    behavior: BehaviorConfig
    ai: AIConfig
    api: APIConfig
    column_mapping: dict[str, list[str]]
    templates: dict[str, str]
    require_tailscale_auth: bool
    tailscale_allowed_users: list[str]
    allow_tailnet_devices: bool
    allow_live_sends: bool
    inbound_schedule: InboundScheduleConfig = field(default_factory=InboundScheduleConfig)
    require_exit_node: bool = False
    root: Path = ROOT
    data_dir: Path = DATA_DIR
    profile_root: Path = ROOT

    def operator(self, key: str) -> OperatorConfig:
        if key not in self.operators:
            raise KeyError(f"Unknown operator '{key}'. Known: {list(self.operators)}")
        return self.operators[key]

    def profile_path(self, operator_key: str) -> Path:
        """Absolute, ensured path to the operator's persistent Chrome profile."""
        op = self.operator(operator_key)
        configured = Path(op.profile_dir)
        path = configured if configured.is_absolute() else self.profile_root / configured
        path = path.resolve()
        if not configured.is_absolute() and not path.is_relative_to(self.profile_root.resolve()):
            raise ValueError("profile_dir must stay within LINKBOUND_PROFILE_ROOT")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def screenshots_dir(self) -> Path:
        path = self.data_dir / "screenshots"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def uploads_dir(self) -> Path:
        path = self.data_dir / "uploads"
        path.mkdir(parents=True, exist_ok=True)
        return path


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings() -> Settings:
    cfg = _read_yaml(CONFIG_PATH)
    templates = _read_yaml(TEMPLATES_PATH)

    server = ServerConfig(**(cfg.get("server") or {}))

    operators: dict[str, OperatorConfig] = {}
    for key, raw in (cfg.get("operators") or {}).items():
        operators[key] = OperatorConfig(
            key=key,
            label=raw.get("label", key),
            profile_dir=raw.get("profile_dir", f"profiles/{key}"),
        )

    safety = SafetyConfig(**(cfg.get("safety") or {}))
    for env_name, field_name in (
        ("LINKBOUND_PILOT_DAILY_CAP", "daily_cap"),
        ("LINKBOUND_PILOT_WEEKLY_CAP", "queue_weekly_cap"),
    ):
        value = os.environ.get(env_name)
        if value is not None:
            try:
                cap = int(value)
            except ValueError as exc:
                raise ValueError(f"{env_name} must be a positive integer") from exc
            if cap < 1 or cap > getattr(safety, field_name):
                raise ValueError(f"{env_name} must be between 1 and the configured cap")
            setattr(safety, field_name, cap)
    browser = BrowserConfig(**(cfg.get("browser") or {}))
    sandbox_value = os.environ.get("LINKBOUND_CHROMIUM_SANDBOX")
    if sandbox_value is not None:
        sandbox_value = sandbox_value.strip().casefold()
        if sandbox_value not in {"true", "false"}:
            raise ValueError("LINKBOUND_CHROMIUM_SANDBOX must be true or false")
        browser.chromium_sandbox = sandbox_value == "true"
    behavior = BehaviorConfig(**(cfg.get("behavior") or {}))

    ai_cfg = AIConfig(**(cfg.get("ai") or {}))
    # The API key always comes from the environment (.env), never config.yaml.
    ai_cfg.api_key = os.environ.get("GEMINI_API_KEY", "").strip()

    api_raw = cfg.get("api") or {}
    api_cfg = APIConfig(
        require_key=bool(api_raw.get("require_key", True)),
        default_webhook=str(api_raw.get("default_webhook", "")),
        keys=[k.strip() for k in os.environ.get("OUTBOUND_API_KEYS", "").split(",") if k.strip()],
    )

    column_mapping = cfg.get("column_mapping") or {}

    data_dir = Path(os.environ.get("LINKBOUND_DATA_DIR", str(DATA_DIR))).expanduser()
    if not data_dir.is_absolute():
        raise ValueError("LINKBOUND_DATA_DIR must be an absolute path")
    data_dir.mkdir(parents=True, exist_ok=True)

    profile_root = Path(os.environ.get("LINKBOUND_PROFILE_ROOT", str(ROOT))).expanduser()
    if not profile_root.is_absolute():
        raise ValueError("LINKBOUND_PROFILE_ROOT must be an absolute path")

    auth_value = os.environ.get("LINKBOUND_REQUIRE_TAILSCALE_AUTH", "false").strip().casefold()
    if auth_value not in {"true", "false"}:
        raise ValueError("LINKBOUND_REQUIRE_TAILSCALE_AUTH must be true or false")
    require_tailscale_auth = auth_value == "true"
    tailnet_value = os.environ.get("LINKBOUND_ALLOW_TAILNET_DEVICES", "false").strip().casefold()
    if tailnet_value not in {"true", "false"}:
        raise ValueError("LINKBOUND_ALLOW_TAILNET_DEVICES must be true or false")
    allow_tailnet_devices = tailnet_value == "true"
    tailscale_allowed_users = [
        user.strip().casefold()
        for user in os.environ.get("LINKBOUND_TAILSCALE_ALLOWED_USERS", "").split(",")
        if user.strip()
    ]
    if allow_tailnet_devices and not require_tailscale_auth:
        raise ValueError("LINKBOUND_REQUIRE_TAILSCALE_AUTH must be true when allowing tailnet devices")
    if require_tailscale_auth and not (tailscale_allowed_users or allow_tailnet_devices):
        raise ValueError("Set LINKBOUND_TAILSCALE_ALLOWED_USERS or LINKBOUND_ALLOW_TAILNET_DEVICES")

    send_value = os.environ.get("LINKBOUND_ALLOW_LIVE_SENDS", "true").strip().casefold()
    if send_value not in {"true", "false"}:
        raise ValueError("LINKBOUND_ALLOW_LIVE_SENDS must be true or false")
    allow_live_sends = send_value == "true"

    exit_value = os.environ.get(
        "LINKBOUND_REQUIRE_EXIT_NODE", "true" if require_tailscale_auth else "false"
    ).strip().casefold()
    if exit_value not in {"true", "false"}:
        raise ValueError("LINKBOUND_REQUIRE_EXIT_NODE must be true or false")
    require_exit_node = exit_value == "true"

    sync_value = os.environ.get("LINKBOUND_INBOUND_SYNC_ENABLED", "false").strip().casefold()
    if sync_value not in {"true", "false"}:
        raise ValueError("LINKBOUND_INBOUND_SYNC_ENABLED must be true or false")
    sync_time = os.environ.get("LINKBOUND_INBOUND_SYNC_TIME", "18:00").strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", sync_time):
        raise ValueError("LINKBOUND_INBOUND_SYNC_TIME must be HH:MM in 24-hour local time")
    sync_timezone = os.environ.get("LINKBOUND_INBOUND_SYNC_TIMEZONE", "America/Los_Angeles").strip()
    try:
        ZoneInfo(sync_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("LINKBOUND_INBOUND_SYNC_TIMEZONE must be an IANA timezone") from exc
    sync_rows = int(os.environ.get("LINKBOUND_INBOUND_SYNC_ROWS_PER_FOLDER", "2"))
    if not 1 <= sync_rows <= 20:
        raise ValueError("LINKBOUND_INBOUND_SYNC_ROWS_PER_FOLDER must be 1-20")
    inbound_schedule = InboundScheduleConfig(
        enabled=sync_value == "true",
        operator=os.environ.get("LINKBOUND_INBOUND_SYNC_OPERATOR", "me").strip(),
        time_local=sync_time, timezone=sync_timezone,
        max_rows_per_folder=sync_rows,
    )
    if inbound_schedule.enabled and not inbound_schedule.operator:
        raise ValueError("LINKBOUND_INBOUND_SYNC_OPERATOR is required when sync is enabled")

    return Settings(
        server=server,
        operators=operators,
        safety=safety,
        browser=browser,
        behavior=behavior,
        ai=ai_cfg,
        api=api_cfg,
        column_mapping=column_mapping,
        templates={str(k): str(v) for k, v in templates.items()},
        require_tailscale_auth=require_tailscale_auth,
        tailscale_allowed_users=tailscale_allowed_users,
        allow_tailnet_devices=allow_tailnet_devices,
        allow_live_sends=allow_live_sends,
        require_exit_node=require_exit_node,
        inbound_schedule=inbound_schedule,
        data_dir=data_dir,
        profile_root=profile_root,
    )
