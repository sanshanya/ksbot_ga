from __future__ import annotations

import json

import pytest

from ga_wps.client import WpsClient, _split
from ga_wps.kdocs import KdocsCli, KdocsCliError
from ga_wps.protocol import Mention


def client() -> WpsClient:
    return WpsClient(api_base="https://example.test", client_id="app", client_secret="secret")


@pytest.mark.parametrize(
    "text, limit, count", [("a" * 20 + "\n\n" + "b" * 20, 25, 2), ("a" * 30, 25, 2)]
)
def test_message_split_limit(text: str, limit: int, count: int) -> None:
    chunks = _split(text, limit)
    assert len(chunks) == count and all(len(chunk) <= limit for chunk in chunks)


def test_endpoints_mentions_and_send_layout(monkeypatch) -> None:
    api = client()
    calls = []

    def request(method, uri, payload=None):
        calls.append((method, uri))
        return {"code": 0, "data": {"id": "sp-1"}}

    monkeypatch.setattr(api, "_request", request)
    api.get_messages("chat/1", 50, "next", 1)
    assert api.current_service_principal()["id"] == "sp-1"
    assert calls == [
        ("GET", "/v7/chats/chat%2F1/messages?page_size=50&page_token=next&start_time=1"),
        ("GET", "/v7/service_principals/current"),
    ]
    api = client()
    sp_calls = []
    monkeypatch.setattr(
        api,
        "current_service_principal",
        lambda: sp_calls.append(1) or {"id": "sp", "company_id": "ACME001"},
    )
    monkeypatch.setattr(api, "get_user", lambda uid: {"id": uid, "user_name": "李四"})
    assert api.resolve_mention("u1", "fallback") == Mention("u1", "ACME001", "李四")
    assert api.resolve_mention("u2", "fallback") == Mention("u2", "ACME001", "李四")
    assert sp_calls == [1]

    sent = []
    monkeypatch.setattr(
        api, "send_markdown", lambda chat, text, mentions=None: sent.append((chat, text, mentions))
    )
    mention = Mention("u1", "cid", "张三")
    api.send_markdown_split("c1", "# 标题", mention=mention, delay=0)
    assert sent == [("c1", '<at id="1">张三</at>\n\n# 标题', [mention])]


def test_mention_fallback_never_emits_invalid_identity(monkeypatch) -> None:
    api = client()
    monkeypatch.setattr(api, "current_service_principal", lambda: {"id": "sp", "company_id": ""})
    assert api.resolve_mention("u1", "fallback") is None
    assert api.resolve_mention("u1", "changed") is None

    api = client()
    monkeypatch.setattr(
        api, "current_service_principal", lambda: {"id": "sp", "company_id": "ACME001"}
    )
    monkeypatch.setattr(api, "get_user", lambda _uid: (_ for _ in ()).throw(RuntimeError("403")))
    assert api.resolve_mention("u1", "Fallback") == Mention("u1", "ACME001", "Fallback")


def test_kdocs_cli_reads_markdown_with_keychain_binary(monkeypatch) -> None:
    calls = []

    class Completed:
        returncode = 0
        stderr = ""
        stdout = json.dumps({"code": 0, "data": {"name": "doc.otl", "content": "# doc"}})

    monkeypatch.setattr("ga_wps.kdocs.shutil.which", lambda _name: "kdocs-cli")
    monkeypatch.setattr("ga_wps.kdocs.Path.is_file", lambda _path: True)
    monkeypatch.setattr(
        "ga_wps.kdocs.subprocess.run",
        lambda command, **_kwargs: calls.append(command) or Completed(),
    )
    result = KdocsCli().read_file_by_url("https://365.kdocs.cn/l/example")
    assert result["content"] == "# doc"
    assert calls[0][1:4] == ["drive", "read-file", "--compact"]
    assert json.loads(calls[0][4]) == {
        "format": "markdown",
        "url": "https://365.kdocs.cn/l/example",
    }


def test_kdocs_cli_creates_smart_doc_with_cli_contract(monkeypatch) -> None:
    calls = []

    class Completed:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "code": 0,
                "data": {"file_id": "doc-1", "link_url": "https://365.kdocs.cn/l/doc-1"},
            }
        )

    monkeypatch.setattr("ga_wps.kdocs.shutil.which", lambda _name: "kdocs-cli")
    monkeypatch.setattr("ga_wps.kdocs.Path.is_file", lambda _path: True)
    monkeypatch.setattr(
        "ga_wps.kdocs.subprocess.run",
        lambda command, **_kwargs: calls.append(command) or Completed(),
    )
    result = KdocsCli().create_smart_doc(title="Weekly report", content="# Report")
    assert result["file_id"] == "doc-1"
    assert calls[0][1:4] == ["drive", "create-file-with-content", "--compact"]
    assert json.loads(calls[0][4]) == {"name": "Weekly report.otl", "content": "# Report"}


def test_kdocs_cli_surfaces_partial_create_state(monkeypatch) -> None:
    class Completed:
        returncode = 1
        stderr = ""
        stdout = json.dumps(
            {"code": 500000, "message": "content write failed", "data": {"file_id": "doc-1"}}
        )

    monkeypatch.setattr("ga_wps.kdocs.shutil.which", lambda _name: "kdocs-cli")
    monkeypatch.setattr("ga_wps.kdocs.Path.is_file", lambda _path: True)
    monkeypatch.setattr("ga_wps.kdocs.subprocess.run", lambda *_args, **_kwargs: Completed())
    with pytest.raises(KdocsCliError, match="creation outcome is partial"):
        KdocsCli().create_smart_doc(title="Weekly report", content="# Report")


def test_kdocs_cli_document_operations_use_official_commands(monkeypatch) -> None:
    calls = []
    payloads = [
        {"code": 0, "data": {}},
        {"code": 0, "data": {"url": "https://www.kdocs.cn/l/doc-1"}},
        {"code": 0, "data": {"files": [{"file_id": "doc-1"}]}},
    ]

    class Completed:
        def __init__(self, payload):
            self.returncode = 0
            self.stderr = ""
            self.stdout = json.dumps(payload)

    monkeypatch.setattr("ga_wps.kdocs.shutil.which", lambda _name: "kdocs-cli")
    monkeypatch.setattr("ga_wps.kdocs.Path.is_file", lambda _path: True)
    monkeypatch.setattr(
        "ga_wps.kdocs.subprocess.run",
        lambda command, **_kwargs: calls.append(command) or Completed(payloads.pop(0)),
    )
    cli = KdocsCli()
    cli.append_smart_doc(file_id="doc-1", content="## More")
    cli.share_file(file_id="doc-1")
    cli.search_files(keyword="baseline")
    assert [call[1:4] for call in calls] == [
        ["otl", "insert-content", "--compact"],
        ["drive", "share-file", "--compact"],
        ["drive", "search-files", "--compact"],
    ]
    assert json.loads(calls[0][4]) == {
        "file_id": "doc-1",
        "content": "## More",
        "format": "markdown",
        "mode": "append",
    }
    assert json.loads(calls[1][4]) == {"file_id": "doc-1", "scope": "anyone"}
