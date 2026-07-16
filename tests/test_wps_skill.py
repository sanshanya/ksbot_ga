from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "wps-chat" / "scripts" / "wps_chat.py"
SPEC = importlib.util.spec_from_file_location("wps_chat_skill", SCRIPT)
assert SPEC and SPEC.loader
wps_chat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wps_chat)


def _msg(mid: str, stamp: int, text="", sender="User", attachment=False) -> dict:
    content = text
    if attachment:
        content = {"file": {"local": {"storage_key": mid, "name": f"{mid}.txt"}}}
    return {
        "id": mid,
        "ctime": stamp,
        "position": stamp,
        "sender": {"id": sender, "type": "sp" if sender == "甘小雨" else "user", "name": sender},
        "content": content,
    }


class FakeWps:
    def __init__(self, responses: dict[tuple[int | None, str | None], tuple[list[dict], str]]):
        self.responses = responses
        self.calls: list[tuple[int | None, str | None]] = []
        self.downloads: list[tuple[str, str, str]] = []

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


def _simple_api() -> FakeWps:
    values = [_msg("old", 1000, "old"), _msg("answer", 3000, "latest answer", "甘小雨"), _msg("file", 3500, attachment=True), _msg("current", 4000, "current")]
    return FakeWps({(None, None): (values, ""), (1, None): (values, "")})


# TEST-CONTRACT: req=WPS-SKILL-01 | rejects=opaque host-only context and inaccessible previous attachment | mock=WPS boundary
def test_history_and_download_are_explainable_ga_callable_capabilities(tmp_path) -> None:
    api = _simple_api()
    ctx = {"chat_id": "chat-1", "workspace": str(tmp_path), "current_event_id": "current"}
    result = wps_chat.history(api, ctx, participant="甘小雨")
    assert "Source:" in result and "Refresh with GA code_run:" in result
    assert "latest answer" in result and "current" not in result and "\ufffd" not in result
    downloaded = wps_chat.download(api, ctx)
    path = tmp_path / "downloads" / "file_file.txt"
    assert path.read_text(encoding="utf-8") == "attachment body" and str(path) in downloaded
    assert api.downloads == [("chat-1", "file", "file")]


# TEST-CONTRACT: req=WPS-SKILL-02 | rejects=domain capability published as another model Tool | mock=none
def test_skill_uses_runtime_context_and_ga_base_tools_only() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "--chat-id" not in source and ".wps_context.json" in source and "AgentTool" not in source


# TEST-CONTRACT: req=WPS-PAGINATION-01 | rejects=empty first page cursor loss and cursor loops | mock=WPS boundary
def test_pages_follow_empty_cursor_and_reject_cycles() -> None:
    api = FakeWps({(None, None): ([], "p2"), (None, "p2"): ([_msg("older", 1)], "")})
    assert [wps_chat.message_id(x) for page in wps_chat.pages(api, "chat-1") for x in page] == ["older"]
    looping = FakeWps({(None, None): ([], "same"), (None, "same"): ([], "same")})
    with pytest.raises(RuntimeError, match="repeated page token"):
        list(wps_chat.pages(looping, "chat-1"))


# TEST-CONTRACT: req=WPS-HISTORY-ALL-01 | rejects=default-window-only filtering and latest-50 attachment search | mock=WPS boundary
def test_filtered_history_and_latest_attachment_expand_to_all_visible_history(tmp_path) -> None:
    recent = [_msg(f"r{i}", 2000 + i, "noise") for i in range(50)]
    old = [_msg("older-answer", 100, "old answer", "甘小雨"), _msg("older-file", 110, attachment=True)]
    newer = [_msg("newer-answer", 3000, "new answer", "甘小雨")]
    responses = {
        (None, None): (recent, ""),
        (1, None): (old, "next"),
        (1, "next"): (recent + newer, ""),
    }
    api = FakeWps(responses)
    ctx = {"chat_id": "chat-1", "workspace": str(tmp_path)}
    result = wps_chat.history(api, ctx, limit=1, participant="甘小雨")
    assert "new answer" in result and "old answer" not in result
    wps_chat.download(api, ctx)
    assert api.downloads == [("chat-1", "older-file", "older-file")]
    assert (1, None) in api.calls


# TEST-CONTRACT: req=WPS-EMPTY-01 | rejects=successful empty fetch described as opaque failure | mock=WPS boundary
def test_history_distinguishes_successful_empty_fetch(tmp_path) -> None:
    api = FakeWps({(None, None): ([], ""), (1, None): ([], "")})
    result = wps_chat.history(api, {"chat_id": "chat-1", "workspace": str(tmp_path)})
    assert "History fetch succeeded" in result and "no accessible messages" in result
