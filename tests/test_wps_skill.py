from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from ga_wps.history import _extract_text

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "wps-chat" / "scripts" / "wps_chat.py"
SPEC = importlib.util.spec_from_file_location("wps_chat_skill", SCRIPT)
assert SPEC and SPEC.loader
wps_chat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wps_chat)


def msg(mid: str, stamp: int, text="", sender="User", attachment=False) -> dict:
    content = (
        {"file": {"local": {"storage_key": mid, "name": f"{mid}.txt"}}}
        if attachment
        else text
    )
    return {
        "id": mid,
        "ctime": stamp,
        "position": stamp,
        "sender": {"id": sender, "type": "sp" if sender == "甘小雨" else "user", "name": sender},
        "content": content,
    }


class FakeWps:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.downloads = []

    def get_messages(self, chat_id, page_size=50, page_token=None, start_time=None):
        assert chat_id == "chat-1" and page_size == 50
        self.calls.append((start_time, page_token))
        items, token = self.responses.get((start_time, page_token), ([], ""))
        return {"data": {"items": items, "next_page_token": token}}

    def download_attachment(self, *, chat_id, message_id, attachment, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("attachment body", encoding="utf-8")
        self.downloads.append((chat_id, message_id, attachment.storage_key))
        return target


def test_history_and_download_scan_all_visible_messages(tmp_path) -> None:
    recent = [msg(f"r{i}", 2000 + i, "noise") for i in range(50)]
    old = [msg("old-answer", 100, "old", "甘小雨"), msg("old-file", 110, attachment=True)]
    new = [msg("new-answer", 3000, "new", "甘小雨"), msg("current", 4000, "current")]
    api = FakeWps(
        {
            (None, None): (recent + new, ""),
            (1, None): (old, "next"),
            (1, "next"): (recent + new, ""),
        }
    )
    context = {"chat_id": "chat-1", "workspace": str(tmp_path), "current_event_id": "current"}
    result = wps_chat.history(api, context, limit=1, participant="甘小雨")
    assert "Source:" in result and "Refresh with GA code_run:" in result
    assert "message_id=new-answer" in result and "message_id=old-answer" not in result and "message_id=current" not in result and "�" not in result

    downloaded = wps_chat.download(api, context)
    path = next((tmp_path / "downloads").rglob("*.txt"))
    assert path.name == "01_old-file.txt"
    assert str(path) in downloaded and path.read_text(encoding="utf-8") == "attachment body"
    assert api.downloads == [("chat-1", "old-file", "old-file")]
    assert (1, None) in api.calls

    source = SCRIPT.read_text(encoding="utf-8")
    assert "--chat-id" not in source and ".wps_context.json" in source and "AgentTool" not in source


def test_pages_follow_empty_cursor_and_reject_cycles() -> None:
    api = FakeWps({(None, None): ([], "p2"), (None, "p2"): ([msg("older", 1)], "")})
    assert [wps_chat.message_id(item) for page in wps_chat.pages(api, "chat-1") for item in page] == ["older"]
    looping = FakeWps({(None, None): ([], "same"), (None, "same"): ([], "same")})
    with pytest.raises(RuntimeError, match="repeated page token"):
        list(wps_chat.pages(looping, "chat-1"))


def test_empty_history_is_reported_as_success(tmp_path) -> None:
    api = FakeWps({(None, None): ([], ""), (1, None): ([], "")})
    result = wps_chat.history(api, {"chat_id": "chat-1", "workspace": str(tmp_path)})
    assert "History fetch succeeded" in result and "no accessible messages" in result


def test_rich_text_mentions_keep_word_boundaries() -> None:
    value = {
        "rich_text": {
            "elements": [{"elements": [
                {"type": "mention", "mention_content": {"text": "甘小雨"}},
                {"type": "text", "text_content": {"content": "同意"}},
            ]}]
        }
    }
    assert _extract_text(value) == "@甘小雨 同意"
