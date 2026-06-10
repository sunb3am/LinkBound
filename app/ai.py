"""Gemini integration: client wiring plus the higher-level helpers used by
LinkBound (name cleanup, message tailoring, template generation).

The ``google-genai`` SDK is imported lazily so the app runs fine when AI is
disabled or the package is not installed. All helpers raise on failure; callers
decide how to degrade gracefully.
"""

from __future__ import annotations

import json
import logging

from . import voice as voicelib
from .settings import AIConfig

log = logging.getLogger(__name__)


class GeminiClient:
    def __init__(self, cfg: AIConfig):
        self.cfg = cfg
        self._client = None

    @property
    def configured(self) -> bool:
        """A key is present (regardless of the enabled flag)."""
        return bool(self.cfg.api_key)

    @property
    def available(self) -> bool:
        """AI is enabled in config AND a key is present."""
        return bool(self.cfg.enabled and self.cfg.api_key)

    def _ensure(self, custom_key: str | None = None):
        from google import genai  # lazy import
        if custom_key:
            return genai.Client(api_key=custom_key)

        if self._client is not None:
            return self._client
        if not self.cfg.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set (add it to .env or provide your own key in settings).")

        self._client = genai.Client(api_key=self.cfg.api_key)
        return self._client

    # ---- low-level -------------------------------------------------------

    def generate_text(self, prompt: str, *, model: str | None = None,
                      system: str | None = None, api_key: str | None = None) -> str:
        client = self._ensure(api_key)
        from google.genai import types

        cfg = types.GenerateContentConfig(system_instruction=system) if system else None
        resp = client.models.generate_content(
            model=model or self.cfg.model, contents=prompt, config=cfg,
        )
        return (getattr(resp, "text", None) or "").strip()

    def generate_json(self, prompt: str, *, model: str | None = None,
                      system: str | None = None, api_key: str | None = None) -> dict:
        client = self._ensure(api_key)
        from google.genai import types

        cfg = types.GenerateContentConfig(
            response_mime_type="application/json",
            system_instruction=system,
        )
        resp = client.models.generate_content(
            model=model or self.cfg.model, contents=prompt, config=cfg,
        )
        raw = (getattr(resp, "text", None) or "").strip()
        return _safe_json(raw)

    def ping(self) -> str:
        """Tiny connectivity check; returns the model's reply."""
        return self.generate_text("Reply with exactly: OK")

    # ---- name cleanup ----------------------------------------------------

    def cleanup_name(self, slug: str, *, headline: str = "") -> tuple[str, str, bool]:
        """Infer (first_name, last_name, confident) from a URL slug.

        Splits concatenated names ("janedoe" -> "Jane Doe") and drops ids. Returns
        empty strings (confident=False) for vanity handles/initials it cannot map
        to a real name, rather than inventing one.
        """
        extra = f'\nProfile headline (may help): "{headline}"' if headline else ""
        prompt = (
            "Extract a person's real first and last name from a LinkedIn profile "
            "URL slug. The slug may concatenate names (e.g. 'janedoe' -> Jane Doe) "
            "or contain trailing ids/hashes. If the slug is a vanity handle or "
            "initials that do not clearly map to a real human name (e.g. 'dryd', "
            "'the-growth-guy'), return empty strings and confident=false. NEVER "
            f'invent a name.\nSlug: "{slug}"{extra}\n'
            'Return strict JSON: {"first_name": string, "last_name": string, "confident": boolean}'
        )
        data = self.generate_json(prompt, model=self.cfg.model)
        first = str(data.get("first_name", "") or "").strip()
        last = str(data.get("last_name", "") or "").strip()
        confident = bool(data.get("confident", False)) and bool(first)
        return first, last, confident

    # ---- template generation --------------------------------------------

    def generate_template(self, *, goal: str, audience: str = "", tone: str = "",
                          max_chars: int | None = 300, existing: str = "",
                          sender: str = "", voice: str = "auto",
                          api_key: str | None = None, model_override: str | None = None) -> str:
        """Generate (or improve) a reusable template body WITH placeholders."""
        limit = (f"Keep it strictly under {max_chars} characters (LinkedIn note limit)."
                 if max_chars else "There is no length limit, but stay concise.")
        improve = (f"\nImprove this existing draft while keeping its intent:\n\"\"\"\n{existing}\n\"\"\""
                   if existing.strip() else "")
        sender_line = f"The sender's name is {sender}. " if sender else ""
        tone_line = f"Extra tone guidance: {tone}\n" if tone else ""
        prompt = (
            "Write a concise, high-reply-rate LinkedIn outbound message TEMPLATE.\n"
            f"Goal: {goal}\n"
            f"Audience: {audience or 'relevant professionals'}\n"
            f"{sender_line}\n"
            f"{tone_line}"
            "Rules:\n"
            "- Use placeholders for personalization: {first_name}, {company}, {role}, {sender}. "
            "Always begin with 'Hi {first_name},'.\n"
            f"- {limit}\n"
            "- One short paragraph. No hashtags, no links.\n"
            f"{improve}\n"
            "Return ONLY the message text."
        )
        system = voicelib.build_system_prompt(voice, "linkedin")
        return self.generate_text(prompt, model=model_override or self.cfg.model, system=system, api_key=api_key)

    def tailor_message(self, *, base: str, context: dict, tone: str = "",
                       max_chars: int | None = 300, sender: str = "",
                       is_note: bool = True, voice: str = "auto",
                       api_key: str | None = None, model_override: str | None = None) -> str:
        """Personalize a message for ONE person. Returns ready-to-send text (no
        placeholders left)."""
        first = context.get("first_name") or ""
        name = context.get("full_name") or first
        headline = context.get("headline") or ""
        company = context.get("company") or ""
        role = context.get("role") or ""
        location = context.get("location") or ""
        kind = "connection-request note" if is_note else "direct message"
        limit = (f"Strictly under {max_chars} characters." if max_chars
                 else "Concise (a few short sentences).")
        details = "; ".join(
            p for p in [
                f"name: {name}" if name else "",
                f"headline: {headline}" if headline else "",
                f"role: {role}" if role else "",
                f"company: {company}" if company else "",
                f"location: {location}" if location else "",
            ] if p
        ) or "no extra details available"
        prompt = (
            f"Personalize this LinkedIn {kind} for a specific person.\n"
            f"Person details: {details}\n"
            f"Sender: {sender or 'the sender'}\n"
            f"Intent / base message:\n\"\"\"\n{base}\n\"\"\"\n"
            "Rules:\n"
            f"- Begin with 'Hi {first or '[first name]'},'.\n"
            "- Reference their role/company naturally when it strengthens relevance; "
            "do not be generic or fawning.\n"
            "- NO unfilled placeholders, no links, no hashtags.\n"
            f"- {tone + '. ' if tone else ''}{limit}\n"
            "Return ONLY the message text."
        )
        system = voicelib.build_system_prompt(voice, "linkedin")
        return self.generate_text(prompt, model=model_override or self.cfg.model, system=system, api_key=api_key)

    def review_message(self, text: str, api_key: str | None = None, model_override: str | None = None) -> dict:
        """Anti-AI review: flag AI tells / banned patterns and suggest a fix."""
        system = voicelib.build_system_prompt("auto", "linkedin")
        prompt = (
            "Review this outbound message against the hard writing rules. "
            "Identify concrete violations (em dashes, 'not just X but Y', self-answering "
            "questions, banned vocabulary, filler phrases, forced triads, hype, vagueness). "
            "Then provide one improved rewrite that fixes them while preserving intent and length.\n\n"
            f"Message:\n\"\"\"\n{text}\n\"\"\"\n\n"
            'Return strict JSON: {"score": 0-100 (higher = more human/on-voice), '
            '"issues": [string], "rewrite": string}'
        )
        return self.generate_json(prompt, model=model_override or self.cfg.model, system=system, api_key=api_key)


def _safe_json(raw: str) -> dict:
    """Parse JSON that may be wrapped in markdown fences or have stray text."""
    if not raw:
        return {}
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if "\n" in s:
            s = s.split("\n", 1)[1]
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(s[start:end + 1])
            except json.JSONDecodeError:
                return {}
    return {}
