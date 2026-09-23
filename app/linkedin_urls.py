"""Validate profile targets before they enter the unattended browser queue."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def canonical_profile_url(raw: str) -> str:
    value = (raw or "").strip()
    parsed = urlsplit(value if "://" in value else "https://" + value)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Queue target has an invalid URL port.") from exc
    if (parsed.scheme.lower() != "https" or host not in {"linkedin.com", "www.linkedin.com"}
            or parsed.username or parsed.password or port is not None
            or not (path.startswith("/in/") or path.startswith("/pub/"))
            or path in {"/in", "/pub"}):
        raise ValueError("Queue targets must be LinkedIn profile URLs under /in/ or /pub/.")
    return urlunsplit(("https", "www.linkedin.com", path, "", ""))
