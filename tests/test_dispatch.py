from __future__ import annotations

import json
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ga_wps.app import ChatRegistry, GaWpsService, SeenEvents
from ga_wps.protocol import WpsAttachment, WpsMessage


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
    )


def test_same_chat_queue_is_serial() -> None:
    service = GaWpsService.__new__(GaWpsService)
    service.executor = ThreadPoolExecutor(max_workers=2)
    service._stopped = threading.Event()
    service._queue_lock = threading.Lock()
    service._queues, service._active_chats = {}, set()
    service._futures_lock, service._futures = threading.Lock(), set()
    seen = []

    def process(item):
        if item.text == "first":
            time.sleep(0.05)
        seen.append(item.text)

    service._process_message = process
    service._enqueue(message("first"))
    service._enqueue(message("second"))
    service.executor.shutdown(wait=True)
    assert seen == ["first", "second"]


def test_event_claim_persists_only_after_acceptance(tmp_path) -> None:
    path = tmp_path / "seen.jsonl"
    seen = SeenEvents(path, 3)
    assert seen.claim("e1") and not seen.claim("e1")
    assert seen.record_accepted("e1")
    assert not SeenEvents(path, 3).claim("e1")

    service = GaWpsService.__new__(GaWpsService)
    service._stopped = threading.Event()
    service.seen_events = SeenEvents(tmp_path / "failed.jsonl", 10)
    service._update_wps_context = lambda _message: None
    service.approvals = SimpleNamespace(handle_reply=lambda _m: False, has_pending=lambda _c: False)
    service._enqueue = lambda _m: (_ for _ in ()).throw(RuntimeError("executor stopped"))
    with pytest.raises(RuntimeError, match="executor stopped"):
        service.on_message(message("retryable"))
    assert service.seen_events.claim("retryable")


def test_stop_clears_chat_approval_session_and_queue(tmp_path) -> None:
    actions = []
    service = GaWpsService.__new__(GaWpsService)
    service._stopped = threading.Event()
    service.seen_events = SeenEvents(tmp_path / "seen.jsonl", 10)
    service._update_wps_context = lambda _message: None
    service.approvals = SimpleNamespace(
        cancel_chat=lambda chat: actions.append(chat),
        handle_reply=lambda _message: False,
    )
    service.registry = SimpleNamespace(
        get_existing=lambda _chat: SimpleNamespace(abort=lambda: actions.append("abort"))
    )
    service.wps = SimpleNamespace(send_markdown_split=lambda *_args: None)
    service._queue_lock = threading.Lock()
    service._queues = {"chat-1": deque([message("queued")])}
    service.on_message(message("/stop"))
    assert actions == ["chat-1", "abort"]
    assert not service._queues["chat-1"]


def test_attachment_paths_are_stable_and_failures_are_observed(tmp_path) -> None:
    service = GaWpsService.__new__(GaWpsService)

    def download(*, attachment, target, **_kwargs):
        if attachment.storage_key == "bad":
            raise RuntimeError("WPS unavailable")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(attachment.storage_key, encoding="utf-8")
        return target

    service.wps = SimpleNamespace(download_attachment=download)
    session = SimpleNamespace(downloads=tmp_path / "downloads")
    first = replace(
        message("event-1"),
        attachments=(
            WpsAttachment("file", "a", "report.csv"),
            WpsAttachment("file", "b", "report.csv"),
        ),
    )
    second = replace(
        message("event-2"),
        attachments=(
            WpsAttachment("file", "bad", "missing.csv"),
            WpsAttachment("file", "c", "report.csv"),
        ),
    )
    first_paths, _ = service._download_attachments(session, first)
    second_paths, observations = service._download_attachments(session, second)
    assert [path.name for path in first_paths] == ["01_report.csv", "02_report.csv"]
    assert first_paths[0].parent != second_paths[0].parent
    assert [path.read_text(encoding="utf-8") for path in first_paths] == ["a", "b"]
    assert second_paths[0].name == "02_report.csv"
    assert "missing.csv" in observations[0] and "WPS unavailable" in observations[0]

    doc_paths, doc_observations = service._download_attachments(
        session,
        replace(message("event-3"), cloud_doc_links=("https://365.kdocs.cn/l/doc",), shared_doc_ids=("file-3",)),
    )
    assert not doc_observations
    assert [path.name for path in doc_paths] == ["cloud_docs.txt", "shared_doc_ids.txt"]
    assert doc_paths[0].read_text(encoding="utf-8").strip() == "https://365.kdocs.cn/l/doc"
    assert doc_paths[1].read_text(encoding="utf-8").strip() == "file-3"


