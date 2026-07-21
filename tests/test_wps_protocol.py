from __future__ import annotations

import pytest

from ga_wps.protocol import Mention, WpsMessage, _extract_text, _split_markdown

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

