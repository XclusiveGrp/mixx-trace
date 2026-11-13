#!/usr/bin/env python3
"""TuneTrace - feedback finder, system tuning and channel advice for live sound.

Run:  python3 tunetrace.py          (opens the interface in your browser)
"""
import argparse
import sys
import threading
import webbrowser

if sys.version_info < (3, 9):
    sys.exit("TuneTrace needs Python 3.9 or newer.")

try:
    import numpy  # noqa: F401
except ImportError:
    sys.exit("TuneTrace needs numpy. Run:  python3 -m pip install -r requirements.txt")

from tt import __version__
from tt.app import App
from tt.engine import SD_ERROR, Engine
from tt.server import serve


def main():
    ap = argparse.ArgumentParser(description="TuneTrace %s" % __version__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--demo", action="store_true", help="start with the built-in demo signal")
    ap.add_argument("--demo-only", action="store_true", help="hosted preview: demo signal only, no audio hardware")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--list-devices", action="store_true")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    if a.list_devices:
        d = Engine.list_devices()
        if d["error"]:
            sys.exit("Audio is unavailable: %s" % d["error"])
        for i in d["inputs"]:
            print("in  [%2d] %-48s %3d ch  %s  %.0f Hz" % (i["id"], i["name"][:48], i["ch"], i["api"], i["sr"]))
        for o in d["outputs"]:
            print("out [%2d] %-48s %3d ch  %s  %.0f Hz" % (o["id"], o["name"][:48], o["ch"], o["api"], o["sr"]))
        return

    app = App(demo_only=a.demo_only)
    host = "0.0.0.0" if a.demo_only else "127.0.0.1"
    try:
        httpd = serve(app, host, a.port)
    except OSError as exc:
        sys.exit("Could not open port %d (%s). Is TuneTrace already running? Try --port 8766." % (a.port, exc))
    url = "http://127.0.0.1:%d/" % a.port
    print("TuneTrace %s is running at %s" % (__version__, url))
    if SD_ERROR and not a.demo_only:
        print("Live input is unavailable (%s). The demo signal still works." % SD_ERROR)
        print("Fix:  python3 -m pip install sounddevice")
    print("Press Ctrl+C to quit.")
    if a.demo or a.demo_only:
        app.start_demo()
    if not a.no_browser and not a.demo_only:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
