#!/usr/bin/env bash
# macOS launcher: double-click in Finder, or run  ./start.command
# Sets up (first time) and starts the LinkedIn Outbound dashboard.
cd "$(dirname "$0")" || exit 1

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done

if [ -z "$PY" ]; then
  echo "Python was not found. Install Python 3.10+ from https://www.python.org/downloads/ then re-run."
  exit 1
fi

"$PY" bootstrap.py
