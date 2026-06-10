"""Loads config.yaml and templates.yaml into typed settings objects."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

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
    root: Path = ROOT
    data_dir: Path = DATA_DIR

    def operator(self, key: str) -> OperatorConfig:
        if key not in self.operators:
            raise KeyError(f"Unknown operator '{key}'. Known: {list(self.operators)}")
        return self.operators[key]

    def profile_path(self, operator_key: str) -> Path:
        """Absolute, ensured path to the operator's persistent Chrome profile."""
        op = self.operator(operator_key)
        path = self.root / op.profile_dir
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
    browser = BrowserConfig(**(cfg.get("browser") or {}))
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

    DATA_DIR.mkdir(parents=True, exist_ok=True)

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
    )
