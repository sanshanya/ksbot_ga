from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .client import WpsClient
from .protocol import WpsAttachment, _int

ALL_HISTORY_START = 1  # WPS treats 0 as omitted; 1 requests all accessible history.


def _timestamp(item: dict[str, Any]) -> float | None:
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
                    pass
    return None


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(map(_extract_text, value))
    if not isinstance(value, dict):
        return ""
    if isinstance(rich := value.get("rich_text"), dict):
        parts: list[str] = []
        for row in rich.get("elements") or rich.get("content") or []:
            for item in row.get("elements", [row]) if isinstance(row, dict) else []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    parts.append(str((item.get("text_content") or {}).get("content", "")))
                elif item.get("type") == "mention":
                    parts.append("@" + str((item.get("mention_content") or {}).get("text", "")) + " ")
        if parts:
            return "".join(parts)
    for key in ("text", "content"):
        if key in value and (text := _extract_text(value[key])):
            return text
    return ""


def message_id(item: dict[str, Any]) -> str:
    nested = item.get("message") if isinstance(item.get("message"), dict) else {}
    return str(item.get("id") or item.get("message_id") or nested.get("id") or "")


def attachment_directory(downloads: Path, message_id: str) -> Path:
    digest = hashlib.sha256(message_id.encode("utf-8", errors="replace")).hexdigest()[:12]
    return downloads / digest


def attachment_target(
    downloads: Path, message_id: str, index: int, name: str, kind: str
) -> Path:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in name)[:160]
    return attachment_directory(downloads, message_id) / f"{index:02d}_{safe or kind}"


def message_content(item: dict[str, Any]) -> Any:
    message = item.get("message") if isinstance(item.get("message"), dict) else item
    value = message.get("content", item.get("content"))
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    return value


def message_attachments(item: dict[str, Any]) -> tuple[WpsAttachment, ...]:
    value = message_content(item)
    if not isinstance(value, dict):
        return ()
    file = value.get("file") if isinstance(value.get("file"), dict) else {}
    sticker = value.get("sticker") if isinstance(value.get("sticker"), dict) else {}
    candidates = [("file", file.get("local")), ("sticker", sticker.get("image"))]
    candidates += [(kind, value.get(kind)) for kind in ("image", "audio", "video")]
    return tuple(
        WpsAttachment(
            kind,
            str(data["storage_key"]),
            str(data.get("name") or ""),
            _int(data.get("size")),
            str(data.get("mime") or data.get("type") or ""),
        )
        for kind, data in candidates
        if isinstance(data, dict) and data.get("storage_key")
    )


def pages(
    api: WpsClient, chat_id: str, start_time: int | None = None
) -> Iterator[list[dict[str, Any]]]:
    token: str | None = None
    seen: set[str] = set()
    while True:
        response = api.get_messages(chat_id, 50, token, start_time)
        data = response.get("data", {})
        page = data.get("items", []) if isinstance(data, dict) else None
        if not isinstance(page, list):
            raise RuntimeError("WPS get_messages returned invalid data or items")
        yield [item for item in page if isinstance(item, dict)]
        next_token = str(data.get("next_page_token") or "")
        if not next_token:
            return
        if next_token == token or next_token in seen:
            raise RuntimeError("WPS get_messages returned a repeated page token")
        seen.add(next_token)
        token = next_token


def _sort_key(item: dict[str, Any]) -> tuple[float, int, str]:
    message = item.get("message") if isinstance(item.get("message"), dict) else {}
    return (
        _timestamp(item) or 0,
        _int(item.get("position") or message.get("position")),
        message_id(item),
    )


def latest(
    api: WpsClient,
    chat_id: str,
    limit: int,
    select: Callable[[dict[str, Any]], Any | None],
) -> tuple[list[Any], int, bool]:
    def scan(start_time: int | None) -> tuple[list[tuple[tuple[float, int, str], Any]], int, bool]:
        found: list[tuple[tuple[float, int, str], Any]] = []
        visible = 0
        for page in pages(api, chat_id, start_time):
            visible += len(page)
            found += [(_sort_key(item), value) for item in page if (value := select(item)) is not None]
            if start_time is None and len(found) >= limit:
                return found, visible, False
            if len(found) > limit * 2:
                found = sorted(found)[-limit:]
        return found, visible, True

    found, visible, exhausted = scan(None)
    if len(found) < limit and exhausted:
        found, visible, exhausted = scan(ALL_HISTORY_START)
    return [value for _, value in sorted(found)[-limit:]], visible, exhausted


