from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .protocol import WpsMessage

logger = logging.getLogger(__name__)

class CallbackServer:
    def __init__(
        self,
        host: str,
        port: int,
        secret: str,
        on_message: Callable[[WpsMessage], None],
    ) -> None:
        self._host = host
        self._port = port
        self._secret = secret
        self._on_message = on_message
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        callback = self._on_message
        secret = self._secret

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/wps/callback":
                    self.send_error(404)
                    return
                supplied = self.headers.get("X-GA-WPS-SECRET", "")
                supplied = supplied or self.headers.get("X-KSBOT-WPS-SECRET", "")
                if secret and supplied != secret:
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    message = WpsMessage.from_payload(payload)
                    if not message.chat_id:
                        raise ValueError("chat_id is required")
                    callback(message)
                    body = b'{"ok":true}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as exc:
                    logger.exception("WPS callback failed")
                    body = json.dumps({"ok": False, "error": str(exc)}).encode()
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, fmt: str, *args: Any) -> None:
                logger.debug("callback: " + fmt, *args)

        self._server = ThreadingHTTPServer((self._host, self._port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=2)

