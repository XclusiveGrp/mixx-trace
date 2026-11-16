#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Xclusive Audio Visual Group (XAVGROUP)
"""Mixx Trace as a desktop app: the same engine, shown in its own window instead of a browser tab.

This is what the Mac app starts. It can also be run by hand:  python3 mixxtrace_app.py
(needs:  python3 -m pip install -r requirements.txt -r packaging/mac/requirements-app.txt)
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser

FROZEN = bool(getattr(sys, "frozen", False))
HOME_DIR = os.path.join(os.path.expanduser("~"), ".mixxtrace")
LOG = os.path.join(HOME_DIR, "app.log")
PORT = 8765
WINDOW = {"width": 1280, "height": 820, "min_size": (960, 620), "background_color": "#0D0F12", "text_select": True}


def log_to_file():
    """A double-clicked app has no Terminal, so keep what it prints where it can be found later."""
    try:
        os.makedirs(HOME_DIR, exist_ok=True)
        if os.path.exists(LOG) and os.path.getsize(LOG) > 512 * 1024:
            os.replace(LOG, LOG + ".old")
        fh = open(LOG, "a", encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = sys.stderr = fh
        print("\n--- %s  started ---" % time.strftime("%Y-%m-%d %H:%M:%S"))
    except OSError:
        pass


def tell(message, button="OK"):
    """A plain system dialog that works even when the window could not be made. Waits for the click."""
    print(message)
    if sys.platform != "darwin":
        return
    q = lambda t: json.dumps(str(t), ensure_ascii=False)          # AppleScript strings quote the same way
    script = "display dialog %s with title %s buttons {%s} default button 1" % (q(message), q("Mixx Trace"), q(button))
    try:
        subprocess.run(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def stay_awake():
    """Mac: ask the system not to slow the app down, or sleep, while it measures behind another window."""
    if sys.platform != "darwin":
        return None
    try:
        import Foundation
        return Foundation.NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            Foundation.NSActivityUserInitiated, "Live audio measurement")
    except Exception:
        traceback.print_exc()
        return None


def start_server(app, port):
    """Serve on the usual port, or on any free one when the Terminal version is already using it."""
    from tt.server import serve
    try:
        httpd = serve(app, "127.0.0.1", port)
    except OSError:
        httpd = serve(app, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    return httpd, "http://127.0.0.1:%d/" % httpd.server_address[1]


def show_window(url):
    """Open the window and block until it is closed. Returns False when a window is not possible here."""
    try:
        import webview
        webview.settings["ALLOW_DOWNLOADS"] = True                # reports and sessions; the app also saves them itself
        webview.create_window("Mixx Trace", url, **WINDOW)
        webview.start()
        return True
    except Exception:
        traceback.print_exc()
        return False


def self_test():
    """Used by the build script: prove that everything the app needs made it into the bundle."""
    from tt import __version__
    from tt.app import App
    from tt import engine
    ok = True

    def check(name, good, detail=""):
        nonlocal ok
        ok = ok and bool(good)
        print("  %-4s %s%s" % ("ok" if good else "FAIL", name, (": " + str(detail)) if detail else ""))

    print("Mixx Trace %s self-test (%s, Python %s)" % (__version__, sys.platform, sys.version.split()[0]))
    app = App(persist=False, native=True)
    httpd, url = start_server(app, 0)
    try:
        page = urllib.request.urlopen(url, timeout=5).read().decode("utf-8")
        check("interface files", "Mixx Trace" in page and "app.js" in page)
        for f in ("app.js", "plot.js", "style.css", "base.css", "icon.svg"):
            check("  " + f, len(urllib.request.urlopen(url + f, timeout=5).read()) > 100)
        st = json.loads(urllib.request.urlopen(url + "api/static", timeout=5).read())
        check("engine answers", st.get("version") == __version__ and st.get("native") is True)
        app.start_demo()
        time.sleep(1.5)
        fr = json.loads(urllib.request.urlopen(url + "api/frame?view=feedback", timeout=5).read())
        check("demo signal runs", fr["status"]["mode"] == "demo" and fr.get("rta"))
    except Exception as exc:
        check("local server", False, exc)
    finally:
        app.stop()
        httpd.shutdown()
        httpd.server_close()
    check("audio input (sounddevice)", engine.SD_ERROR is None, engine.SD_ERROR or "")
    if engine.SD_ERROR is None:
        try:
            d = engine.Engine.list_devices()
            check("audio devices listed", not d["error"], d["error"] or "%d inputs, %d outputs" % (len(d["inputs"]), len(d["outputs"])))
        except Exception as exc:
            check("audio devices listed", False, exc)
    try:
        import webview
        if sys.platform == "darwin":
            import webview.platforms.cocoa  # noqa: F401  (pulls in the macOS window parts)
        check("app window (pywebview)", True, getattr(webview, "__version__", ""))
    except Exception as exc:
        check("app window (pywebview)", False, exc)
    print("RESULT: %s" % ("all good" if ok else "something is missing, see FAIL above"))
    return 0 if ok else 1


def main(argv=None, hard_exit=False):
    ap = argparse.ArgumentParser(description="Mixx Trace desktop app")
    ap.add_argument("--demo", action="store_true", help="start with the built-in demo signal")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-window", action="store_true", help="run the engine only (for testing)")
    ap.add_argument("--self-test", action="store_true", help="check the bundle and exit")
    a, _ = ap.parse_known_args(argv)        # macOS may pass its own arguments to a double-clicked app

    if a.self_test:
        return self_test()
    if FROZEN or sys.stdout is None:
        log_to_file()

    try:
        from tt import __version__
        from tt.app import App
        app = App(native=True)
        httpd, url = start_server(app, a.port)
    except Exception:
        traceback.print_exc()
        tell("Mixx Trace could not start. The details are in %s" % LOG)
        return 1
    print("Mixx Trace %s desktop app at %s" % (__version__, url))
    awake = stay_awake()                    # keep the reference: the request ends when it is dropped
    if a.demo:
        app.start_demo()

    try:
        if a.no_window or os.environ.get("MIXXTRACE_NO_WINDOW"):
            print("Engine only. Press Ctrl+C to quit.")
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
        elif not show_window(url):
            # Never leave an engine running with no way to see or stop it.
            webbrowser.open(url)
            tell("Mixx Trace could not open its own window, so it opened in your browser instead.\n\n"
                 "Leave this message up while you work. Click Quit when you are done.", "Quit")
            if sys.platform != "darwin":
                try:
                    while True:
                        time.sleep(3600)
                except KeyboardInterrupt:
                    pass
    finally:
        if hard_exit:                                    # a stuck audio driver must not keep the Dock icon alive
            t = threading.Timer(4.0, os._exit, [0])
            t.daemon = True
            t.start()
        try:
            app.stop()
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            traceback.print_exc()
    del awake
    return 0


if __name__ == "__main__":
    code = main(hard_exit=True)
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(code or 0)