def _format_time(stamp: float | None) -> str:
    if stamp is None:
        return ""
    try:
        value = datetime.fromtimestamp(stamp).astimezone()
    except (OSError, OverflowError, ValueError):
        value = datetime.fromtimestamp(stamp, timezone.utc)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def record(item: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    message = item.get("message") if isinstance(item.get("message"), dict) else item
    sender = item.get("sender") if isinstance(item.get("sender"), dict) else message.get("sender", {})
    sender = sender if isinstance(sender, dict) else {}
    sender_id = str(sender.get("id") or "?")
    names = context.get("sender_names") if isinstance(context.get("sender_names"), dict) else {}
    name = next(
        (
            str(value)
            for value in (
                sender.get("name"),
                sender.get("display_name"),
                sender.get("sender_name"),
                sender.get("app_name"),
                names.get(sender_id),
            )
            if value
        ),
        "Bot" if sender.get("type") in {"sp", "app"} else f"User({sender_id[:6]})",
    )
    return {
        "id": message_id(item),
        "sender": name,
        "time": _format_time(_timestamp(item)),
        "text": _extract_text(message_content(item)).strip(),
        "attachments": message_attachments(item),
    }


def history(
    api: WpsClient,
    context: dict[str, Any],
    limit: int = 30,
    participant: str = "",
    keyword: str = "",
    *,
    script: Path | None = None,
) -> str:
    limit = min(50, max(1, int(limit)))
    participant, keyword = participant.strip(), keyword.strip()
    current = str(context.get("current_event_id") or "")

    def select(item: dict[str, Any]) -> dict[str, Any] | None:
        row = record(item, context)
        if row["id"] == current:
            return None
        if participant and participant.casefold() not in row["sender"].casefold():
            return None
        if keyword and keyword.casefold() not in row["text"].casefold():
            return None
        return row

    rows, visible, exhausted = latest(api, str(context["chat_id"]), limit, select)
    source = script or Path("skills/wps-chat/scripts/wps_chat.py")
    lines = [
        "WPS chat history capability result",
        f"Source: {source} history",
        f"Fetched at: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"Scope: latest {limit} matching messages from all WPS-visible history, oldest to newest.",
        f"Coverage: scanned {visible} visible messages; "
        + ("accessible history exhausted." if exhausted else "latest window supplied the requested count."),
        f"Refresh: python \"{source}\" history --limit {limit}",
    ]
    if participant:
        lines.append(f"Participant filter: {participant}")
    if keyword:
        lines.append(f"Keyword filter: {keyword}")
    if not rows:
        note = (
            "History fetch succeeded, but no messages matched the requested filters."
            if visible
            else "History fetch succeeded, but WPS returned no accessible messages."
        )
        return "\n".join(lines + [note])
    lines.append(f"Returned range: {rows[0]['time'] or 'unknown'} to {rows[-1]['time'] or 'unknown'}.")
    for row in rows:
        lines.append(
            f"[{row['time'] or 'time unavailable'}] {row['sender']} "
            f"(message_id={row['id']}): {row['text'][:1500] or '[non-text message]'}"
        )
        lines += [
            f"  attachment {index}: {attachment.name or attachment.kind}"
            + (f", {attachment.size} bytes" if attachment.size else "")
            for index, attachment in enumerate(row["attachments"], 1)
        ]
    return "\n".join(lines)


def download(
    api: WpsClient, context: dict[str, Any], wanted: str = "", index: int = 1
) -> str:
    select = (
        (lambda item: item if message_id(item) == wanted else None)
        if wanted
        else (lambda item: item if message_attachments(item) else None)
    )
    values, _, _ = latest(api, str(context["chat_id"]), 1, select)
    if not values:
        raise RuntimeError("message or attachment not found in WPS-visible history")
    item, index = values[-1], max(1, int(index))
    found = message_attachments(item)
    if index > len(found):
        raise RuntimeError(f"message has {len(found)} attachment(s); index {index} is invalid")
    attachment, wanted = found[index - 1], message_id(item)
    target = attachment_target(
        Path(str(context.get("workspace") or Path.cwd())) / "downloads",
        wanted,
        index,
        attachment.name,
        attachment.kind,
    )
    path = api.download_attachment(
        chat_id=str(context["chat_id"]),
        message_id=wanted,
        attachment=attachment,
        target=target,
    )
    return (
        "WPS attachment capability result\n"
        f"Source message: {wanted}\nDownloaded to: {path}\n"
        "File surfaces: GA file_read/code_run."
    )
