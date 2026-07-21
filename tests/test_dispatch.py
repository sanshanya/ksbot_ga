from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ga_wps.app import GaWpsService, SeenEvents
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
        raw={},
    )


def attachment_message(event_id: str, *attachments: WpsAttachment) -> WpsMessage:
    return replace(message(event_id), attachments=attachments)


# TEST-CONTRACT: req=DISPATCH-01 | rejects=same-chat messages processed out of order or concurrently | gap=no serial dispatch test | revert=remove _active_chats guard in _enqueue | mock=_process_message (GA session boundary)=>SESSION-01
def test_same_chat_queue_preserves_delivery_order() -> None:
    service = GaWpsService.__new__(GaWpsService)
    service.executor = ThreadPoolExecutor(max_workers=2)
    service._queue_lock = threading.Lock()
    service._queues = {}
    service._active_chats = set()
    service._active_requesters = {}
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


# TEST-CONTRACT: req=DISPATCH-02 | rejects=duplicate WPS event is processed twice across callback retries | gap=no event_id claim and accepted record | revert=restore immediate SeenEvents persistence | mock=none
def test_seen_events_deduplicates_and_survives_restart(tmp_path) -> None:
    path = tmp_path / "seen_events.jsonl"
    seen = SeenEvents(path, 3)
    assert seen.claim("e1") is True
    assert seen.claim("e1") is False
    assert seen.record_accepted("e1") is True
    assert SeenEvents(path, 3).claim("e1") is False
    for event_id in ("e2", "e3", "e4"):
        assert seen.claim(event_id) is True
        assert seen.record_accepted(event_id) is True
    assert len(path.read_text(encoding="utf-8").splitlines()) < 6


# TEST-CONTRACT: req=DISPATCH-03 | rejects=failed queue submission permanently suppresses a callback retry | gap=durable dedupe happens before enqueue | revert=restore persistence in on_message entry | mock=executor submission boundary
def test_queue_failure_releases_claim_without_persisting_event(tmp_path) -> None:
    path = tmp_path / "seen_events.jsonl"
    service = GaWpsService.__new__(GaWpsService)
    service._stopped = threading.Event()
    service.seen_events = SeenEvents(path, 10)
    service._update_wps_context = lambda message: None
    service.approvals = SimpleNamespace(handle_reply=lambda message: False, has_pending=lambda chat_id: False)

    def enqueue(message: WpsMessage) -> None:
        raise RuntimeError("executor stopped")

    service._enqueue = enqueue

    with pytest.raises(RuntimeError, match="executor stopped"):
        service.on_message(message("queue-failure"))

    assert not path.exists()
    assert service.seen_events.claim("queue-failure") is True
    service.seen_events.release("queue-failure")


# TEST-CONTRACT: req=STOP-OWNER-01 | rejects=chat /stop closes the stopper's window instead of the active task owner's window | gap=no active requester tracking | revert=remove active requester lifecycle and owner-targeted cancellation | mock=service boundaries
def test_stop_cancels_active_requester_approval_session_and_queue(tmp_path) -> None:
    stopped: list[str] = []
    session = SimpleNamespace(abort=lambda: stopped.append("session"))
    cancelled: list[tuple[str, str]] = []
    service = GaWpsService.__new__(GaWpsService)
    service._stopped = threading.Event()
    service.seen_events = SeenEvents(tmp_path / "seen.jsonl", 10)
    service._update_wps_context = lambda message: None
    service.approvals = SimpleNamespace(
        cancel_chat=lambda chat_id, user_id: cancelled.append((chat_id, user_id)) or True,
        handle_reply=lambda message: False,
    )
    service.registry = SimpleNamespace(get_existing=lambda chat_id: session)
    service.wps = SimpleNamespace(send_markdown_split=lambda chat_id, text: stopped.append(text))
    service._queue_lock = threading.Lock()
    service._queues = {"chat-1": deque([message("queued")])}
    service._active_requesters = {"chat-1": "task-owner"}
    service.on_message(message("/stop"))
    assert cancelled == [("chat-1", "task-owner")]
    assert "session" in stopped
    assert not service._queues["chat-1"]


