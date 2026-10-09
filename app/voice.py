"""LinkBound voice system for AI content.

Bundles outreach writing guidance (Anti-AI Writing Bible distillation plus
reusable voice profiles) into a compact system instruction that every AI
content call uses. The result: generated outreach sounds like a real human,
not like a generic LLM.

Four built-in voices:
  professional  – Clean, direct, confident B2B tone.
  founder       – Warm, curious, specific. Founder-to-founder.
  casual        – Conversational, friendly, low-friction networking.
  auto          – Blends founder warmth with professional clarity.

Users may also create custom voice profiles stored in the DB.
"""

from __future__ import annotations

import functools
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUIDES_DIR = ROOT / "guides"

# The hard rules that apply to every voice (distilled Anti-AI Bible).
ANTI_AI_RULES = """\
Hard writing rules (always apply):
- Never use em dashes. Use commas, colons, periods, or parentheses.
- Never use the "not just X, but Y" / "not only X" / "more than just X" contrast template, in any framing (including negative "not another X").
- Never pose a question and immediately answer it ("Why does this matter? Because...").
- Avoid the rule of three / forced triads ("fast, fair, and human"). Vary groupings: sometimes one thing, sometimes two or four.
- Avoid mirrored/parallel sentence structures stacked back to back.
- No emojis in the message body (a single trailing smiley is acceptable only for very warm, casual notes).
- No filler phrases: "it's important to note", "in today's world", "at the end of the day", "the reality is", "let's be honest", "this is where X comes in".
- Banned AI vocabulary: delve, leverage, utilize, navigate (metaphorical), landscape, paradigm, synergy, ecosystem, robust, seamless, groundbreaking, harness, revolutionize, streamline, holistic, foster, resonate, underscore, moreover, furthermore, pivotal, elevate, unlock, empower, supercharge, game-changer, cutting-edge, best-in-class, disrupt, democratize.
- Vary sentence length deliberately. Mix short with medium. Never a metronomic staccato rhythm.
- Be specific and concrete. Real claims, real numbers, real details. No vague universals ("everyone knows"), no vague attribution ("studies show").
- No performative enthusiasm ("we're thrilled to..."), no inspirational closes ("together we can reshape the future"), no faux humility.
- Write like you're talking to a smart person over coffee, not presenting a slide deck. Say the thing, say it well, stop."""

PROFESSIONAL_VOICE = """\
Voice: Professional – direct, clean B2B outreach.
- Open with something relevant to the recipient, not a generic line. Reference their work, company, or a specific detail.
- Confident without overselling. Every claim should be verifiable. No superlatives or hype.
- The ask is specific and low-friction ("open to 15 min this week?"), never "let's connect sometime" or "I'd love to pick your brain".
- Keep the tone measured and respectful. No exclamation marks unless genuinely warranted.
- Lead with the value to them, not what you need."""

FOUNDER_VOICE = """\
Voice: Founder – warm, curious, authentic founder-to-founder.
- Warm AND direct at the same time. No "I hope this finds you well." Open with something specific to THIS person or situation, never a generic line.
- Genuinely curious about the recipient. Tell them WHY their specific perspective or work is interesting, not just "you're experienced".
- Community-first and generous: lead with value where natural (a relevant intro, a genuine "I'd love your read on X").
- Grounded in specifics: names, numbers, real experiences. Admits it's hard, doing it anyway.
- The ask is specific and low-friction ("open to a quick 15 min next week?"), never "let's connect sometime".
- Signature texture: natural enthusiasm, builder identity, conversational. Confident builder, not corporate."""

CASUAL_VOICE = """\
Voice: Casual – friendly, conversational networking.
- Write like you'd text a professional friend. Relaxed but still respectful.
- Short sentences. One idea at a time. Easy to read on mobile.
- Lead with genuine interest or a shared connection. No formality.
- The ask should feel effortless: "always great to meet people building cool stuff. coffee sometime?"
- It's okay to be slightly playful, but don't try too hard. Authenticity over cleverness."""


@functools.lru_cache(maxsize=8)
def _file_extract(name: str, max_chars: int = 2600) -> str:
    """Load a condensed extract of a writing guide if present on disk."""
    path = GUIDES_DIR / name
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ""
    return text[:max_chars]


def available_voices() -> list[dict]:
    return [
        {"key": "auto", "label": "Auto (balanced outreach)"},
        {"key": "professional", "label": "Professional (B2B)"},
        {"key": "founder", "label": "Founder (warm & curious)"},
        {"key": "casual", "label": "Casual (networking)"},
    ]


_VOICE_MAP = {
    "professional": PROFESSIONAL_VOICE,
    "founder": FOUNDER_VOICE,
    "casual": CASUAL_VOICE,
}


@functools.lru_cache(maxsize=16)
def build_system_prompt(voice: str = "auto", surface: str = "linkedin") -> str:
    """Compose the system instruction for a given voice + surface."""
    voice = (voice or "auto").lower()
    parts: list[str] = [
        "You write outbound messages for LinkedIn. Follow the voice instructions and the hard writing rules exactly. Output ONLY the message text unless asked otherwise.",
        ANTI_AI_RULES,
    ]
    if voice in _VOICE_MAP:
        parts.append(_VOICE_MAP[voice])
    else:  # auto: blend founder warmth with professional clarity
        parts.append(FOUNDER_VOICE)
        parts.append("Blend: lead with the founder's personal warmth and curiosity; keep the professional voice's directness and structure for the ask.")

    if surface == "linkedin":
        parts.append(
            "Surface: LinkedIn outbound. Keep it tight and human. A connection-request note must be a single short paragraph. "
            "Open with the person's first name. One specific reason for reaching out, one line on what you do only if it adds relevance, one low-friction ask."
        )

    # Optional grounding from the on-disk guides (kept short to control tokens).
    guide = _file_extract("00_outreach_voice.md", 1500)
    if guide:
        parts.append("Reference excerpt (Voice Guide):\n" + guide)

    return "\n\n".join(p for p in parts if p)
