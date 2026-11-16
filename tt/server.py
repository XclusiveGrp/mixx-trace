# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Xclusive Audio Visual Group (XAVGROUP)
"""Small HTTP server: serves the interface and a JSON API. Standard library only."""
import json
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("image/svg+xml", ".svg")


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "MixxTrace"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, separators=(",", ":"), allow_nan=False)
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype == "application/json" else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if app.demo_only:                      # hosted preview sits behind a proxy on another origin
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _local_only(self):
            """Refuse cross-site pages poking at the local API."""
            if app.demo_only:
                return True
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost", "[::1]"):
                return False
            origin = self.headers.get("Origin")      # browsers add this when another site's page makes the request
            if origin is None:
                return True
            try:
                return urlparse(origin).hostname in ("127.0.0.1", "localhost", "::1")
            except ValueError:
                return False

        def do_OPTIONS(self):
            self._send(204, b"")

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            p = u.path
            try:
                if p.startswith("/api/"):
                    if not self._local_only():
                        return self._send(403, {"ok": False, "msg": "Forbidden"})
                    if p == "/api/frame":
                        return self._send(200, app.frame(q.get("view", ["feedback"])[0]))
                    if p == "/api/static":
                        return self._send(200, app.static())
                    if p == "/api/devices":
                        return self._send(200, app.devices(refresh=q.get("refresh", ["0"])[0] == "1"))
                    if p == "/api/report.txt":
                        return self._send(200, app.report(), "text/plain",
                                          {"Content-Disposition": 'attachment; filename="mixxtrace-report-%s.txt"' % time.strftime("%Y%m%d-%H%M")})
                    if p == "/api/session.json":
                        return self._send(200, json.dumps(app._session(), allow_nan=False), "application/json",
                                          {"Content-Disposition": 'attachment; filename="mixxtrace-session-%s.json"' % time.strftime("%Y%m%d-%H%M")})
                    return self._send(404, {"ok": False, "msg": "Not found"})
                rel = "index.html" if p in ("", "/") else p.lstrip("/")
                full = os.path.normpath(os.path.join(WEB, rel))
                if not full.startswith(WEB) or not os.path.isfile(full):
                    return self._send(404, "Not found", "text/plain")
                with open(full, "rb") as fh:
                    data = fh.read()
                self._send(200, data, mimetypes.guess_type(full)[0] or "application/octet-stream")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                try:
                    self._send(422, {"ok": False, "msg": "Server error: %s" % exc})
                except Exception:
                    pass

        def do_POST(self):
            try:
                if urlparse(self.path).path != "/api/cmd":
                    return self._send(404, {"ok": False, "msg": "Not found"})
                if not self._local_only():
                    return self._send(403, {"ok": False, "msg": "Forbidden"})
                n = int(self.headers.get("Content-Length") or 0)
                if n > 8 * 1024 * 1024:
                    return self._send(413, {"ok": False, "msg": "Too large"})
                try:
                    data = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._send(400, {"ok": False, "msg": "Bad JSON"})
                if not isinstance(data, dict):
                    return self._send(400, {"ok": False, "msg": "Bad request"})
                self._send(200, app.command(data))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                try:
                    self._send(422, {"ok": False, "msg": "Server error: %s" % exc})
                except Exception:
                    pass

    return Handler


def serve(app, host, port):
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True

    def housekeeping():
        while True:
            time.sleep(5)
            app.idle_check()

    threading.Thread(target=housekeeping, daemon=True).start()
    return httpd
