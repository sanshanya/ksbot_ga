from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from ga_core.ga_runtime import GaChatSession, GaSessionFactory
from ga_core.gate import KubectlAiGate
from ga_core.skills import build_skill_prompt

from .approval import ApprovalManager
from .config import WpsSettings
from .history import attachment_directory, attachment_target, history as render_history
from .callback import CallbackServer
from .client import WpsClient
from .protocol import WpsMessage

logger = logging.getLogger(__name__)


class SeenEvents:
    def __init__(self, path: Path, limit: int) -> None:
        self.path = path
        self.limit = limit
        self._lock = threading.Lock()
        self._order: deque[str] = deque()
        self._ids: set[str] = set()
        self._inflight: set[str] = set()
        self._writes = 0
        if path.is_file():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            self._writes = len(lines) % limit
            for line in lines[-limit:]:
                try:
                    event_id = str(json.loads(line).get("event_id", ""))
                except (json.JSONDecodeError, AttributeError):
                    continue
                if event_id and event_id not in self._ids:
                    self._order.append(event_id)
                    self._ids.add(event_id)
            if len(lines) > limit:
                self._compact()

    def _compact(self) -> None:
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            "".join(json.dumps({"event_id": item}) + "\n" for item in self._order),
            encoding="utf-8",
        )
        temp.replace(self.path)
        self._writes = 0

    def claim(self, event_id: str) -> bool:
        if not event_id:
            return True
        with self._lock:
            if event_id in self._ids or event_id in self._inflight:
                return False
            self._inflight.add(event_id)
            return True

    def release(self, event_id: str) -> None:
        if not event_id:
            return
        with self._lock:
            self._inflight.discard(event_id)

    def record_accepted(self, event_id: str) -> bool:
        if not event_id:
            return True
        with self._lock:
            if event_id in self._ids:
                self._inflight.discard(event_id)
                return False
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps({"event_id": event_id, "seen_at": int(time.time())}) + "\n"
                    )
            except Exception:
                self._inflight.discard(event_id)
                raise
            if len(self._order) >= self.limit:
                self._ids.discard(self._order.popleft())
            self._order.append(event_id)
            self._ids.add(event_id)
            self._inflight.discard(event_id)
            self._writes += 1
            if self._writes >= self.limit:
                self._compact()
            return True


class ChatRegistry:
    def __init__(self, factory: GaSessionFactory) -> None:
        self.factory = factory
        self._lock = threading.Lock()
        self._sessions: dict[str, GaChatSession] = {}

    def get(self, chat_id: str) -> tuple[GaChatSession, bool]:
        with self._lock:
            session = self._sessions.get(chat_id)
            created = session is None
            if session is None:
                session = self.factory.create(chat_id)
                self._sessions[chat_id] = session
            return session, created

    def get_existing(self, chat_id: str) -> GaChatSession | None:
        with self._lock:
            return self._sessions.get(chat_id)


