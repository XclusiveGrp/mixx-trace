#!/bin/bash
# Builds "Mixx Trace.app" on this Mac. Run it from Terminal:  bash build-mac-app.command
# (or double-click it). It needs internet the first time and takes a few minutes.
cd "$(dirname "$0")" || exit 1

APP="dist/Mixx Trace.app"
LOG="build-mac-app.log"

finish() {   # finish <exit code> <message>
  echo
  echo "$2"
  if [ -z "$MIXXTRACE_BUILD_NO_PROMPT" ]; then read -r -p "Press Return to close."; fi
  exit "$1"
}

if [ "$(uname -s)" != "Darwin" ]; then
  finish 1 "This script builds the Mac app, so it has to run on a Mac."
fi

# Newest well-supported Python first; the plain python3 from Apple's developer tools is the last resort.
PY=""
for c in python3.14 python3.13 python3.15 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  finish 1 "Python 3 was not found. Install it from https://www.python.org/downloads/ and run this again."
fi
echo "Building Mixx Trace.app with $("$PY" --version 2>&1) on $(uname -m). Full details go to $LOG."
: > "$LOG"

echo "1/4  Getting the build tools (needs internet the first time)..."
if [ ! -x .venv-build/bin/python ]; then
  "$PY" -m venv .venv-build >>"$LOG" 2>&1 || finish 1 "Could not create the build environment. See $LOG."
fi
.venv-build/bin/python -m pip install --quiet --upgrade pip >>"$LOG" 2>&1
if ! .venv-build/bin/python -m pip install --quiet -r requirements.txt -r packaging/mac/requirements-app.txt >>"$LOG" 2>&1; then
  tail -n 15 "$LOG"
  rm -rf .venv-build
  finish 1 "Installing the build tools failed. Check the internet connection and run this again. Details: $LOG"
fi

echo "2/4  Building the app (a few minutes)..."
rm -rf build dist
if ! .venv-build/bin/python -m PyInstaller --noconfirm --clean --log-level WARN \
      --distpath dist --workpath build packaging/mac/MixxTrace.spec >>"$LOG" 2>&1 || [ ! -d "$APP" ]; then
  tail -n 25 "$LOG"
  finish 1 "The build failed. Send the file $LOG so it can be fixed."
fi

echo "3/4  Checking the app..."
# macOS only lets an app use the microphone if it is signed; a local (ad-hoc) signature is enough.
if ! codesign --verify --deep --strict "$APP" >>"$LOG" 2>&1; then
  codesign --force --deep --sign - "$APP" >>"$LOG" 2>&1 || echo "     Note: could not sign the app (see $LOG)."
fi
xattr -dr com.apple.quarantine "$APP" 2>/dev/null
"$APP/Contents/MacOS/Mixx Trace" --self-test 2>&1 | tee -a "$LOG"
if [ "${PIPESTATUS[0]}" -ne 0 ]; then
  finish 1 "The app was built but did not pass its own check. Send the file $LOG so it can be fixed."
fi

echo "4/4  Done: $APP"
DEST="/Applications/Mixx Trace.app"
ANSWER="y"
if [ -z "$MIXXTRACE_BUILD_NO_PROMPT" ]; then
  read -r -p "Put Mixx Trace in your Applications folder? [Y/n] " ANSWER
fi
case "$ANSWER" in
  n|N|no|No|NO)
    open -R "$APP"
    finish 0 "Mixx Trace.app is in the dist folder (shown in Finder). Double-click it to start."
    ;;
esac
if pgrep -f "$DEST/Contents/MacOS/" >/dev/null 2>&1; then
  osascript -e 'tell application "Mixx Trace" to quit' >/dev/null 2>&1
  sleep 2
fi
if rm -rf "$DEST" 2>>"$LOG" && ditto "$APP" "$DEST" 2>>"$LOG"; then
  open -R "$DEST"
  finish 0 "Mixx Trace is in your Applications folder (shown in Finder). Double-click it to start, then keep it in the Dock if you like."
fi
open -R "$APP"
finish 0 "Could not copy into Applications, so Mixx Trace.app is in the dist folder (shown in Finder). Drag it to Applications yourself."
