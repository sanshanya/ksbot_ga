from __future__ import annotations

import pytest

from ga_wps.client import WpsClient, _split
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
