#!/usr/bin/env sh
# First run creates a virtualenv and installs Flask; then starts the app.
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
exec .venv/bin/python app.py "$@"
