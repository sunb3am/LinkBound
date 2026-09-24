"""Run a bounded inbox pilot while the LinkBound app service is stopped."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys

from app import db, inbound_store
from app.coordinator import RunCoordinator
from app.inbox_sync import scan_account
from app.settings import OperatorConfig, load_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a no-send LinkedIn inbox pilot")
    parser.add_argument("--operator", required=True)
    parser.add_argument("--max-rows-per-folder", type=int, default=2)
    parser.add_argument("--max-list-rows", type=int, default=500)
    args = parser.parse_args()
    if sys.platform.startswith("linux") and subprocess.run(
        ["systemctl", "is-active", "--quiet", "linkbound-app"], check=False
    ).returncode == 0:
        parser.error("Stop linkbound-app before a standalone browser pilot")
    settings = load_settings()
    db.init_db(settings.data_dir / "outbound.db")
    try:
        inbound_store.mark_interrupted_runs()
        settings.operators = {
            row["key"]: OperatorConfig(
                key=row["key"], label=row["label"], profile_dir=row["profile_dir"]
            )
            for row in db.list_operators()
        }
        result = asyncio.run(scan_account(
            settings, RunCoordinator(settings, None), args.operator,
            max_rows_per_folder=args.max_rows_per_folder,
            max_list_rows=args.max_list_rows,
        ))
        print(json.dumps(result, sort_keys=True))
        return 1 if result["stopped"] else 0
    finally:
        db.close_db()


if __name__ == "__main__":
    raise SystemExit(main())
