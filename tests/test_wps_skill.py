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
    assert "Source:" in result and "Refresh:" in result
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


def test_document_skill_uses_keychain_capability_without_chat_context(monkeypatch, capsys) -> None:
    class FakeDocs:
        available = True
        authenticated = True

        def read_file(self, **_kwargs):
            return {"name": "baseline.otl", "content": "# baseline"}

    monkeypatch.setattr(wps_chat, "KdocsCli", FakeDocs)
    assert wps_chat.main(["document", "--url", "https://365.kdocs.cn/l/example"]) == 0
    result = capsys.readouterr().out
    assert "baseline.otl" in result and "# baseline" in result


def test_document_create_skill_writes_workspace_markdown_once(monkeypatch, tmp_path, capsys) -> None:
    calls = []

    class FakeDocs:
        available = True
        authenticated = True

        def create_smart_doc(self, **kwargs):
            calls.append(("create", kwargs))
            return {"file_id": "doc-1"}

        def share_file(self, **kwargs):
            calls.append(("share", kwargs))
            return {"url": "https://365.kdocs.cn/l/doc-1"}

    draft = tmp_path / "draft.md"
    draft.write_text("# baseline\n\ncontent", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wps_chat, "KdocsCli", FakeDocs)
    assert wps_chat.main(
        ["document-create", "--title", "Baseline", "--content-file", str(draft)]
    ) == 0
    output = capsys.readouterr().out
    assert "WPS smart document created and shared" in output
    assert "doc-1" in output and "https://365.kdocs.cn/l/doc-1" in output
    assert "anyone with the link can view" in output
    assert calls == [
        ("create", {"title": "Baseline", "content": "# baseline\n\ncontent", "parent_id": ""}),
        ("share", {"file_id": "doc-1", "scope": "anyone"}),
    ]


def test_document_create_does_not_repeat_when_sharing_fails(monkeypatch, capsys) -> None:
    calls = []

    class FakeDocs:
        available = True
        authenticated = True

        def create_smart_doc(self, **kwargs):
            calls.append(("create", kwargs))
            return {"file_id": "doc-1"}

        def share_file(self, **kwargs):
            calls.append(("share", kwargs))
            raise RuntimeError("share unavailable")

    monkeypatch.setattr(wps_chat, "KdocsCli", FakeDocs)
    assert wps_chat.main(["document-create", "--title", "Baseline", "--content", "# x"]) == 1
    error = capsys.readouterr().err
    assert "file_id=doc-1" in error and "document-share" in error
    assert [name for name, _ in calls] == ["create", "share"]


def test_document_share_and_append_are_explicit_side_effects(monkeypatch, tmp_path, capsys) -> None:
    calls = []

    class FakeDocs:
        available = True
        authenticated = True

        def append_smart_doc(self, **kwargs):
            calls.append(("append", kwargs))

        def share_file(self, **kwargs):
            calls.append(("share", kwargs))
            return {"url": "https://365.kdocs.cn/l/doc-1"}

    draft = tmp_path / "append.md"
    draft.write_text("## Follow-up", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wps_chat, "KdocsCli", FakeDocs)
    assert wps_chat.main(
        ["document-append", "--file-id", "doc-1", "--content-file", str(draft)]
    ) == 0
    assert wps_chat.main(["document-share", "--file-id", "doc-1"]) == 0
    capsys.readouterr()
    assert calls == [
        ("append", {"file_id": "doc-1", "content": "## Follow-up"}),
        ("share", {"file_id": "doc-1", "scope": "anyone"}),
    ]


def test_document_search_renders_cli_file_envelope(monkeypatch, capsys) -> None:
    class FakeDocs:
        available = True
        authenticated = True

        def search_files(self, **kwargs):
            assert kwargs == {"keyword": "baseline", "page_size": 20, "search_type": "content"}
            return {"items": [{"file": {"id": "doc-1", "name": "baseline.otl", "link_url": "https://www.kdocs.cn/l/doc-1"}}]}

    monkeypatch.setattr(wps_chat, "KdocsCli", FakeDocs)
    assert wps_chat.main(["document-search", "--keyword", "baseline", "--type", "content"]) == 0
    output = capsys.readouterr().out
    assert "Type: content" in output
    assert "[open](https://www.kdocs.cn/l/doc-1)" in output


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
