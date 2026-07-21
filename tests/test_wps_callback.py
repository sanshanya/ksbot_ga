from __future__ import annotations

import urllib.error
import urllib.request

from ga_wps.callback import CallbackServer
from ga_wps.protocol import WpsMessage

# TEST-CONTRACT: req=WPS-CALLBACK-01 | rejects=callback endpoint accepts request without correct secret | gap=no secret enforcement test | revert=remove secret check in do_POST | mock=none
def test_callback_rejects_wrong_secret_with_403() -> None:
    received: list[WpsMessage] = []
    server = CallbackServer("127.0.0.1", 0, "s3cret", received.append)
    server.start()
    try:
        port = server._server.server_port  # type: ignore[union-attr]
        base = f"http://127.0.0.1:{port}/wps/callback"

        # Wrong secret → 403
        req_bad = urllib.request.Request(
            base,
            data=b'{"chat_id":"c1"}',
            headers={"X-GA-WPS-SECRET": "wrong"},
        )
        try:
            urllib.request.urlopen(req_bad, timeout=2)
            raise AssertionError("wrong-secret request should have been rejected")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403

        # Correct secret → 200 and message dispatched
        req_ok = urllib.request.Request(
            base,
            data=b'{"chat_id":"c1","chat_type":"p2p","text":"hi","sender_id":"u1"}',
            headers={"X-GA-WPS-SECRET": "s3cret"},
        )
        resp = urllib.request.urlopen(req_ok, timeout=2)
        assert resp.status == 200
        assert len(received) == 1
    finally:
        server.stop()

