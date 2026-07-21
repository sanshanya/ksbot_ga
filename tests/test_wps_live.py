from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from ga_wps.config import WpsSettings
from ga_wps.client import WpsClient

CHAT_ID = os.getenv("WPS_HISTORY_TEST_CHAT_ID", "")
pytestmark = pytest.mark.skipif(
    not CHAT_ID,
    reason="set WPS_HISTORY_TEST_CHAT_ID with real WPS credentials to verify WPS Skill history",
)
SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "wps-chat" / "scripts" / "wps_chat.py"
SPEC = importlib.util.spec_from_file_location("wps_chat_skill_live", SCRIPT)
assert SPEC and SPEC.loader
wps_chat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wps_chat)


def test_live_wps_skill_history_can_be_fetched_and_rendered(tmp_path) -> None:
    settings = WpsSettings.from_env()
    settings.validate()
    client = WpsClient(
        api_base=settings.api_base,
        client_id=settings.client_id,
        client_secret=settings.client_secret,
    )
    rendered = wps_chat.history(
        client,
        {"chat_id": CHAT_ID, "workspace": str(tmp_path), "sender_names": {}},
        limit=settings.recent_history_messages,
    )
    assert "WPS chat history capability result" in rendered
    assert "message_id=" in rendered
