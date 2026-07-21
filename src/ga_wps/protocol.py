from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

@dataclass(frozen=True)
class Mention:
    user_id: str
    company_id: str
    display_name: str

    def at_tag(self, index: int) -> str:
        return f'<at id="{index}">{self.display_name or self.user_id[:8]}</at>'

    def payload(self, index: int) -> dict[str, Any]:
        # resolve_mention guarantees company_id is non-empty (returns None otherwise),
        # so we always include it — WPS messages/create rejects empty company_id with
        # HTTP 400 (strconv.ParseInt("") fails).
        return {
            "id": str(index),
            "type": "user",
            "identity": {
                "id": self.user_id,
                "type": "user",
                "company_id": self.company_id,
            },
        }


@dataclass(frozen=True)
class WpsAttachment:
    kind: str
    storage_key: str
    name: str = ""
    size: int = 0
    mime: str = ""


@dataclass(frozen=True)
class WpsMessage:
    chat_id: str
    sender_id: str
    sender_name: str
    chat_type: str
    text: str
    event_id: str
    mentioned: bool
    attachments: tuple[WpsAttachment, ...]
    cloud_doc_links: tuple[str, ...]
    shared_doc_ids: tuple[str, ...]
    raw: dict[str, Any]

    @property
    def is_private(self) -> bool:
        return self.chat_type == "p2p"

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "WpsMessage":
        raw = payload.get("raw_event") if isinstance(payload.get("raw_event"), dict) else payload
        sender = raw.get("sender", {}) if isinstance(raw, dict) else {}
        raw_message = raw.get("message", {}) if isinstance(raw, dict) else {}
        if not isinstance(raw_message, dict):
            raw_message = {}
        attachments: list[WpsAttachment] = []
        for item in payload.get("attachments") or []:
            if not isinstance(item, dict) or not item.get("storage_key"):
                continue
            attachments.append(
                WpsAttachment(
                    kind=str(item.get("type", "file")),
                    storage_key=str(item["storage_key"]),
                    name=str(item.get("name", "")),
                    size=_safe_int(item.get("size")),
                    mime=str(item.get("mime", "")),
                )
            )
        cloud_links = tuple(
            str(item.get("link_url"))
            for item in payload.get("cloud_docs") or []
            if isinstance(item, dict) and item.get("link_url")
        )
        doc_ids = tuple(
            str(item.get("file_id") or item.get("link_id"))
            for item in payload.get("shared_docs") or []
            if isinstance(item, dict) and (item.get("file_id") or item.get("link_id"))
        )
        text = str(payload.get("text") or "").strip()
        if not text and (attachments or cloud_links or doc_ids):
            text = "[attachment-only message]"
        return cls(
            chat_id=str(payload.get("chat_id", "")),
            sender_id=str(payload.get("sender_id", "")),
            sender_name=str(
                payload.get("sender_name") or sender.get("name") or sender.get("sender_name") or ""
            ),
            chat_type=str(payload.get("chat_type", "")),
            text=text,
            event_id=str(
                payload.get("event_id")
                or raw_message.get("id")
                or raw.get("message_id", "")
            ),
            mentioned=bool(payload.get("mentioned", False)),
            attachments=tuple(attachments),
            cloud_doc_links=cloud_links,
            shared_doc_ids=doc_ids,
            raw=raw if isinstance(raw, dict) else {},
        )


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _message_timestamp(item: dict[str, Any]) -> float | None:
    message = item.get("message") if isinstance(item.get("message"), dict) else {}
    for source in (item, message):
        for key in ("create_time", "created_at", "ctime", "send_time", "timestamp", "time"):
            value = source.get(key)
            if value in (None, ""):
                continue
            try:
                number = float(value)
                return number / 1000 if number > 10_000_000_000 else number
            except (TypeError, ValueError):
                try:
                    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
    return None


def _split_markdown(text: str, limit: int = 4500) -> list[str]:
    normalized = text.replace("\r\n", "\n").strip()
    if not normalized:
        return [""]
    blocks = [block.strip() for block in normalized.split("\n\n") if block.strip()]
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if len(block) > limit:
            if current:
                chunks.append(current)
                current = ""
            for start in range(0, len(block), limit):
                chunks.append(block[start : start + limit])
            continue
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) > limit:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _wps_ok(result: dict[str, Any]) -> bool:
    if isinstance(result.get("ok"), bool):
        return bool(result["ok"])
    for key in ("code", "errcode"):
        if key in result:
            return result.get(key) in (0, "0")
    return False

def _require_ok(result: dict[str, Any], operation: str) -> dict[str, Any]:
    if not _wps_ok(result):
        code = result.get("code") or result.get("errcode") or "?"
        message = result.get("message") or result.get("msg") or "unknown error"
        raise RuntimeError(f"WPS {operation} failed: code={code} message={message}")
    return result

def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_extract_text(item) for item in value)
    if not isinstance(value, dict):
        return ""
    rich = value.get("rich_text")
    if isinstance(rich, dict):
        parts: list[str] = []
        for row in rich.get("elements") or rich.get("content") or []:
            children = row.get("elements", [row]) if isinstance(row, dict) else []
            for item in children:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    parts.append(str((item.get("text_content") or {}).get("content", "")))
                elif item.get("type") == "mention":
                    parts.append("@" + str((item.get("mention_content") or {}).get("text", "")) + " ")
        if parts:
            return "".join(parts)
    for key in ("text", "content"):
        if key in value:
            text = _extract_text(value[key])
            if text:
                return text
    return ""
