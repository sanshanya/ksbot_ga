from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from ga_wps.callback import CallbackServer
from ga_wps.protocol import WpsMessage


def payload():
    return {
        "chat_id": "c1",
        "sender_id": "u1",
        "sender_name": "Alice",
        "chat_type": "p2p",
        "text": "hello",
        "event_id": "m1",
        "mentioned": False,
        "attachments": [],
        "cloud_docs": [],
        "shared_docs": [],
    }


def test_callback_authenticates_and_dispatches_canonical_message() -> None:
    received: list[WpsMessage] = []
    server = CallbackServer("127.0.0.1", 0, "s3cret", received.append)
    server.start()
    try:
        url = f"http://127.0.0.1:{server._server.server_port}/wps/callback"  # type: ignore[union-attr]
        wrong = urllib.request.Request(url, data=b"{}", headers={"X-GA-WPS-SECRET": "wrong"})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(wrong, timeout=2)
        assert error.value.code == 403
        request = urllib.request.Request(
            url,
            data=json.dumps(payload()).encode(),
            headers={"X-GA-WPS-SECRET": "s3cret"},
        )
        assert urllib.request.urlopen(request, timeout=2).status == 200
        assert received == [WpsMessage.from_payload(payload())]
    finally:
        server.stop()
