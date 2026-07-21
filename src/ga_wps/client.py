from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from email.utils import formatdate
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .protocol import Mention, WpsAttachment, _require_ok, _split_markdown

logger = logging.getLogger(__name__)

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
