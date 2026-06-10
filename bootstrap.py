"""One-command setup + launch for the LinkedIn Outbound tool.

Cross-platform (Windows / macOS / Linux). Run it with your SYSTEM Python:

    python bootstrap.py            # set up (first time) and start the dashboard
    python bootstrap.py --setup    # only set up, don't start

It creates a local virtual environment (.venv), installs dependencies, installs
a browser for Playwright, and then launches the dashboard. Subsequent runs skip
the install step and start immediately.
"""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
DEPS_MARKER = VENV_DIR / ".deps_ok"


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def run(cmd: list[str]) -> None:
    print("+", " ".join(str(c) for c in cmd))
    subprocess.check_call(cmd)


def ensure_setup() -> None:
    py = venv_python()
    if not py.exists():
        print("Creating virtual environment (.venv)...")
        venv.create(str(VENV_DIR), with_pip=True)

    if DEPS_MARKER.exists():
        return

    run([str(py), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(py), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])

    # Browser for Playwright. Prefer the installed Google Chrome channel (the
    # default in config.yaml); also fetch bundled Chromium as a fallback so the
    # tool works even on machines without Chrome.
    try:
        run([str(py), "-m", "playwright", "install", "chrome"])
    except subprocess.CalledProcessError:
        print("Note: could not install the Google Chrome channel.")
        print("      The bundled Chromium will be used instead. If the browser")
        print('      fails to launch, set  browser.channel: "chromium"  in config.yaml.')
    try:
        run([str(py), "-m", "playwright", "install", "chromium"])
    except subprocess.CalledProcessError:
        print("Warning: Chromium install failed; ensure a browser is available.")

    DEPS_MARKER.write_text("ok\n", encoding="utf-8")


def main() -> None:
    ensure_setup()
    if "--setup" in sys.argv:
        print("\nSetup complete. Start the dashboard with:\n    python bootstrap.py")
        return
    print("\nStarting the dashboard (Ctrl+C to stop)...\n")
    run([str(venv_python()), str(ROOT / "run.py")])


if __name__ == "__main__":
    main()
