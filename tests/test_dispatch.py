from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from ga_wps.app import GaWpsService, SeenEvents
from ga_wps.wps import WpsMessage


def message(text: str) -> WpsMessage:
    return WpsMessage(
        chat_id="chat-1",
        sender_id="u",
        sender_name="Alice",
        chat_type="p2p",
        text=text,
        event_id=text,
        mentioned=True,
        attachments=(),
        cloud_doc_links=(),
        shared_doc_ids=(),
        raw={},
    )


# TEST-CONTRACT: req=DISPATCH-01 | rejects=same-chat messages processed out of order or concurrently | gap=no serial dispatch test | revert=remove _active_chats guard in _enqueue | mock=_process_message (GA session boundary)=>SESSION-01
def test_same_chat_queue_preserves_delivery_order() -> None:
    service = GaWpsService.__new__(GaWpsService)
    service.executor = ThreadPoolExecutor(max_workers=2)
    service._queue_lock = threading.Lock()
    service._queues = {}
    service._active_chats = set()
    service._futures_lock = threading.Lock()
    service._futures = set()
    seen: list[str] = []

    def process(item: WpsMessage) -> None:
        if item.text == "first":
            time.sleep(0.05)
        seen.append(item.text)

    service._process_message = process
    service._enqueue(message("first"))
    service._enqueue(message("second"))
    service.executor.shutdown(wait=True)
    assert seen == ["first", "second"]


# TEST-CONTRACT: req=DISPATCH-02 | rejects=duplicate WPS event is processed twice across callback retries | gap=no event_id dedupe | revert=remove SeenEvents.accept guard | mock=none
def test_seen_events_deduplicates_and_survives_restart(tmp_path) -> None:
    path = tmp_path / "seen_events.jsonl"
    seen = SeenEvents(path, 3)
    assert seen.accept("e1") is True
    assert seen.accept("e1") is False
    assert SeenEvents(path, 3).accept("e1") is False
    for event_id in ("e2", "e3", "e4"):
        assert seen.accept(event_id) is True
    assert len(path.read_text(encoding="utf-8").splitlines()) < 6


# TEST-CONTRACT: req=DISPATCH-03 | rejects=/stop leaves approval waiting and queued messages active | gap=no coordinated stop path | revert=move stop after approval reply handling | mock=service boundaries
def test_stop_cancels_approval_session_and_queue(tmp_path) -> None:
    stopped: list[str] = []
    session = SimpleNamespace(abort=lambda: stopped.append("session"))
    service = GaWpsService.__new__(GaWpsService)
    service._stopped = threading.Event()
    service.seen_events = SeenEvents(tmp_path / "seen.jsonl", 10)
    service._update_wps_context = lambda message: None
    service.approvals = SimpleNamespace(
        cancel_chat=lambda chat_id, user_id: stopped.append("approval") or True,
        handle_reply=lambda message: False,
    )
    service.registry = SimpleNamespace(get_existing=lambda chat_id: session)
    service.wps = SimpleNamespace(send_markdown_split=lambda chat_id, text: stopped.append(text))
    service._queue_lock = threading.Lock()
    service._queues = {"chat-1": deque([message("queued")])}
    service.on_message(message("/stop"))
    assert {"approval", "session"}.issubset(stopped)
    assert not service._queues["chat-1"]


# TEST-CONTRACT: req=WPS-SKILL-CONTEXT-01 | rejects=Skill script requires model-supplied chat identity | gap=no runtime context file | revert=remove _update_wps_context | mock=workspace mapping boundary


def test_wps_context_binds_chat_and_remembers_sender_names(tmp_path) -> None:
    service = GaWpsService.__new__(GaWpsService)
    service._context_lock = threading.Lock()
    service.registry = SimpleNamespace(
        factory=SimpleNamespace(workspace_for=lambda chat_id: tmp_path / chat_id)
    )
    service._update_wps_context(message("hello"))

    data = __import__("json").loads(
        (tmp_path / "chat-1" / ".wps_context.json").read_text(encoding="utf-8")
    )
    assert data["chat_id"] == "chat-1"
    assert data["current_event_id"] == "hello"
    assert data["sender_names"] == {"u": "Alice"}


# TEST-CONTRACT: req=WPS-SKILL-BOOTSTRAP-01 | rejects=bootstrap and Agent refresh use different history logic | gap=duplicate implementation | revert=restore subprocess CLI boundary | mock=shared renderer boundary


def test_bootstrap_calls_shared_history_renderer(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "chat"
    workspace.mkdir()
    (workspace / ".wps_context.json").write_text(
        '{"chat_id":"chat-1","workspace":"' + str(workspace).replace('\\', '/') + '"}',
        encoding="utf-8",
    )
    service = GaWpsService.__new__(GaWpsService)
    service.wps = object()
    service._wps_skill_script = tmp_path / "wps_chat.py"
    service.settings = SimpleNamespace(recent_history_messages=30)
    captured = {}

    def fake_history(api, context, limit, *, script):
        captured.update(api=api, context=context, limit=limit, script=script)
        return "能力结果"

    monkeypatch.setattr("ga_wps.app.render_history", fake_history)
    result = service._bootstrap_wps_context(SimpleNamespace(workspace=workspace))

    assert result == "能力结果"
    assert captured == {
        "api": service.wps,
        "context": {"chat_id": "chat-1", "workspace": str(workspace).replace('\\', '/')},
        "limit": 30,
        "script": service._wps_skill_script,
    }
