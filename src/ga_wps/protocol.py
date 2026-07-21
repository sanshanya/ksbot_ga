from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Mention:
    user_id: str
    company_id: str
    display_name: str

    def at_tag(self, index: int) -> str:
        return f'<at id="{index}">{self.display_name or self.user_id[:8]}</at>'

    def payload(self, index: int) -> dict[str, Any]:
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

    @property
    def is_private(self) -> bool:
        return self.chat_type == "p2p"

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "WpsMessage":
        strings = ("chat_id", "sender_id", "sender_name", "chat_type", "text", "event_id")
        lists = ("attachments", "cloud_docs", "shared_docs")
        invalid = [key for key in strings if not isinstance(payload.get(key), str)]
        invalid += [key for key in lists if not isinstance(payload.get(key), list)]
        if not isinstance(payload.get("mentioned"), bool):
            invalid.append("mentioned")
        if invalid:
            raise ValueError(f"invalid canonical WPS payload fields: {', '.join(invalid)}")
        if not all(payload[key] for key in ("chat_id", "sender_id", "event_id")):
            raise ValueError("chat_id, sender_id, and event_id are required")
        attachments = tuple(
            WpsAttachment(
                str(item.get("type") or "file"),
                str(item["storage_key"]),
                str(item.get("name") or ""),
                _int(item.get("size")),
                str(item.get("mime") or ""),
            )
            for item in payload["attachments"]
            if isinstance(item, dict) and item.get("storage_key")
        )
        cloud = tuple(
            str(item["link_url"])
            for item in payload["cloud_docs"]
            if isinstance(item, dict) and item.get("link_url")
        )
        docs = tuple(
            str(item.get("file_id") or item.get("link_id"))
            for item in payload["shared_docs"]
            if isinstance(item, dict) and (item.get("file_id") or item.get("link_id"))
        )
        text = payload["text"].strip() or (
            "[attachment-only message]" if attachments or cloud or docs else ""
        )
        return cls(
            payload["chat_id"],
            payload["sender_id"],
            payload["sender_name"],
            payload["chat_type"],
            text,
            payload["event_id"],
            payload["mentioned"],
            attachments,
            cloud,
            docs,
        )


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