def test_artifact_failure_reports_without_rerunning_agent(tmp_path) -> None:
    artifact = tmp_path / "report.txt"
    artifact.write_text("report", encoding="utf-8")
    calls, runs = [], []
    session = SimpleNamespace(
        agent=SimpleNamespace(history=[]),
        downloads=tmp_path / "downloads",
        run=lambda **kwargs: (runs.append(kwargs) or ("answer", (artifact,))),
    )
    service = GaWpsService.__new__(GaWpsService)
    service.registry = SimpleNamespace(get=lambda _chat: (session, False))
    service.wps = SimpleNamespace(
        send_markdown_split=lambda _chat, text, **_kwargs: calls.append(text),
        upload_file=lambda *_args: (_ for _ in ()).throw(RuntimeError("upload refused")),
    )
    service._process_message(message("artifact"))
    assert len(runs) == 1
    assert calls == ["answer", "Artifact delivery failed for report.txt: upload refused"]
    assert "upload refused" in session.agent.history[0]


def test_context_and_recent_history_use_one_runtime_observation_path(tmp_path, monkeypatch) -> None:
    service = GaWpsService.__new__(GaWpsService)
    service._context_lock = threading.Lock()
    service.registry = SimpleNamespace(
        factory=SimpleNamespace(workspace_for=lambda chat: tmp_path / chat)
    )
    service._update_wps_context(message("hello"))
    workspace = tmp_path / "chat-1"
    context = json.loads((workspace / ".wps_context.json").read_text(encoding="utf-8"))
    assert context["current_event_id"] == "hello"
    assert context["sender_names"] == {"u": "Alice"}

    service.wps = object()
    service._wps_skill_script = tmp_path / "wps_chat.py"
    service.settings = SimpleNamespace(recent_history_messages=30)
    monkeypatch.setattr("ga_wps.app.render_history", lambda *args, **kwargs: "history observation")
    assert service._bootstrap_wps_context(SimpleNamespace(workspace=workspace)) == "history observation"



def test_shutdown_aborts_sessions_drops_queue_and_has_a_deadline() -> None:
    release, started, actions = threading.Event(), threading.Event(), []
    executor = ThreadPoolExecutor(max_workers=1)
    running = executor.submit(lambda: (started.set(), release.wait())[1])
    assert started.wait(1)
    queued = executor.submit(lambda: actions.append("queued"))
    registry = ChatRegistry.__new__(ChatRegistry)
    registry._lock = threading.Lock()
    registry._sessions = {
        key: SimpleNamespace(abort=lambda key=key: actions.append(key)) for key in ("a", "b")
    }
    service = GaWpsService.__new__(GaWpsService)
    service.settings = SimpleNamespace(shutdown_timeout_seconds=0.05)
    service._stopped, service._stop_lock, service._stop_result = threading.Event(), threading.RLock(), None
    service._queue_lock, service._queues = threading.Lock(), {"c": deque([message("queued")])}
    service._futures_lock, service._futures = threading.Lock(), {running, queued}
    service.executor, service.registry, service.bridge = executor, registry, None
    service.approvals = SimpleNamespace(cancel_all=lambda: actions.append("approvals"))
    service.callback = SimpleNamespace(stop=lambda: actions.append("callback"))
    start = time.monotonic()
    assert service.stop() is False and time.monotonic() - start < 0.5
    assert not service._queues["c"] and queued.cancelled()
    assert actions[:4] == ["approvals", "a", "b", "callback"]
    release.set()
    running.result(1)
