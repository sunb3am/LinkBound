"""Print a non-secret snapshot of the headed browser host for release comparison."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import locale
import os
import platform
import pwd
import stat
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def _command(*args: str) -> str | None:
    try:
        return subprocess.check_output(args, text=True, timeout=5).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _egress_ip() -> str | None:
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as response:
            return response.read(80).decode("ascii").strip()
    except (OSError, UnicodeError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--challenge", default="not_observed",
                        choices=("not_observed", "observed", "unknown"))
    args = parser.parse_args()
    profile = args.profile.resolve()
    info = profile.stat()
    result = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "account": args.account,
        "challenge": args.challenge,
        "host": platform.node(),
        "os": platform.platform(),
        "chrome": _command("google-chrome", "--version"),
        "playwright": importlib.metadata.version("playwright"),
        "display": os.environ.get("DISPLAY"),
        "timezone": time.tzname[0],
        "locale": locale.setlocale(locale.LC_ALL, None),
        "egress_ip": _egress_ip(),
        "profile": str(profile),
        "profile_owner": pwd.getpwuid(info.st_uid).pw_name,
        "profile_mode": oct(stat.S_IMODE(info.st_mode)),
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
