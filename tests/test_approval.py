from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from ga_wps.approval import ApprovalManager
from ga_wps.protocol import WpsMessage


class FakeWps:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def resolve_mention(self, chat_id: str, user_id: str, display_name: str):
        return None

    def send_markdown_split(self, chat_id: str, text: str, mention=None) -> None:
        self.sent.append((chat_id, text))


def _message(text: str, sender_id: str = "user-1") -> WpsMessage:
    return WpsMessage(
        chat_id="chat-1",
        sender_id=sender_id,
        sender_name="Alice",
        chat_type="group",
        text=text,
        event_id="m1",
        mentioned=True,
        attachments=(),
        cloud_doc_links=(),
        shared_doc_ids=(),
        raw={},
    )


def _wait_for_prompt(wps: FakeWps) -> None:
    for _ in range(100):
        if wps.sent:
            return
        time.sleep(0.01)
    raise AssertionError("approval prompt was not sent")


def _spawn_request(manager: ApprovalManager) -> tuple[threading.Thread, list[tuple[bool, str]]]:
    result: list[tuple[bool, str]] = []
    thread = threading.Thread(
        target=lambda: result.append(
            manager.request(
                chat_id="chat-1",
                user_id="user-1",
                display_name="Alice",
                review="将删除 kaic-kis 中的 Pod，可能短暂降低服务容量。",
            )
        )
    )
    return thread, result


# TEST-CONTRACT: req=APPROVAL-01 | rejects=task requester approval does not resume write | gap=no requester approval path | revert=remove approved=True branch | mock=FakeWps boundary


def test_requester_approval_unblocks_waiter(tmp_path: Path) -> None:
    wps = FakeWps()
    manager = ApprovalManager(wps=wps, timeout_seconds=2, audit_path=tmp_path / "audit.jsonl")
    thread, result = _spawn_request(manager)
    thread.start()
    _wait_for_prompt(wps)
    assert manager.handle_reply(_message("同意"))
    thread.join(timeout=2)
    assert result == [(True, "")]


# TEST-CONTRACT: req=APPROVAL-02 | rejects=another group member can approve requester's write | gap=no requester identity check | revert=remove sender_id comparison | mock=FakeWps boundary


def test_other_group_member_cannot_resolve_approval(tmp_path: Path) -> None:
    wps = FakeWps()
    manager = ApprovalManager(wps=wps, timeout_seconds=5, audit_path=tmp_path / "audit.jsonl")
    thread, result = _spawn_request(manager)
    thread.start()
    _wait_for_prompt(wps)
    try:
        assert manager.handle_reply(_message("同意", sender_id="user-2")) is False
        assert result == []
    finally:
        manager.cancel_all()
        thread.join(timeout=2)


# TEST-CONTRACT: req=APPROVAL-03 | rejects=requester feedback is ignored instead of cancelling and returning to model | gap=no feedback path | revert=only accept approval keywords | mock=FakeWps boundary


def test_requester_other_text_becomes_feedback(tmp_path: Path) -> None:
    wps = FakeWps()
    audit = tmp_path / "audit.jsonl"
    manager = ApprovalManager(wps=wps, timeout_seconds=2, audit_path=audit)
    thread, result = _spawn_request(manager)
    thread.start()
    _wait_for_prompt(wps)
    feedback = "先检查这个 Pod 是否还有流量"
    assert manager.handle_reply(_message(feedback))
    thread.join(timeout=2)
    assert result == [(False, feedback)]
    record = json.loads(audit.read_text(encoding="utf-8").strip())
    assert record["feedback"] == feedback
    assert record["approved"] is False


# TEST-CONTRACT: req=APPROVAL-04 | rejects=timeout is treated as approval | gap=no timeout path | revert=treat unresolved as approved | mock=none


def test_approval_timeout_is_rejection(tmp_path: Path) -> None:
    manager = ApprovalManager(wps=FakeWps(), timeout_seconds=1, audit_path=tmp_path / "audit.jsonl")
    assert manager.request(
        chat_id="chat-1", user_id="user-1", display_name="Alice", review="review"
    ) == (False, "")


# TEST-CONTRACT: req=STOP-CHAT-EMERGENCY-01 | rejects=/stop is limited to the message sender during a chat emergency | gap=approval cancellation enforces requester ownership | revert=restore requester check in cancel_chat | mock=FakeWps boundary
def test_any_chat_member_can_cancel_pending_approval(tmp_path: Path) -> None:
    manager = ApprovalManager(wps=FakeWps(), timeout_seconds=5, audit_path=tmp_path / "audit.jsonl")
    thread, result = _spawn_request(manager)
    thread.start()
    _wait_for_prompt(manager.wps)
    assert manager.cancel_chat("chat-1", "user-2") is True
    thread.join(timeout=2)
    assert result == [(False, "/stop")]


# TEST-CONTRACT: req=APPROVAL-06 | rejects=ambiguous consent words execute a protected write | gap=approval vocabulary broader than explicit consent | revert=restore yes/ok/允许 fast paths | mock=FakeWps boundary
@pytest.mark.parametrize("reply", ["ok", "允许", "批准", "可以执行", "同意，执行完告诉我"])
def test_only_exact_consent_approves(tmp_path: Path, reply: str) -> None:
    manager = ApprovalManager(wps=FakeWps(), timeout_seconds=2, audit_path=tmp_path / "audit.jsonl")
    thread, result = _spawn_request(manager)
    thread.start()
    _wait_for_prompt(manager.wps)
    assert manager.handle_reply(_message(reply))
    thread.join(timeout=2)
    assert result == [(False, reply)]


