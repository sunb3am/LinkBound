"""Entrypoint: launches the LinkBound dashboard + API server.

Usage:
    python run.py

Then open the printed URL (default http://127.0.0.1:8000).
"""

from __future__ import annotations

import uvicorn

from app.settings import load_settings


def main() -> None:
    settings = load_settings()
    host = settings.server.host
    port = settings.server.port
    print("=" * 60)
    print("  LinkBound · LinkedIn Outbound Automation")
    print(f"  Dashboard: http://{host}:{port}")
    print("  First run per operator: log into LinkedIn in the browser")
    print("  window that opens, complete 2FA, then start the run.")
    print("=" * 60)
    # Import string enables clean shutdown; reload off for stable browser state.
    uvicorn.run("app.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
