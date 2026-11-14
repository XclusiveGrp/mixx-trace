#!/bin/bash
# Mixx Trace launcher for macOS (also works on Linux). Double-click it, or run it from Terminal.
cd "$(dirname "$0")" || exit 1

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3 was not found."
  echo "Install it from https://www.python.org/downloads/ and run this file again."
  read -r -p "Press Return to close."
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up Mixx Trace (about a minute, needs internet once)..."
  "$PY" -m venv .venv || { echo "Could not create the Python environment."; read -r -p "Press Return to close."; exit 1; }
  .venv/bin/python -m pip install --quiet --upgrade pip
  if ! .venv/bin/python -m pip install --quiet -r requirements.txt; then
    echo "Installing the requirements failed. Check the internet connection and run this file again."
    rm -rf .venv
    read -r -p "Press Return to close."
    exit 1
  fi
fi

echo "Starting Mixx Trace. Leave this window open while you work; close it to quit."
exec .venv/bin/python mixxtrace.py "$@"
