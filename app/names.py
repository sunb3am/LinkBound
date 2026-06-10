"""Best-effort name extraction.

Two sources, in order of reliability:
  1. The CSV/operator-provided name (trusted).
  2. The LinkedIn URL slug (a guess, e.g. /in/jane-doe-8a1b2 -> "Jane Doe").

At send time the runner reads the real profile name from the page (the most
reliable source) and, when the row's name was only a URL guess (or missing),
re-renders the message from the captured name. So slug guessing only needs to be
"good enough" for the preview; the live page corrects it.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_HAS_DIGIT = re.compile(r"\d")


def slug_from_url(url: str) -> str:
    """Return the profile slug (the path segment after /in/)."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    path = urlsplit(raw).path or ""
    parts = [p for p in path.split("/") if p]
    if "in" in parts:
        i = parts.index("in")
        if i + 1 < len(parts):
            return parts[i + 1]
    return parts[-1] if parts else ""


def guess_name_from_url(url: str) -> tuple[str, str]:
    """Guess (first_name, last_name) from the URL slug.

    Drops id-like tokens (anything containing a digit), splits on - and _, and
    title-cases the rest. Returns ("", "") when nothing usable is found.
    """
    slug = slug_from_url(url)
    if not slug:
        return ("", "")
    tokens = [t for t in re.split(r"[-_]+", slug) if t]
    name_tokens = [t for t in tokens if not _HAS_DIGIT.search(t)]
    if not name_tokens:
        return ("", "")
    titled = [_titlecase(t) for t in name_tokens]
    first = titled[0]
    last = " ".join(titled[1:]) if len(titled) > 1 else ""
    return (first, last)


def split_full_name(full_name: str) -> tuple[str, str]:
    """Split a full name into (first, last). The first token is the first name."""
    parts = (full_name or "").split()
    if not parts:
        return ("", "")
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[0], " ".join(parts[1:]))


def _titlecase(token: str) -> str:
    # Preserve simple intra-token capitalization for names like "McName" only
    # loosely; a plain capitalize is good enough for a preview guess.
    return token[:1].upper() + token[1:].lower() if token else token
