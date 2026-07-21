from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .protocol import WpsMessage

logger = logging.getLogger(__name__)


class CallbackServer:
    def __init__(
        self, host: str, port: int, secret: str, on_message: Callable[[WpsMessage], None]
    ) -> None:
        self.host, self.port, self.secret = host, port, secret
        self.on_message = on_message
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        callback, secret = self.on_message, self.secret

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/wps/callback":
                    self.send_error(404)
                    return
                supplied = self.headers.get("X-GA-WPS-SECRET", "") or self.headers.get(
                    "X-KSBOT-WPS-SECRET", ""
                )
                if secret and supplied != secret:
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    callback(WpsMessage.from_payload(json.loads(self.rfile.read(length) or b"{}")))
                    self._reply(200, {"ok": True})
                except Exception as exc:
                    logger.exception("WPS callback failed")
                    self._reply(400, {"ok": False, "error": str(exc)})

            def _reply(self, status: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args) -> None:
                logger.debug("callback: " + fmt, *args)

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=2)
