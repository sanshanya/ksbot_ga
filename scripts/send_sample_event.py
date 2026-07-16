from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen

payload = {
    "chat_id": "local-demo-chat",
    "chat_type": "p2p",
    "sender_id": "local-user",
    "event_id": "local-event-1",
    "mentioned": True,
    "text": "Reply with a one-line confirmation that the GA+WPS callback works.",
    "attachments": [],
    "cloud_docs": [],
    "shared_docs": [],
    "raw_event": {"sender": {"name": "Local Tester"}},
}
headers = {"Content-Type": "application/json"}
secret = os.getenv("GA_WPS_CALLBACK_SECRET", "")
if secret:
    headers["X-GA-WPS-SECRET"] = secret
request = Request(
    os.getenv("GA_WPS_CALLBACK_URL", "http://127.0.0.1:23883/wps/callback"),
    data=json.dumps(payload).encode(),
    headers=headers,
    method="POST",
)
with urlopen(request, timeout=10) as response:
    print(response.read().decode())
