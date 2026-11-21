"""Tiny web dashboard for the simulator (standard library only)."""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

log = logging.getLogger(__name__)


def serve(session, host: str = "127.0.0.1", port: int = 8080) -> None:
    page = resources.files(__package__).joinpath("dashboard.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("http: " + fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data).encode(), "application/json")

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._json(session.state())
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/api/action":
                return self._send(404, b"not found", "text/plain")
            length = int(self.headers.get("Content-Length") or 0)
            if length > 4096:
                return self._json({"ok": False, "reason": "too large"}, 413)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json({"ok": False, "reason": "bad JSON"}, 400)
            if not isinstance(data, dict):
                return self._json({"ok": False, "reason": "bad request"}, 400)
            with session.lock:
                self._json(session.action(data))

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever(poll_interval=0.25)
    finally:
        httpd.server_close()
