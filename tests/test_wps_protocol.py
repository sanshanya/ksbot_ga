from __future__ import annotations

import pytest

from ga_wps.protocol import Mention, WpsMessage


def payload(**updates):
    value = {
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
    value.update(updates)
    return value


def test_canonical_payload_and_mention_contract() -> None:
    message = WpsMessage.from_payload(
        payload(
            text="",
            attachments=[
                {"type": "image", "storage_key": "key-1", "name": "photo.png", "size": "12"}
            ],
            shared_docs=[{"link_id": "shared-1"}],
        )
    )
    assert message.text == "[attachment-only message]"
    assert message.attachments[0].size == 12
    assert message.shared_doc_ids == ("shared-1",)
    with pytest.raises(ValueError, match="canonical WPS payload"):
        WpsMessage.from_payload({"chat_id": "c1", "raw_event": {"message": {"id": "m1"}}})

    mention = Mention("uid_123", "ACME001", "张三")
    assert mention.at_tag(1) == '<at id="1">张三</at>'
    assert mention.payload(1)["identity"] == {
        "id": "uid_123",
        "type": "user",
        "company_id": "ACME001",
    }
