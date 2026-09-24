"""Fail-closed checks for visible LinkedIn account stops in the headed browser."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class LinkedInStop:
    kind: str
    detail: str


class LinkedInAccountStop(RuntimeError):
    """A visible account warning stopped navigation before the run began."""


def classify_page(url: str, visible_text: str = "") -> LinkedInStop | None:
    parsed = urlsplit(url or "")
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return LinkedInStop("browser", "Browser left LinkedIn; inspect the session")
    if any(part in path for part in ("/checkpoint", "/captcha", "/security/verification")):
        return LinkedInStop("challenge", "LinkedIn security challenge requires manual review")
    if any(part in path for part in ("/login", "/signup", "/authwall", "/uas/login")):
        return LinkedInStop("login", "LinkedIn login is required")

    text = " ".join((visible_text or "").casefold().split())
    if any(phrase in text for phrase in (
        "your account has been restricted", "your account is restricted",
        "we've restricted your account", "restricted action message",
        "we detected unusual activity", "we've noticed unusual activity",
    )):
        return LinkedInStop("restriction", "LinkedIn restriction notice requires manual review")
    if any(phrase in text for phrase in (
        "weekly invitation limit", "reached the monthly limit",
        "you have reached the limit", "profile viewing limit",
        "you've reached your profile viewing limit",
    )):
        return LinkedInStop("limit", "LinkedIn activity limit requires manual review")
    if any(phrase in text for phrase in (
        "security verification", "verify your identity", "complete a security check",
        "quick security check", "complete the captcha",
    )):
        return LinkedInStop("challenge", "LinkedIn security challenge requires manual review")
    return None


async def inspect_page(page) -> LinkedInStop | None:
    url_stop = classify_page(page.url)
    if url_stop is not None:
        return url_stop
    try:
        visible_text = await page.evaluate(
            "() => document.body ? document.body.innerText.slice(0, 30000) : ''"
        )
    except Exception:
        return LinkedInStop("browser", "Could not inspect the LinkedIn page; stop this run")
    return classify_page(page.url, visible_text)