# TEST-CONTRACT: req=ATTACHMENT-PATH-01 | rejects=same-name attachments overwrite each other or cross event boundaries | gap=runtime uses one chat-level filename | revert=restore flat downloads filename target | mock=WPS download boundary
def test_realtime_attachment_paths_are_event_scoped_and_indexed(tmp_path) -> None:
    service = GaWpsService.__new__(GaWpsService)

    def download_attachment(*, attachment, target, **kwargs):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(attachment.storage_key, encoding="utf-8")
        return target

    service.wps = SimpleNamespace(download_attachment=download_attachment)
    session = SimpleNamespace(downloads=tmp_path / "downloads")
    first = attachment_message(
        "event-1",
        WpsAttachment("file", "a", "report.csv"),
        WpsAttachment("file", "b", "report.csv"),
    )
    first_paths, first_observations = service._download_attachments(session, first)
    second = attachment_message("event-2", WpsAttachment("file", "c", "report.csv"))
    second_paths, second_observations = service._download_attachments(session, second)

    assert first_observations == second_observations == ()
    assert [path.name for path in first_paths] == ["01_report.csv", "02_report.csv"]
    assert first_paths[0].parent != second_paths[0].parent
    assert [path.read_text(encoding="utf-8") for path in first_paths] == ["a", "b"]
    assert second_paths[0].read_text(encoding="utf-8") == "c"


# TEST-CONTRACT: req=ATTACHMENT-DOWNLOAD-OBSERVATION-01 | rejects=download failure is only logged and hidden from Agent | gap=exception is swallowed without runtime observation | revert=remove failed-download observation from _download_attachments | mock=WPS download boundary
def test_attachment_download_failure_returns_runtime_observation(tmp_path) -> None:
    service = GaWpsService.__new__(GaWpsService)

    def download_attachment(*, attachment, target, **kwargs):
        if attachment.storage_key == "bad":
            raise RuntimeError("WPS unavailable")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("ok", encoding="utf-8")
        return target

    service.wps = SimpleNamespace(download_attachment=download_attachment)
    session = SimpleNamespace(downloads=tmp_path / "downloads")
    paths, observations = service._download_attachments(
        session,
        attachment_message(
            "event-3",
            WpsAttachment("file", "bad", "missing.csv"),
            WpsAttachment("file", "good", "present.csv"),
        ),
    )

    assert [path.name for path in paths] == ["02_present.csv"]
    assert len(observations) == 1
    assert "missing.csv" in observations[0]
    assert "WPS unavailable" in observations[0]


# TEST-CONTRACT: req=ARTIFACT-DELIVERY-01 | rejects=artifact upload failure silently reports success or reruns Agent | gap=upload exception is logged only | revert=remove explicit delivery failure feedback and observation | mock=WPS upload boundary
def test_artifact_upload_failure_reports_without_rerunning_agent(tmp_path) -> None:
    service = GaWpsService.__new__(GaWpsService)
    artifact = tmp_path / "report.txt"
    artifact.write_text("report", encoding="utf-8")
    calls: list[str] = []
    runs: list[dict] = []
    session = SimpleNamespace(
        agent=SimpleNamespace(history=[]),
        downloads=tmp_path / "downloads",
        run=lambda **kwargs: (runs.append(kwargs) or ("answer", (artifact,))),
    )
    service.registry = SimpleNamespace(get=lambda chat_id: (session, False))
    service.wps = SimpleNamespace(
        send_markdown_split=lambda chat_id, text, **kwargs: calls.append(text),
        upload_file=lambda chat_id, path: (_ for _ in ()).throw(RuntimeError("upload refused")),
    )

    service._process_message(message("artifact-event"))

    assert len(runs) == 1
    assert calls == ["answer", "Artifact delivery failed for report.txt: upload refused"]
    assert session.agent.history == [
        "[Runtime observation] Artifact delivery failed for report.txt: upload refused"
    ]


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
