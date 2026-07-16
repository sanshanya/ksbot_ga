from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from ga_wps.wps import (
    CallbackServer,
    Mention,
    WpsClient,
    WpsMessage,
    _extract_text,
    _split_markdown,
)


# TEST-CONTRACT: req=WPS-MSG-01 | rejects=attachment size normalization is lost | gap=no from_payload test | revert=remove _safe_int call | mock=none
def test_payload_normalizes_attachments() -> None:
    message = WpsMessage.from_payload(
        {
            "chat_id": "c1",
            "chat_type": "p2p",
            "text": "",
            "attachments": [
                {
                    "type": "image",
                    "storage_key": "key-1",
                    "name": "photo.png",
                    "size": "12",
                }
            ],
            "raw_event": {"sender": {"name": "Alice"}},
        }
    )
    assert message.attachments[0].storage_key == "key-1"
    assert message.attachments[0].size == 12


# TEST-CONTRACT: req=WPS-MSG-02 | rejects=event id present only inside raw message is lost, disabling dedupe | gap=no fallback extraction | revert=remove raw_message id fallback | mock=none
def test_payload_uses_raw_message_id_fallback() -> None:
    message = WpsMessage.from_payload(
        {
            "chat_id": "c1",
            "chat_type": "p2p",
            "text": "hello",
            "raw_event": {"sender": {"id": "u1"}, "message": {"id": "m-raw"}},
        }
    )
    assert message.event_id == "m-raw"


# TEST-CONTRACT: req=WPS-SPLIT-01 | rejects=markdown split exceeds message size limit (accumulated or single-block) | gap=no split limit test | revert=remove limit check in _split_markdown | mock=none
@pytest.mark.parametrize(
    "text, limit, expected_count",
    [
        ("a" * 20 + "\n\n" + "b" * 20, 25, 2),  # accumulated overflow
        ("a" * 30, 25, 2),  # single-block overflow
    ],
)
def test_markdown_split_respects_limit(text: str, limit: int, expected_count: int) -> None:
    chunks = _split_markdown(text, limit=limit)
    assert len(chunks) == expected_count
    assert all(len(c) <= limit for c in chunks)


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


# TEST-CONTRACT: req=WPS-TEXT-01 | rejects=rich-text mention and body are concatenated without a boundary | gap=mention rendering joins raw parts | revert=remove explicit space after mention | mock=real-shaped WPS rich-text fixture
def test_extract_text_keeps_mention_boundary() -> None:
    value = {
        "rich_text": {
            "elements": [
                {
                    "elements": [
                        {"type": "mention", "mention_content": {"text": "甘小雨"}},
                        {"type": "text", "text_content": {"content": "同意"}},
                    ]
                }
            ]
        }
    }
    assert _extract_text(value) == "@甘小雨 同意"


# TEST-CONTRACT: req=WPS-IDENTITY-01 | rejects=client id is mistaken for tenant service-principal id | mock=WPS API boundary
def test_client_exposes_full_history_query_and_current_service_principal(monkeypatch) -> None:
    client = WpsClient(api_base="https://example.test", client_id="app", client_secret="secret")
    calls = []

    def request(method, uri, payload=None):
        calls.append((method, uri))
        return {"code": 0, "data": {"id": "sp-1"}}

    monkeypatch.setattr(client, "_request", request)
    client.get_messages("chat/1", page_size=50, page_token="next", start_time=1)
    assert client.current_service_principal()["id"] == "sp-1"
    assert calls == [
        ("GET", "/v7/chats/chat%2F1/messages?page_size=50&page_token=next&start_time=1"),
        ("GET", "/v7/service_principals/current"),
    ]


# TEST-CONTRACT: req=WPS-MENTION-01 | rejects=reply at-tag and mentions identity diverge or omit company_id | gap=no outbound mention payload contract | revert=restore mandatory company_id or mismatched ids | mock=none
def test_mention_payload_matches_at_tag_and_carries_company_id() -> None:
    mention = Mention("uid_123", "ACME001", "张三")
    assert mention.at_tag(1) == '<at id="1">张三</at>'
    assert mention.payload(1) == {
        "id": "1",
        "type": "user",
        "identity": {
            "id": "uid_123",
            "type": "user",
            "company_id": "ACME001",
        },
    }