@pytest.mark.parametrize("reply", ["同意。", "同意!", "approve.", "approve！", "同意30分钟", "approve 10m"])
def test_consent_allows_terminal_punctuation(tmp_path: Path, reply: str) -> None:
    manager = ApprovalManager(wps=FakeWps(), timeout_seconds=2, audit_path=tmp_path / "audit.jsonl")
    thread, result = _spawn_request(manager)
    thread.start()
    _wait_for_prompt(manager.wps)
    assert manager.handle_reply(_message(reply)) is True
    thread.join(timeout=2)
    assert result == [(True, "")]


@pytest.mark.parametrize("reply", ["同意，执行", "同意执行", "@张三 同意", "同意 @张三"])
def test_consent_with_words_or_other_mentions_remains_feedback(tmp_path: Path, reply: str) -> None:
    manager = ApprovalManager(wps=FakeWps(), timeout_seconds=2, audit_path=tmp_path / "audit.jsonl")
    thread, result = _spawn_request(manager)
    thread.start()
    _wait_for_prompt(manager.wps)
    assert manager.handle_reply(_message(reply)) is True
    thread.join(timeout=2)
    assert result == [(False, reply)]

# TEST-CONTRACT: req=APPROVAL-WINDOW-01 | rejects=explicit timed consent still prompts for every protected write | gap=no chat-user approval lease | revert=remove _windows fast path | mock=FakeWps boundary

def test_timed_consent_auto_approves_until_revoked(tmp_path: Path) -> None:
    wps = FakeWps()
    manager = ApprovalManager(wps=wps, timeout_seconds=2, audit_path=tmp_path / "audit.jsonl")
    thread, result = _spawn_request(manager)
    thread.start()
    _wait_for_prompt(wps)
    assert manager.handle_reply(_message("同意5分钟"))
    thread.join(timeout=2)
    assert result == [(True, "")]
    assert manager.request(
        chat_id="chat-1", user_id="user-1", display_name="Alice", review="下一条生产写"
    ) == (True, "")
    assert len(wps.sent) == 2
    manager.timeout_seconds = 0
    assert manager.request(chat_id="chat-1", user_id="user-1", display_name="Alice", review="Gate 失败", allow_window=False) == (False, "")
    assert manager.request(chat_id="chat-1", user_id="user-1", display_name="Alice", review="Gate 恢复") == (True, "")
    assert manager.request(chat_id="chat-1", user_id="user-2", display_name="Bob", review="他人") == (False, "")
    assert manager.request(chat_id="chat-2", user_id="user-1", display_name="Alice", review="其他群") == (False, "")
    assert manager.cancel_chat("chat-1", "user-1") is None
    assert manager.request(
        chat_id="chat-1", user_id="user-1", display_name="Alice", review="窗口已关闭"
    ) == (False, "")
    records = [json.loads(line) for line in manager.audit_path.read_text(encoding="utf-8").splitlines()]
    assert [record["outcome"] for record in records] == ["decision", "approval_window", "timeout", "approval_window"] + ["timeout"] * 3
    assert records[0]["window_expires_at"] > records[0]["timestamp"]


# TEST-CONTRACT: req=APPROVAL-WINDOW-02 | rejects=Gate-failure approval can create a new timed auto-approval window | gap=PendingApproval does not retain allow_window | revert=remove allow_window guard from handle_reply | mock=FakeWps boundary
def test_disallowed_window_cannot_be_created_by_timed_consent(tmp_path: Path) -> None:
    wps = FakeWps()
    manager = ApprovalManager(wps=wps, timeout_seconds=2, audit_path=tmp_path / "audit.jsonl")
    result: list[tuple[bool, str]] = []
    thread = threading.Thread(
        target=lambda: result.append(
            manager.request(
                chat_id="chat-1",
                user_id="user-1",
                display_name="Alice",
                review="Gate unavailable",
                allow_window=False,
            )
        )
    )
    thread.start()
    _wait_for_prompt(wps)
    assert manager.handle_reply(_message("同意5分钟")) is True
    thread.join(timeout=2)

    assert result == [(True, "")]
    manager.timeout_seconds = 0
    assert manager.request(
        chat_id="chat-1", user_id="user-1", display_name="Alice", review="下一条操作"
    ) == (False, "")
    record = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["window_expires_at"] == 0


# TEST-CONTRACT: req=APPROVAL-TIMEOUT-01 | rejects=approval arriving at the wait boundary is audited as timeout | gap=wait result and pending decision are finalized separately | revert=move pending cleanup back outside the decision lock | mock=controlled Event boundary
def test_approval_boundary_reply_is_finalized_as_decision(tmp_path: Path, monkeypatch) -> None:
    manager = ApprovalManager(wps=FakeWps(), timeout_seconds=2, audit_path=tmp_path / "audit.jsonl")

    class BoundaryResult:
        def __bool__(self) -> bool:
            assert manager.handle_reply(_message("同意")) is True
            return False

    class BoundaryEvent:
        def __init__(self) -> None:
            self._set = False

        def wait(self, timeout: float | None = None) -> BoundaryResult:
            return BoundaryResult()

        def set(self) -> None:
            self._set = True

        def is_set(self) -> bool:
            return self._set

    monkeypatch.setattr("ga_wps.approval.threading.Event", BoundaryEvent)
    result = manager.request(
        chat_id="chat-1", user_id="user-1", display_name="Alice", review="review"
    )

    record = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip())
    assert result == (True, "")
    assert record["approved"] is True
    assert record["outcome"] == "decision"
