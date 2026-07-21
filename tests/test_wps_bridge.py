from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
MODULE = Path(__file__).resolve().parents[1] / "bridge" / "wps_event_normalize.mjs"
pytestmark = pytest.mark.skipif(NODE is None, reason="node is required")


def run(payload: dict, bot_ids=("app-1", "sp-1")) -> dict:
    source = (
        f"import {{normalize}} from {json.dumps(MODULE.as_uri())};"
        f"console.log(JSON.stringify(normalize({json.dumps(payload, ensure_ascii=False)},"
        f"{json.dumps(list(bot_ids))},'fallback')));"
    )
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", source],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(result.stdout)


def payload(content: dict, mentions=(), top_mentions=()) -> dict:
    return {
        "chat": {"id": "chat-1", "type": "group"},
        "sender": {"id": "user-1", "type": "user", "name": "Alice"},
        "mentions": list(top_mentions),
        "message": {
            "id": "message-1",
            "type": "text",
            "content": content,
            "mentions": list(mentions),
        },
    }


def rich(*items) -> dict:
    return {"rich_text": {"elements": [{"elements": list(items)}]}}


def at(text: str, identity: dict) -> dict:
    return {"type": "mention", "mention_content": {"text": text, "identity": identity}}


def text(value: str) -> dict:
    return {"type": "text", "text_content": {"content": value}}


@pytest.mark.parametrize(
    "event, expected_text, mentioned, bot_ids",
    [
        (
            payload(rich(at("甘小雨", {"type": "app", "app_id": "app-1"}), text("同意"))),
            "同意",
            True,
            ("app-1", "sp-1"),
        ),
        (
            payload(rich(at("张三", {"type": "user", "id": "u2"}), text("同意"))),
            "@张三 同意",
            False,
            ("app-1", "sp-1"),
        ),
        (
            payload(rich(at("甘小雨", {"type": "sp", "id": "app-1"}))),
            "",
            True,
            ("app-1", "sp-1"),
        ),
        (
            payload(
                {"text": {"content": '<at id="1">甘小雨</at><at id="2">张三</at>同意'}},
                [
                    {"id": "1", "identity": {"type": "sp", "id": "sp-1", "name": "甘小雨"}},
                    {"id": "2", "identity": {"type": "user", "id": "u2", "name": "张三"}},
                ],
            ),
            "@张三 同意",
            True,
            ("app-1", "sp-1"),
        ),
        (
            payload({"text": {"content": '<at id="9">未知成员</at>同意'}}),
            "@未知成员 同意",
            False,
            ("app-1", "sp-1"),
        ),
        (
            payload(
                {"text": {"content": '<at id="1">甘小雨</at><at id="2">张三</at>同意'}},
                top_mentions=[
                    {"id": "1", "identity": {"type": "sp", "id": "sp-1", "name": "甘小雨"}},
                    {"id": "2", "identity": {"type": "user", "id": "u2", "name": "张三"}},
                ],
            ),
            "@张三 同意",
            True,
            ("AK-client", "sp-1"),
        ),
    ],
)
def test_bridge_emits_one_canonical_mention_semantics(event, expected_text, mentioned, bot_ids) -> None:
    value = run(event, bot_ids)
    assert value["text"] == expected_text
    assert value["mentioned"] is mentioned
    assert value["sender_name"] == "Alice"
    assert "raw_event" not in value