# TEST-CONTRACT: req=WPS-MENTION-02 | rejects=group reply mention sent without a valid company_id | gap=sp lookup fails or returns empty company_id | revert=send mention with empty company_id (HTTP 400) | mock=WPS SP API boundary
def test_resolve_mention_returns_none_when_sp_has_no_company_id(monkeypatch) -> None:
    client = WpsClient(api_base="https://example.test", client_id="app", client_secret="secret")
    monkeypatch.setattr(client, "current_service_principal", lambda: {"id": "sp-1", "company_id": ""})
    monkeypatch.setattr(client, "get_user", lambda uid: {"id": uid, "user_name": "李四"})
    first = client.resolve_mention("chat-1", "uid_123", "fallback")
    second = client.resolve_mention("chat-2", "uid_123", "changed")
    assert first is None and second is None


# TEST-CONTRACT: req=WPS-MENTION-03 | rejects=missing contact permission silently removes the group mention | gap=sp lookup raises | revert=require sp lookup success | mock=WPS SP API failure
def test_resolve_mention_falls_back_to_none_on_sp_failure(monkeypatch) -> None:
    client = WpsClient(api_base="https://example.test", client_id="app", client_secret="secret")
    def denied():
        raise RuntimeError("403")
    monkeypatch.setattr(client, "current_service_principal", denied)
    monkeypatch.setattr(client, "get_user", lambda uid: {"id": uid, "user_name": "李四"})
    assert client.resolve_mention("chat-1", "uid_123456", "") is None


# TEST-CONTRACT: req=WPS-MENTION-02B | rejects=mention sent when company_id is available | gap=resolve_mention returns None despite valid company_id from SP | revert=drop company_id guard | mock=WPS SP + contact API boundary
def test_resolve_mention_returns_mention_with_company_id_from_sp(monkeypatch) -> None:
    client = WpsClient(api_base="https://example.test", client_id="app", client_secret="secret")
    sp_calls: list[str] = []
    user_calls: list[str] = []

    def sp():
        sp_calls.append("sp")
        return {"id": "sp-1", "company_id": "ACME001"}

    def get_user(user_id: str):
        user_calls.append(user_id)
        return {"id": user_id, "user_name": "李四"}

    monkeypatch.setattr(client, "current_service_principal", sp)
    monkeypatch.setattr(client, "get_user", get_user)
    mention = client.resolve_mention("chat-1", "uid_123", "fallback")
    assert mention == Mention("uid_123", "ACME001", "李四")
    # company_id is cached on the client — second resolve_mention for a different
    # user must NOT re-query the SP (only get_user).
    mention2 = client.resolve_mention("chat-2", "uid_456", "fallback2")
    assert mention2 == Mention("uid_456", "ACME001", "李四")  # name from get_user
    assert sp_calls == ["sp"]  # SP queried only once
    assert user_calls == ["uid_123", "uid_456"]


# TEST-CONTRACT: req=WPS-MENTION-02C | rejects=mention display_name falls back to caller-supplied name when get_user fails | gap=name lookup fails silently | revert=require get_user success | mock=WPS contact API failure
def test_resolve_mention_falls_back_to_display_name_when_get_user_fails(monkeypatch) -> None:
    client = WpsClient(api_base="https://example.test", client_id="app", client_secret="secret")
    monkeypatch.setattr(client, "current_service_principal", lambda: {"id": "sp-1", "company_id": "ACME001"})
    def denied(_uid: str):
        raise RuntimeError("403")
    monkeypatch.setattr(client, "get_user", denied)
    mention = client.resolve_mention("chat-1", "uid_123", "FallbackName")
    assert mention == Mention("uid_123", "ACME001", "FallbackName")


# TEST-CONTRACT: req=WPS-MENTION-04 | rejects=reply mention corrupts a leading Markdown block | gap=at tag concatenated inline | revert=restore space-prefix rendering | mock=send API boundary
def test_send_markdown_split_places_mention_on_own_line(monkeypatch) -> None:
    client = WpsClient(api_base="https://example.test", client_id="app", client_secret="secret")
    sent = []
    monkeypatch.setattr(
        client,
        "send_markdown",
        lambda chat, text, mentions=None: sent.append((chat, text, mentions)),
    )
    mention = Mention("uid_123", "cid", "张三")
    client.send_markdown_split("chat-1", "# 标题", mention=mention, delay=0)
    assert sent == [("chat-1", '<at id="1">张三</at>\n\n# 标题', [mention])]
