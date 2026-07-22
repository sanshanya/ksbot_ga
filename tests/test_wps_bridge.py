from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
MODULE = Path(__file__).resolve().parents[1] / "bridge/wps_event_normalize.mjs"
pytestmark = pytest.mark.skipif(NODE is None, reason="node is required")


def run(event: dict, ids=("app-1", "sp-1")) -> dict:
    source = (
        f"import {{normalize}} from {json.dumps(MODULE.as_uri())};"
        f"console.log(JSON.stringify(normalize({json.dumps(event, ensure_ascii=False)},"
        f"{json.dumps(list(ids))},'fallback')));"
    )
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", source],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(result.stdout)


def event(content: dict, mentions=(), top=()) -> dict:
    return {
        "chat": {"id": "chat-1", "type": "group"},
        "sender": {"id": "user-1", "type": "user", "name": "Alice"},
        "mentions": list(top),
        "message": {
            "id": "message-1",
            "type": "text",
            "content": content,
            "mentions": list(mentions),
        },
    }


def rich(*items) -> dict:
    return {"rich_text": {"elements": [{"elements": list(items)}]}}


def mention(text: str, **identity) -> dict:
    return {"type": "mention", "mention_content": {"text": text, "identity": identity}}


def text(value: str) -> dict:
    return {"type": "text", "text_content": {"content": value}}


def inline(value: str, mentions=(), top=()) -> dict:
    return event({"text": {"content": value}}, mentions, top)


BOT = {"id": "1", "identity": {"type": "sp", "id": "sp-1", "name": "甘小雨"}}
USER = {"id": "2", "identity": {"type": "user", "id": "u2", "name": "张三"}}


@pytest.mark.parametrize(
    "payload, expected, mentioned, ids",
    [
        (
            event(rich(mention("甘小雨", type="app", app_id="app-1"), text("同意"))),
            "同意",
            True,
            ("app-1", "sp-1"),
        ),
        (
            event(rich(mention("张三", type="user", id="u2"), text("同意"))),
            "@张三 同意",
            False,
            ("app-1", "sp-1"),
        ),
        (event(rich(mention("甘小雨", type="sp", id="app-1"))), "", True, ("app-1", "sp-1")),
        (
            inline('<at id="1">甘小雨</at><at id="2">张三</at>同意', [BOT, USER]),
            "@张三 同意",
            True,
            ("app-1", "sp-1"),
        ),
        (inline('<at id="9">未知成员</at>同意'), "@未知成员 同意", False, ("app-1", "sp-1")),
        (
            inline('<at id="1">甘小雨</at><at id="2">张三</at>同意', top=[BOT, USER]),
            "@张三 同意",
            True,
            ("AK-client", "sp-1"),
        ),
    ],
)
def test_bridge_canonical_mentions(payload, expected, mentioned, ids) -> None:
    value = run(payload, ids)
    assert (value["text"], value["mentioned"], value["sender_name"]) == (
        expected,
        mentioned,
        "Alice",
    )
    assert "raw_event" not in value


def test_cloud_document_preserves_file_id_for_content_extraction() -> None:
    value = event({"file": {"type": "cloud", "cloud": {"id": "file-1", "link_url": "url"}}})
    value["message"]["type"] = "file"
    result = run(value)
    assert result["cloud_docs"] == [{"link_url": "url"}]
    assert result["shared_docs"] == [{"file_id": "file-1", "link_id": ""}]


def test_rich_document_preserves_url_for_kdocs_cli() -> None:
    value = event(
        rich(
            {
                "type": "doc",
                "doc_content": {
                    "text": "tracking",
                    "file": {
                        "id": "file-1",
                        "link_id": "link-1",
                        "link_url": "https://365.kdocs.cn/l/doc",
                    },
                },
            }
        )
    )
    result = run(value)
    assert result["cloud_docs"] == [{"link_url": "https://365.kdocs.cn/l/doc"}]
    assert result["shared_docs"] == [
        {
            "file_id": "file-1",
            "link_id": "link-1",
            "link_url": "https://365.kdocs.cn/l/doc",
        }
    ]
