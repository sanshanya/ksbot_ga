from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .client import WpsClient
from .protocol import WpsMessage

logger = logging.getLogger(__name__)
_CONSENT = re.compile(
    r"^(?:同意|approve)(?:\s*(\d+)\s*(?:分钟|分|minutes?|min|m))?[。.!！]?$", re.I
)


@dataclass
class PendingApproval:
    chat_id: str
    user_id: str
    review: str
    event: threading.Event
    approved: bool = False
    feedback: str = ""
    window_expires_at: float = 0.0
    allow_window: bool = True


class ApprovalManager:
    def __init__(self, *, wps: WpsClient, timeout_seconds: int, audit_path: Path) -> None:
        self.wps, self.timeout_seconds, self.audit_path = wps, timeout_seconds, audit_path
        self._lock = threading.Lock()
        self._pending: dict[str, PendingApproval] = {}
        self._windows: dict[tuple[str, str], float] = {}

    def request(
        self, *, chat_id: str, user_id: str, display_name: str,
        review: str, allow_window: bool = True,
    ) -> tuple[bool, str]:
        with self._lock:
            key = (chat_id, user_id)
            stored = self._windows.get(key, 0.0)
            if stored <= time.time():
                self._windows.pop(key, None)
                stored = 0.0
            expiry = stored if allow_window else 0.0
            pending = PendingApproval(
                chat_id,
                user_id,
                review,
                threading.Event(),
                bool(expiry),
                window_expires_at=expiry,
                allow_window=allow_window,
            )
            if not expiry:
                self._pending[chat_id] = pending
        if expiry:
            self._audit(pending, "approval_window")
            return True, ""
        outcome = "send_failed"
        try:
            mention = self.wps.resolve_mention(chat_id, user_id, display_name) if user_id else None
            prompt = (
                f"**需要确认的 Kubernetes 操作**\n\n{review}\n\n"
                "回复“同意”仅执行本次；回复“同意5分钟”（分钟数可替换）开启本群、该发起人的后续受保护写限时自动同意；"
                "其他回复会取消本次操作并交给模型。"
            )
            self.wps.send_markdown_split(chat_id, prompt, mention=mention)
            wait_signaled = bool(pending.event.wait(self.timeout_seconds))
            with self._lock:
                decided = wait_signaled or pending.event.is_set()
                approved = pending.approved
                feedback = pending.feedback
                if self._pending.get(chat_id) is pending:
                    self._pending.pop(chat_id, None)
                outcome = "decision" if decided else "timeout"
            return approved, feedback
        finally:
            with self._lock:
                if self._pending.get(chat_id) is pending:
                    self._pending.pop(chat_id, None)
            self._audit(pending, outcome)

    def handle_reply(self, message: WpsMessage) -> bool:
        with self._lock:
            pending = self._pending.get(message.chat_id)
            if pending is None or message.sender_id != pending.user_id:
                return False
            text = message.text.strip()
            match = _CONSENT.fullmatch(text)
            minutes = int(match.group(1) or 0) if match else 0
            pending.approved = bool(match and (match.group(1) is None or minutes > 0))
            pending.feedback = "" if pending.approved else text
            if pending.approved and minutes and pending.allow_window:
                pending.window_expires_at = time.time() + minutes * 60
                self._windows[(pending.chat_id, pending.user_id)] = pending.window_expires_at
            pending.event.set()
        reply = (
            "操作已批准，但本次未开启自动同意窗口。"
            if pending.approved and minutes and not pending.allow_window
            else
            f"操作已批准，并开启 {minutes} 分钟自动同意窗口。"
            if pending.window_expires_at
            else "操作已批准。" if pending.approved
            else "操作已取消，意见将交给模型继续处理。"
        )
        try:
            self.wps.send_markdown_split(message.chat_id, reply)
        except Exception:
            logger.warning("failed to send approval acknowledgement", exc_info=True)
        return True

    def has_pending(self, chat_id: str) -> bool:
        with self._lock:
            return chat_id in self._pending

    def cancel_chat(self, chat_id: str, user_id: str) -> bool | None:
        with self._lock:
            pending = self._pending.get(chat_id)
            if pending is None:
                self._windows.pop((chat_id, user_id), None)
                return None
            self._windows.pop((chat_id, pending.user_id), None)
            pending.approved = False
            pending.feedback = "/stop"
            pending.event.set()
            return True

    def cancel_all(self) -> None:
        with self._lock:
            self._windows.clear()
            for pending in self._pending.values():
                pending.approved = False
                pending.event.set()

    def _audit(self, pending: PendingApproval, outcome: str) -> None:
        record = {
            "timestamp": int(time.time()), "chat_id": pending.chat_id,
            "user_id": pending.user_id, "approved": pending.approved,
            "outcome": outcome, "review": pending.review, "feedback": pending.feedback,
            "window_expires_at": int(pending.window_expires_at),
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
