from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from email.utils import formatdate
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import requests

from .protocol import Mention, WpsAttachment

logger = logging.getLogger(__name__)


def _split(text: str, limit: int = 4500) -> list[str]:
    blocks = [block.strip() for block in text.replace("\r\n", "\n").strip().split("\n\n") if block.strip()]
    if not blocks:
        return [""]
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if len(block) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(block[start : start + limit] for start in range(0, len(block), limit))
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


def _ok(result: dict[str, Any], operation: str) -> dict[str, Any]:
    success = result["ok"] if isinstance(result.get("ok"), bool) else any(
        result.get(key) in (0, "0") for key in ("code", "errcode")
    )
    if not success:
        code = result.get("code") or result.get("errcode") or "?"
        message = result.get("message") or result.get("msg") or "unknown error"
        raise RuntimeError(f"WPS {operation} failed: code={code} message={message}")
    return result


class WpsClient:
    def __init__(
        self, *, api_base: str, client_id: str, client_secret: str, timeout: int = 30
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.client_id, self.client_secret, self.timeout = client_id, client_secret, timeout
        self._http = requests.Session()
        self._token, self._token_expiry = "", 0.0
        self._mention_cache: dict[str, Mention | None] = {}
        self._company_id = ""

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
        data = self._json(
            self._send(
                "POST",
                f"{self.api_base}/oauth2/token",
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ).content
        )
        if not (token := data.get("access_token")):
            raise RuntimeError(f"WPS token response missing access_token: {data}")
        self._token = str(token)
        self._token_expiry = time.monotonic() + max(60, int(data.get("expires_in", 7200)) - 300)
        return self._token

    def _headers(self, method: str, uri: str, body: bytes) -> dict[str, str]:
        date = formatdate(timeval=time.time(), localtime=False, usegmt=True)
        digest = hashlib.sha256(body).hexdigest() if body else ""
        signing = f"KSO-1{method.upper()}{uri}application/json{date}{digest}"
        signature = hmac.new(
            self.client_secret.encode(), signing.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "Content-Type": "application/json",
            "X-Kso-Date": date,
            "X-Kso-Authorization": f"KSO-1 {self.client_id}:{signature}",
            "Authorization": f"Bearer {self._access_token()}",
        }

    @staticmethod
    def _json(raw: bytes) -> dict[str, Any]:
        value = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(value, dict):
            raise RuntimeError("WPS response is not a JSON object")
        return value

    def _send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        try:
            response = self._http.request(method, url, timeout=self.timeout, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            response = getattr(exc, "response", None)
            detail = (
                response.content.decode("utf-8", errors="replace")
                if response is not None
                else str(exc)
            )
            raise RuntimeError(f"WPS request failed: {detail}") from exc

    def _request(
        self, method: str, uri: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            if payload is not None
            else b""
        )
        response = self._send(
            method,
            f"{self.api_base}{uri}",
            data=body if payload is not None else None,
            headers=self._headers(method, uri, body),
        )
        return self._json(response.content)

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
        return self._request(
            "GET", f"/v7/chats/{quote(chat_id, safe='')}/messages?{urlencode(query)}"
        )

    def _data(self, uri: str, operation: str) -> dict[str, Any]:
        data = _ok(self._request("GET", uri), operation).get("data")
        if not isinstance(data, dict) or not data.get("id"):
            raise RuntimeError(f"WPS {operation} response missing data.id")
        return data

    def current_service_principal(self) -> dict[str, Any]:
        return self._data("/v7/service_principals/current", "get current service principal")

    def get_user(self, user_id: str) -> dict[str, Any]:
        return self._data(f"/v7/users/{quote(user_id, safe='')}", "get user")

    def send_markdown(
        self, chat_id: str, markdown: str, mentions: list[Mention] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "text",
            "receiver": {"receiver_id": chat_id, "type": "chat"},
            "content": {"text": {"content": markdown, "type": "markdown"}},
        }
        if mentions:
            payload["mentions"] = [mention.payload(i) for i, mention in enumerate(mentions, 1)]
        return _ok(self._request("POST", "/v7/messages/create", payload), "send message")

    def send_markdown_split(
        self, chat_id: str, markdown: str, *, mention: Mention | None = None, delay: float = 0.4
    ) -> None:
        parts = _split(markdown)
        for index, part in enumerate(parts):
            mentions = [mention] if mention and index == 0 else None
            if mention and index == 0:
                part = f"{mention.at_tag(1)}\n\n{part}"
            self.send_markdown(chat_id, part, mentions)
            if delay and index + 1 < len(parts):
                time.sleep(delay)

    def resolve_mention(self, user_id: str, display_name: str) -> Mention | None:
        if user_id in self._mention_cache:
            return self._mention_cache[user_id]
        company_id = self._company_id
        if not company_id:
            try:
                company_id = str(self.current_service_principal().get("company_id") or "")
            except Exception as exc:
                logger.warning("WPS service principal lookup failed: %s", exc)
            self._company_id = company_id
        if not company_id:
            self._mention_cache[user_id] = None
            return None
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
        result = _ok(
            self._request(
                "GET",
                f"/v7/chats/{quote(chat_id, safe='')}/messages/{quote(message_id, safe='')}"
                f"/resources/{quote(attachment.storage_key, safe='')}/download",
            ),
            "get attachment download URL",
        )
        if not (url := result.get("data", {}).get("url")):
            raise RuntimeError(f"WPS attachment response missing url: {result}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self._transfer("GET", str(url)))
        return target

    def upload_file(self, chat_id: str, path: Path) -> None:
        data = path.read_bytes()
        allocation = _ok(
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
        storage_key, entry = info.get("storage_key"), info.get("upload_entry", {})
        if not storage_key or not entry.get("url"):
            raise RuntimeError(f"WPS upload allocation failed: {allocation}")
        url = str(entry["url"])
        if params := entry.get("params"):
            url += ("&" if "?" in url else "?") + urlencode(params)
        self._transfer(
            str(entry.get("method", "PUT")),
            url,
            data=data,
            headers={str(key): str(value) for key, value in (entry.get("headers") or {}).items()},
        )
        _ok(
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

    def _transfer(
        self,
        method: str,
        url: str,
        *,
        data: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> bytes:
        return self._send(
            method,
            url,
            data=data if data and method.upper() != "GET" else None,
            headers=headers or {},
        ).content
