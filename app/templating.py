"""Renders templates with row variables and enforces LinkedIn's limits.

The 300-char limit only applies to connection-request NOTES. Direct messages and
InMail bodies are not capped, so the caller passes the appropriate ``note_limit``
based on the action type (None = no limit).
"""

from __future__ import annotations

import re
import string

LINKEDIN_NOTE_LIMIT = 300

_FIELD_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")


class _SafeMissing(dict):
    """Tracks which keys were requested but missing during format_map."""

    def __init__(self, data: dict[str, str]):
        super().__init__(data)
        self.missing: set[str] = set()

    def __missing__(self, key: str) -> str:
        self.missing.add(key)
        return "{" + key + "}"


def referenced_variables(template_text: str) -> set[str]:
    return set(_FIELD_RE.findall(template_text))


def render(
    template_text: str,
    variables: dict[str, str],
    note_limit: int | None = LINKEDIN_NOTE_LIMIT,
) -> tuple[str, list[str]]:
    """Render a template. Returns (message, issues).

    issues is empty when the message is send-ready. Possible issues:
      - "missing variable: X" when a referenced field has no/blank value
      - "exceeds N chars (M)" when note_limit is set and the result is too long
    """
    issues: list[str] = []

    needed = referenced_variables(template_text)
    for var in sorted(needed):
        value = variables.get(var, "")
        if value is None or str(value).strip() == "":
            issues.append(f"missing variable: {var}")

    mapping = _SafeMissing({k: str(v) for k, v in variables.items() if v is not None})
    try:
        rendered = string.Formatter().vformat(template_text, (), mapping)
    except (ValueError, IndexError) as exc:
        return "", [f"template error: {exc}"]

    for key in mapping.missing:
        msg = f"missing variable: {key}"
        if msg not in issues:
            issues.append(msg)

    rendered = rendered.strip()
    if note_limit is not None and len(rendered) > note_limit:
        issues.append(f"exceeds {note_limit} chars ({len(rendered)})")

    return rendered, issues
