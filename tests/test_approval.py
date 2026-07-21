from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from ga_wps.approval import ApprovalManager, parse_consent
from ga_wps.protocol import WpsMessage


class FakeWps:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def resolve_mention(self, *args):
        return None

    def send_markdown_split(self, _chat_id: str, text: str, mention=None) -> None:
        self.sent.append(text)


def message(text: str, sender: str = "user-1") -> WpsMessage:
    return WpsMessage(
        chat_id="chat-1",
        sender_id=sender,
        sender_name="Alice",
        chat_type="group",
        text=text,
        event_id="m1",
        mentioned=True,
        attachments=(),
        cloud_doc_links=(),
        shared_doc_ids=(),
    )


def spawn(manager: ApprovalManager, *, allow_window: bool = True):
    result: list[tuple[bool, str]] = []
    thread = threading.Thread(
        target=lambda: result.append(
            manager.request(
                chat_id="chat-1",
                user_id="user-1",
                display_name="Alice",
                review="review",
                allow_window=allow_window,
            )
        )
    )
    thread.start()
    for _ in range(100):
        if manager.wps.sent:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("approval prompt was not sent")
    return thread, result


def resolve(manager: ApprovalManager, reply: str, *, allow_window: bool = True):
    thread, result = spawn(manager, allow_window=allow_window)
    assert manager.handle_reply(message(reply)) is True
    thread.join(timeout=2)
    return result


def manager(tmp_path: Path, timeout: int = 2) -> ApprovalManager:
    return ApprovalManager(
        wps=FakeWps(), timeout_seconds=timeout, audit_path=tmp_path / "audit.jsonl"
    )


def test_only_requester_resolves_pending_approval(tmp_path: Path) -> None:
    approvals = manager(tmp_path)
    thread, result = spawn(approvals)
    assert approvals.handle_reply(message("同意", "user-2")) is False
    assert result == []
    assert approvals.handle_reply(message("同意")) is True
    thread.join(timeout=2)
    assert result == [(True, "")]


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("同意", 0),
        ("同意。", 0),
        ("approve!", 0),
        ("同意30分钟", 30),
        ("ok", None),
        ("批准", None),
        ("同意执行", None),
        ("@张三 同意", None),
        ("同意0分钟", None),
    ],
)
def test_consent_protocol_is_exact(reply: str, expected: int | None) -> None:
    assert parse_consent(reply) == expected


def test_feedback_timeout_and_stop_do_not_execute(tmp_path: Path) -> None:
    approvals = manager(tmp_path)
    feedback = "先检查 Pod 流量"
    assert resolve(approvals, feedback) == [(False, feedback)]
    record = json.loads(approvals.audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["feedback"] == feedback and record["approved"] is False

    assert manager(tmp_path / "timeout", timeout=0).request(
        chat_id="chat-1", user_id="user-1", display_name="Alice", review="review"
    ) == (False, "")

    stopped = manager(tmp_path / "stop", timeout=5)
    thread, result = spawn(stopped)
    assert stopped.cancel_chat("chat-1", "another-member") is True
    thread.join(timeout=2)
    assert result == [(False, "/stop")]


def test_timed_window_is_scoped_and_revocable(tmp_path: Path) -> None:
    approvals = manager(tmp_path)
    assert resolve(approvals, "同意5分钟") == [(True, "")]
    request = dict(display_name="Alice", review="next")
    assert approvals.request(chat_id="chat-1", user_id="user-1", **request) == (True, "")

    approvals.timeout_seconds = 0
    assert approvals.request(
        chat_id="chat-1", user_id="user-1", allow_window=False, **request
    ) == (False, "")
    assert approvals.request(chat_id="chat-1", user_id="user-1", **request) == (True, "")
    assert approvals.request(chat_id="chat-1", user_id="user-2", **request) == (False, "")
    assert approvals.request(chat_id="chat-2", user_id="user-1", **request) == (False, "")

    approvals.cancel_chat("chat-1", "user-1")
    assert approvals.request(chat_id="chat-1", user_id="user-1", **request) == (False, "")


def test_fail_closed_approval_cannot_create_window(tmp_path: Path) -> None:
    approvals = manager(tmp_path)
    assert resolve(approvals, "同意5分钟", allow_window=False) == [(True, "")]
    approvals.timeout_seconds = 0
    assert approvals.request(
        chat_id="chat-1", user_id="user-1", display_name="Alice", review="next"
    ) == (False, "")
    record = json.loads(approvals.audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["window_expires_at"] == 0


def test_boundary_reply_is_a_decision_not_timeout(tmp_path: Path, monkeypatch) -> None:
    approvals = manager(tmp_path)

    class BoundaryEvent:
        set_value = False

        def wait(self, timeout=None):
            assert approvals.handle_reply(message("同意")) is True
            return False

        def set(self):
            self.set_value = True

        def is_set(self):
            return self.set_value

    monkeypatch.setattr("ga_wps.approval.threading.Event", BoundaryEvent)
    assert approvals.request(
        chat_id="chat-1", user_id="user-1", display_name="Alice", review="review"
    ) == (True, "")
    record = json.loads(approvals.audit_path.read_text(encoding="utf-8").strip())
    assert record["outcome"] == "decision"