class GaWpsService:
    def __init__(self, settings: WpsSettings) -> None:
        self.settings = settings
        self.wps = WpsClient(
            api_base=settings.api_base,
            client_id=settings.client_id,
            client_secret=settings.client_secret,
        )
        self.gate = KubectlAiGate(
            inventory_path=settings.core.cluster_config,
            base_url=settings.core.gate_base_url,
            api_key=settings.core.gate_api_key,
            model=settings.core.gate_model,
            timeout=settings.core.gate_timeout_seconds,
            probe_timeout=settings.core.kubectl_probe_timeout_seconds,
        )
        self.approvals = ApprovalManager(
            wps=self.wps,
            timeout_seconds=settings.approval_timeout_seconds,
            audit_path=settings.core.runtime_root / "approval.jsonl",
        )
        base_prompt = (settings.core.project_root / "config" / "system_prompt.md").read_text(
            encoding="utf-8"
        )
        shared_prompt = base_prompt + build_skill_prompt(settings.core.project_root / "skills")
        factory = GaSessionFactory(
            ga_root=settings.core.ga_root,
            runtime_root=settings.core.runtime_root,
            shared_prompt=shared_prompt,
            gate=self.gate,
            approval_sink=self.approvals,
            max_turns=settings.core.max_turns,
        )
        self.registry = ChatRegistry(factory)
        self.executor = ThreadPoolExecutor(
            max_workers=settings.max_workers, thread_name_prefix="ga-wps-chat"
        )
        self.callback = CallbackServer(
            settings.callback_host,
            settings.callback_port,
            settings.callback_secret,
            self.on_message,
        )
        self.bridge: subprocess.Popen[str] | None = None
        self._futures: set[Future[None]] = set()
        self._futures_lock = threading.Lock()
        self._queue_lock = threading.Lock()
        self._context_lock = threading.Lock()
        self._wps_skill_script = (
            settings.core.project_root / "skills" / "wps-chat" / "scripts" / "wps_chat.py"
        ).resolve()
        self._queues: dict[str, deque[WpsMessage]] = {}
        self._active_chats: set[str] = set()
        self._active_requesters: dict[str, str] = {}
        self._stopped = threading.Event()
        self.seen_events = SeenEvents(
            settings.core.runtime_root / "seen_events.jsonl", settings.seen_events_limit
        )

    def start(self) -> None:
        sp_id = (
            str(self.wps.current_service_principal()["id"]) if self.settings.launch_bridge else ""
        )
        self.callback.start()
        if self.settings.launch_bridge:
            self.bridge = self._start_bridge(sp_id)
        logger.info(
            "GA+WPS service started callback=http://%s:%s/wps/callback",
            self.settings.callback_host,
            self.settings.callback_port,
        )

    def on_message(self, message: WpsMessage) -> None:
        if self._stopped.is_set() or not self.seen_events.claim(message.event_id):
            return
        accepted = False
        try:
            self._update_wps_context(message)
            if message.text.strip().lower() in {"/stop", "stop", "停止"}:
                with self._queue_lock:
                    requester = self._active_requesters.get(message.chat_id, message.sender_id)
                self.approvals.cancel_chat(message.chat_id, requester)
                session = self.registry.get_existing(message.chat_id)
                if session is not None:
                    session.abort()
                reply = (
                    "已停止当前任务。" if session is not None else "当前会话没有正在运行的任务。"
                ) + " 自动同意窗口已关闭。"
                with self._queue_lock:
                    if queue := self._queues.get(message.chat_id):
                        queue.clear()
                self.seen_events.record_accepted(message.event_id)
                accepted = True
                self.wps.send_markdown_split(message.chat_id, reply)
                return
            if self.approvals.handle_reply(message):
                self.seen_events.record_accepted(message.event_id)
                accepted = True
                return
            if not message.is_private and not message.mentioned:
                return
            if self.approvals.has_pending(message.chat_id):
                self.wps.send_markdown_split(
                    message.chat_id,
                    "该操作正在等待任务发起人确认。其他成员的消息不会改变审批结果。",
                )
                self.seen_events.record_accepted(message.event_id)
                accepted = True
                return
            self._enqueue(message)
            self.seen_events.record_accepted(message.event_id)
            accepted = True
        finally:
            if not accepted:
                self.seen_events.release(message.event_id)

    def _enqueue(self, message: WpsMessage) -> None:
        with self._queue_lock:
            queue = self._queues.setdefault(message.chat_id, deque())
            queue.append(message)
            if message.chat_id in self._active_chats:
                return
            self._active_chats.add(message.chat_id)
            try:
                future = self.executor.submit(self._drain_chat, message.chat_id)
            except Exception:
                queue.pop()
                self._active_chats.discard(message.chat_id)
                if not queue:
                    self._queues.pop(message.chat_id, None)
                raise
        with self._futures_lock:
            self._futures.add(future)
        future.add_done_callback(self._future_done)

    def _drain_chat(self, chat_id: str) -> None:
        while True:
            with self._queue_lock:
                queue = self._queues.get(chat_id)
                if not queue:
                    self._queues.pop(chat_id, None)
                    self._active_chats.discard(chat_id)
                    return
                message = queue.popleft()
                self._active_requesters[chat_id] = message.sender_id
            try:
                self._process_message(message)
            except Exception as exc:
                logger.exception("chat task failed chat_id=%s", chat_id)
                try:
                    self.wps.send_markdown_split(
                        chat_id, f"Task failed before a final answer: {type(exc).__name__}: {exc}"
                    )
                except Exception:
                    logger.exception("failed to deliver task failure chat_id=%s", chat_id)
            finally:
                with self._queue_lock:
                    if self._active_requesters.get(chat_id) == message.sender_id:
                        self._active_requesters.pop(chat_id, None)

    def _future_done(self, future: Future[None]) -> None:
        with self._futures_lock:
            self._futures.discard(future)
        try:
            future.result()
        except Exception:
            logger.exception("chat task failed")

    def _process_message(self, message: WpsMessage) -> None:
        session, created = self.registry.get(message.chat_id)
        attachment_paths, observations = self._download_attachments(session, message)
        if created:
            observations = (*observations, self._bootstrap_wps_context(session))
        text, files = session.run(
            chat_id=message.chat_id,
            user_id=message.sender_id,
            display_name=message.sender_name or f"User({message.sender_id[:6]})",
            user_text=message.text,
            attachment_paths=attachment_paths,
            runtime_observations=observations,
        )
        mention = None
        if not message.is_private and message.sender_id:
            mention = self.wps.resolve_mention(
                message.sender_id, message.sender_name or f"User({message.sender_id[:6]})"
            )
        self.wps.send_markdown_split(message.chat_id, text, mention=mention)
        for path in files:
            try:
                self.wps.upload_file(message.chat_id, path)
            except Exception as exc:
                failure = f"Artifact delivery failed for {path.name}: {exc}"
                session.agent.history.append(f"[Runtime observation] {failure}")
                logger.exception("failed to upload artifact %s", path)
                try:
                    self.wps.send_markdown_split(message.chat_id, failure)
                except Exception:
                    logger.exception("failed to report artifact delivery failure %s", path)

    def _update_wps_context(self, message: WpsMessage) -> None:
        workspace = self.registry.factory.workspace_for(message.chat_id)
        workspace.mkdir(parents=True, exist_ok=True)
        path = workspace / ".wps_context.json"
        with self._context_lock:
            data: dict[str, object] = {}
            if path.is_file():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data = loaded
                except (json.JSONDecodeError, OSError):
                    logger.warning("invalid WPS runtime context, rebuilding: %s", path)
            names = data.get("sender_names")
            names = names if isinstance(names, dict) else {}
            if message.sender_id and message.sender_name:
                names[message.sender_id] = message.sender_name
            data.update(
                version=1,
                chat_id=message.chat_id,
                workspace=str(workspace.resolve()),
                current_event_id=message.event_id,
                sender_names=names,
            )
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(path)

    def _bootstrap_wps_context(self, session: GaChatSession) -> str:
        context_path = session.workspace / ".wps_context.json"
        try:
            context = json.loads(context_path.read_text(encoding="utf-8"))
            return render_history(
                self.wps,
                context,
                self.settings.recent_history_messages,
                script=self._wps_skill_script,
            )
        except Exception as exc:
            logger.warning("WPS bootstrap capability failed: %s", exc, exc_info=True)
            retry = f'python "{self._wps_skill_script}" history --limit {self.settings.recent_history_messages}'
            return (
                "WPS chat history capability failed; no history was injected. "
                f"Error: {type(exc).__name__}: {exc}\nRetry with GA code_run: {retry}"
            )

    def _download_attachments(
        self, session: GaChatSession, message: WpsMessage
    ) -> tuple[tuple[Path, ...], tuple[str, ...]]:
        paths: list[Path] = []
        observations: list[str] = []
        for index, attachment in enumerate(message.attachments, 1):
            target = attachment_target(
                session.downloads, message.event_id, index, attachment.name, attachment.kind
            )
            try:
                paths.append(
                    self.wps.download_attachment(
                        chat_id=message.chat_id,
                        message_id=message.event_id,
                        attachment=attachment,
                        target=target,
                    )
                )
            except Exception as exc:
                observations.append(
                    f"Attachment download failed for {attachment.name or attachment.kind} "
                    f"at {target}: {exc}"
                )
                logger.exception("failed to download WPS attachment")
        event_dir = attachment_directory(session.downloads, message.event_id)
        for url in message.cloud_doc_links:
            path = event_dir / "cloud_docs.txt"
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(url + "\n")
                paths.append(path)
            except Exception as exc:
                observations.append(f"Cloud document record failed for {url}: {exc}")
        if message.shared_doc_ids:
            path = event_dir / "shared_doc_ids.txt"
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write("\n".join(message.shared_doc_ids) + "\n")
                paths.append(path)
            except Exception as exc:
                observations.append(f"Shared document record failed: {exc}")
        return tuple(dict.fromkeys(paths)), tuple(observations)

    def _start_bridge(self, sp_id: str) -> subprocess.Popen[str]:
        bridge_dir = self.settings.core.project_root / "bridge"
        env = os.environ.copy()
        env["WPS_EVENT_BRIDGE_TARGET"] = (
            f"http://{self.settings.callback_host}:{self.settings.callback_port}/wps/callback"
        )
        if self.settings.callback_secret:
            env["WPS_EVENT_BRIDGE_SECRET"] = self.settings.callback_secret
        env["WPS365_SP_ID"] = sp_id
        return subprocess.Popen(
            [self.settings.bridge_node, "wps_event_bridge.mjs"],
            cwd=bridge_dir,
            env=env,
            text=True,
        )

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        self.approvals.cancel_all()
        self.callback.stop()
        if self.bridge and self.bridge.poll() is None:
            self.bridge.terminate()
            try:
                self.bridge.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.bridge.kill()
        self.executor.shutdown(wait=True, cancel_futures=False)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = WpsSettings.from_env()
    settings.validate()
    service = GaWpsService(settings)
    service.start()

    def stop_handler(_signum: int, _frame: object) -> None:
        service.stop()

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    try:
        while not service._stopped.wait(1):
            if service.bridge and service.bridge.poll() is not None:
                raise RuntimeError(f"WPS event bridge exited with {service.bridge.returncode}")
    finally:
        service.stop()


if __name__ == "__main__":
    main()
