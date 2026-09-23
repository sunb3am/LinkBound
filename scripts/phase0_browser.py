"""No-send headed Chrome pilot on the server's Xvnc display.

Run twice against the same persistent profile to verify that a manual login
survives closing and reopening Chrome. This script never calls process() or any
of the runner's send methods.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from app.runner import LinkedInRunner
from app.settings import ROOT, load_settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _absolute_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("use an absolute path outside the code release")
    path = path.resolve()
    if path.is_relative_to(ROOT.resolve()):
        raise argparse.ArgumentTypeError("path must be outside the code release")
    return path


async def run(operator: str, profile_dir: Path, evidence_dir: Path, wait_seconds: int) -> Path:
    if not os.environ.get("DISPLAY"):
        raise RuntimeError("DISPLAY is unset; start the Xvnc display first")

    settings = load_settings()
    if settings.browser.headless or settings.browser.channel != "chrome" or settings.browser.cdp_url:
        raise RuntimeError("pilot requires headed local Google Chrome with no CDP URL")

    settings.operator(operator).profile_dir = str(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    result: dict[str, object] = {
        "started_at": _now(),
        "operator": operator,
        "display": os.environ["DISPLAY"],
        "profile_dir": str(profile_dir),
        "action": "feed_navigation_only",
        "wait_seconds": wait_seconds,
    }
    runner = LinkedInRunner(settings, operator)
    try:
        await runner.start()
        await runner.open_feed()
        result["initial_login_check"] = await runner.logged_in_now()
        print("Chrome is open on the Xvnc display. Use noVNC to log in if needed.", flush=True)
        print(f"Waiting {wait_seconds} seconds before checking the feed again.", flush=True)
        await asyncio.sleep(wait_seconds)
        await runner.open_feed()
        result["final_login_check"] = await runner.logged_in_now()
        result["outcome"] = "completed"
    except BaseException as exc:
        result["outcome"] = "interrupted" if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            await runner.close()
        finally:
            result["finished_at"] = _now()
            destination = evidence_dir / f"phase0-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
            destination.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print(f"Pilot evidence: {destination}", flush=True)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator", default="me", help="sender key from config.yaml")
    parser.add_argument("--profile-dir", type=_absolute_path, required=True)
    parser.add_argument("--evidence-dir", type=_absolute_path, required=True)
    parser.add_argument("--wait-seconds", type=int, default=900)
    args = parser.parse_args()
    if args.wait_seconds < 1:
        parser.error("--wait-seconds must be positive")
    asyncio.run(run(args.operator, args.profile_dir, args.evidence_dir, args.wait_seconds))


if __name__ == "__main__":
    main()
