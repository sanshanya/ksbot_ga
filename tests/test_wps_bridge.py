from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


NODE = shutil.which("node")
MODULE = Path(__file__).resolve().parents[1] / "bridge" / "wps_event_normalize.mjs"


def _run_normalize(payload: dict, bot_ids=("app-1", "sp-1")) -> dict:
    source = (
        f"import {{ normalize }} from {json.dumps(MODULE.as_uri())};"
        f"const value = normalize({json.dumps(payload, ensure_ascii=False)}, 'event', {json.dumps(list(bot_ids))}, 'fallback');"
        "console.log(JSON.stringify(value));"
    )
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", source],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(result.stdout)


def _normalize(elements: list[dict]) -> dict:
    payload = {
        "chat": {"id": "chat-1", "type": "group"},
        "sender": {"id": "user-1", "type": "user"},
        "message": {
            "id": "message-1",
            "type": "text",
            "content": {"rich_text": {"elements": [{"elements": elements}]}},
        },
    }
    return _run_normalize(payload)


@pytest.mark.skipif(NODE is None, reason="node is required for WPS bridge normalization")
def test_current_app_mention_is_metadata_not_business_text() -> None:
    value = _normalize(
        [
            {
                "type": "mention",
                "mention_content": {
                    "text": "甘小雨",
                    "identity": {"type": "app", "app_id": "app-1"},
                },
            },
            {"type": "text", "text_content": {"content": "同意"}},
        ]
    )
    assert value["mentioned"] is True
    assert value["text"] == "同意"


@pytest.mark.skipif(NODE is None, reason="node is required for WPS bridge normalization")
def test_other_user_mention_keeps_an_explicit_boundary() -> None:
    value = _normalize(
        [
            {
                "type": "mention",
                "mention_content": {
                    "text": "张三",
                    "identity": {"type": "user", "id": "user-2"},
                },
            },
            {"type": "text", "text_content": {"content": "同意"}},
        ]
    )
    assert value["text"] == "@张三 同意"


@pytest.mark.skipif(NODE is None, reason="node is required for WPS bridge normalization")
def test_current_app_mention_only_does_not_reappear_from_fallback_text() -> None:
    value = _normalize(
        [
            {
                "type": "mention",
                "mention_content": {
                    "text": "甘小雨",
                    "identity": {"type": "sp", "id": "app-1"},
                },
            }
        ]
    )
    assert value["mentioned"] is True
    assert value["text"] == ""


@pytest.mark.skipif(NODE is None, reason="node is required for WPS bridge normalization")
def test_inline_at_tags_remove_only_current_app_mentions() -> None:
    payload = {
        "chat": {"id": "chat-1", "type": "group"},
        "sender": {"id": "user-1", "type": "user"},
        "message": {
            "id": "message-1",
            "type": "text",
            "content": {
                "text": {
                    "content": '<at id="1">甘小雨</at><at id="2">张三</at>同意'
                }
            },
            "mentions": [
                {"id": "1", "identity": {"type": "sp", "id": "sp-1", "name": "甘小雨"}},
                {"id": "2", "identity": {"type": "user", "id": "user-2", "name": "张三"}},
            ],
        },
    }
    value = _run_normalize(payload)
    assert value["mentioned"] is True
    assert value["text"] == "@张三 同意"


@pytest.mark.skipif(NODE is None, reason="node is required for WPS bridge normalization")
def test_inline_at_tags_support_second_bot_mention_without_removing_other_people() -> None:
    payload = {
        "chat": {"id": "chat-1", "type": "group"},
        "sender": {"id": "user-1", "type": "user"},
        "message": {
            "id": "message-1",
            "type": "text",
            "content": {
                "text": {
                    "content": '<at id="1">李四</at><at id="2">甘小雨</at>同意'
                }
            },
            "mentions": [
                {"id": "1", "identity": {"type": "user", "id": "user-3", "name": "李四"}},
                {"id": "2", "identity": {"type": "app", "app_id": "app-1", "name": "甘小雨"}},
            ],
        },
    }
    value = _run_normalize(payload)
    assert value["mentioned"] is True
    assert value["text"] == "@李四 同意"


@pytest.mark.skipif(NODE is None, reason="node is required for WPS bridge normalization")
def test_unknown_inline_mention_is_preserved_not_silently_removed() -> None:
    payload = {
        "chat": {"id": "chat-1", "type": "group"},
        "sender": {"id": "user-1", "type": "user"},
        "message": {
            "id": "message-1",
            "type": "text",
            "content": {"text": {"content": '<at id="9">未知成员</at>同意'}},
            "mentions": [],
        },
    }
    value = _run_normalize(payload)
    assert value["mentioned"] is False
    assert value["text"] == "@未知成员 同意"


@pytest.mark.skipif(NODE is None, reason="node is required for WPS bridge normalization")
def test_real_sp_id_and_data_mentions_fallback_remove_only_the_bot() -> None:
    payload = {
        "chat": {"id": "chat-1", "type": "group"},
        "sender": {"id": "user-1", "type": "user"},
        "mentions": [
            {"id": "1", "identity": {"type": "sp", "id": "sp-1", "name": "甘小雨"}},
            {"id": "2", "identity": {"type": "user", "id": "u2", "name": "张三"}},
        ],
        "message": {
            "id": "message-1",
            "type": "text",
            "mentions": [],
            "content": {"text": {"content": '<at id="1">甘小雨</at><at id="2">张三</at>同意'}},
        },
    }
    value = _run_normalize(payload, bot_ids=("AK-client", "sp-1"))
    assert value["mentioned"] is True
    assert value["text"] == "@张三 同意"
