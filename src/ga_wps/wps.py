from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


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


class WpsClient:
    def __init__(
        self,
        *,
        api_base: str,
        client_id: str,
        client_secret: str,
        timeout: int = 30,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self._token = ""
        self._token_expiry = 0.0
        self._mention_cache: dict[str, Mention | None] = {}
        self._company_id: str = ""

    def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        body = urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        ).encode()
        request = Request(
            f"{self.api_base}/oauth2/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        data = self._open_json(request)
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"WPS token response missing access_token: {data}")
        self._token = str(token)
        self._token_expiry = time.monotonic() + max(60, int(data.get("expires_in", 7200)) - 300)
        return self._token

    def _headers(self, method: str, uri: str, body: bytes) -> dict[str, str]:
        content_type = "application/json"
        kso_date = formatdate(timeval=time.time(), localtime=False, usegmt=True)
        digest = hashlib.sha256(body).hexdigest() if body else ""
        signing = f"KSO-1{method.upper()}{uri}{content_type}{kso_date}{digest}"
        signature = hmac.new(
            self.client_secret.encode(), signing.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "Content-Type": content_type,
            "X-Kso-Date": kso_date,
            "X-Kso-Authorization": f"KSO-1 {self.client_id}:{signature}",
            "Authorization": f"Bearer {self._access_token()}",
        }

    def _request(
        self, method: str, uri: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            if payload is not None
            else b""
        )
        request = Request(
            f"{self.api_base}{uri}",
            data=body if payload is not None else None,
            headers=self._headers(method, uri, body),
            method=method.upper(),
        )
        return self._open_json(request)

    def _open(self, request: Request) -> bytes:
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"WPS HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"WPS request failed: {exc}") from exc

    def _open_json(self, request: Request) -> dict[str, Any]:
        raw = self._open(request)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def get_messages(
        self,
        chat_id: str,
        page_size: int = 30,
        page_token: str | None = None,
        start_time: int | None = None,
    ) -> dict[str, Any]:
        query: dict[str, str | int] = {"page_size": page_size}
        if page_token:
            query["page_token"] = page_token
        if start_time is not None:
            query["start_time"] = start_time
        uri = f"/v7/chats/{quote(chat_id, safe='')}/messages?{urlencode(query)}"
        return self._request("GET", uri)

    def current_service_principal(self) -> dict[str, Any]:
        result = _require_ok(
            self._request("GET", "/v7/service_principals/current"),
            "get current service principal",
        )
        data = result.get("data")
        if not isinstance(data, dict) or not data.get("id"):
            raise RuntimeError("WPS current service principal response missing data.id")
        return data

    def get_user(self, user_id: str) -> dict[str, Any]:
        result = _require_ok(
            self._request("GET", f"/v7/users/{quote(user_id, safe='')}"), "get user"
        )
        data = result.get("data")
        if not isinstance(data, dict) or not data.get("id"):
            raise RuntimeError("WPS get user response missing data.id")
        return data

    def send_markdown(
        self, chat_id: str, markdown: str, mentions: list[Mention] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "text",
            "receiver": {"receiver_id": chat_id, "type": "chat"},
            "content": {"text": {"content": markdown, "type": "markdown"}},
        }
        if mentions:
            payload["mentions"] = [m.payload(i) for i, m in enumerate(mentions, 1)]
        return _require_ok(
            self._request("POST", "/v7/messages/create", payload), "send message"
        )

    def send_markdown_split(
        self,
        chat_id: str,
        markdown: str,
        *,
        mention: Mention | None = None,
        delay: float = 0.4,
    ) -> None:
        parts = _split_markdown(markdown)
        for index, part in enumerate(parts):
            mentions = [mention] if mention and index == 0 else None
            if mentions:
                part = f"{mention.at_tag(1)}\n\n{part}"
            self.send_markdown(chat_id, part, mentions)
            if delay and index + 1 < len(parts):
                time.sleep(delay)

    def resolve_mention(self, chat_id: str, user_id: str, display_name: str) -> Mention | None:
        del chat_id  # mention identity is user-scoped; chat only selects the message receiver.
        if user_id in self._mention_cache:
            return self._mention_cache[user_id]
        # company_id comes from the service principal (the app's tenant), NOT from
        # /v7/users/{id} — that endpoint has no company_id field. The SP endpoint
        # returns data.company_id and requires no extra scope. Cached on the client
        # so we only hit it once per process.
        company_id = self._company_id
        if not company_id:
            try:
                sp = self.current_service_principal()
                company_id = str(sp.get("company_id") or "")
            except Exception as exc:
                logger.warning("WPS service principal lookup failed: %s", exc)
            if company_id:
                self._company_id = company_id
        if not company_id:
            # No company_id → messages/create would 400 (ParseInt "").
            # Safe fallback: send no mention at all (plain text reply).
            self._mention_cache[user_id] = None
            return None
        # display_name comes from /v7/users/{id}.user_name (the real WPS profile name).
        # Falls back to the caller-supplied display_name if contact lookup fails.
        name = display_name.strip()
        try:
            user = self.get_user(user_id)
            name = str(user.get("user_name") or user.get("name") or name).strip()
        except Exception as exc:
            logger.warning("WPS contact lookup failed user_id=%s: %s", user_id, exc)
        mention = Mention(user_id, company_id, name or f"User({user_id[:6]})")
        self._mention_cache[user_id] = mention
        return mention

    def download_attachment(
        self, *, chat_id: str, message_id: str, attachment: WpsAttachment, target: Path
    ) -> Path:
        result = _require_ok(
            self._request(
                "GET",
                f"/v7/chats/{quote(chat_id, safe='')}/messages/{quote(message_id, safe='')}"
                f"/resources/{quote(attachment.storage_key, safe='')}/download",
            ),
            "get attachment download URL",
        )
        url = result.get("data", {}).get("url")
        if not url:
            raise RuntimeError(f"WPS attachment response missing url: {result}")
        data = self._transfer_bytes("GET", str(url))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def upload_file(self, chat_id: str, path: Path) -> None:
        data = path.read_bytes()
        allocation = _require_ok(
            self._request(
                "POST",
                "/v7/chats/resources/upload",
                {
                    "file_name": path.name[:256],
                    "file_size": len(data),
                    "checksum": hashlib.sha256(data).hexdigest(),
                },
            ),
            "allocate upload",
        )
        info = allocation.get("data", {})
        storage_key = info.get("storage_key")
        entry = info.get("upload_entry", {})
        if not storage_key or not entry.get("url"):
            raise RuntimeError(f"WPS upload allocation failed: {allocation}")
        upload_url = str(entry["url"])
        params = entry.get("params") or {}
        if params:
            separator = "&" if "?" in upload_url else "?"
            upload_url += separator + urlencode(params)
        self._transfer_bytes(
            str(entry.get("method", "PUT")),
            upload_url,
            data=data,
            headers={str(k): str(v) for k, v in (entry.get("headers") or {}).items()},
        )
        _require_ok(
            self._request(
                "POST",
                "/v7/messages/create",
                {
                    "type": "file",
                "receiver": {"receiver_id": chat_id, "type": "chat"},
                "content": {
                    "file": {
                        "type": "local",
                        "local": {
                            "storage_key": str(storage_key),
                            "name": path.name,
                            "size": len(data),
                        },
                    }
                },
                },
            ),
            "send file",
        )

    def _transfer_bytes(
        self,
        method: str,
        url: str,
        *,
        data: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> bytes:
        return self._open(
            Request(
                url,
                data=data if data and method.upper() != "GET" else None,
                headers=headers or {},
                method=method.upper(),
            )
        )


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


class CallbackServer:
    def __init__(
        self,
        host: str,
        port: int,
        secret: str,
        on_message: Callable[[WpsMessage], None],
    ) -> None:
        self._host = host
        self._port = port
        self._secret = secret
        self._on_message = on_message
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        callback = self._on_message
        secret = self._secret

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/wps/callback":
                    self.send_error(404)
                    return
                supplied = self.headers.get("X-GA-WPS-SECRET", "")
                supplied = supplied or self.headers.get("X-KSBOT-WPS-SECRET", "")
                if secret and supplied != secret:
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    message = WpsMessage.from_payload(payload)
                    if not message.chat_id:
                        raise ValueError("chat_id is required")
                    callback(message)
                    body = b'{"ok":true}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as exc:
                    logger.exception("WPS callback failed")
                    body = json.dumps({"ok": False, "error": str(exc)}).encode()
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, fmt: str, *args: Any) -> None:
                logger.debug("callback: " + fmt, *args)

        self._server = ThreadingHTTPServer((self._host, self._port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=2)
