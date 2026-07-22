"""Keychain-backed WPS smart-document reads and creation through kdocs-cli."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


class KdocsCliError(RuntimeError):
    def __init__(self, code: int | str, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        detail = f"code={code}: {message}"
        if isinstance(data, dict) and data.get("file_id"):
            detail += (
                f"; document may already exist (file_id={data['file_id']}); "
                "creation outcome is partial"
            )
        super().__init__(detail)


class KdocsCli:
    """Small subprocess seam; authentication stays in the system keychain."""

    def __init__(self, executable: str = "", timeout: float = 30) -> None:
        self._configured = executable or os.getenv("KDOCS_CLI_PATH", "")
        self._timeout = timeout
        self._executable: str | None = None

    def _find(self) -> str | None:
        if self._executable is not None:
            return self._executable
        candidates = [self._configured, shutil.which("kdocs-cli")]
        local_app = os.getenv("LOCALAPPDATA", "")
        if local_app:
            candidates.append(str(Path(local_app) / "kdocs-cli" / "kdocs-cli.exe"))
        candidates.append(str(Path.home() / ".local" / "bin" / "kdocs-cli.exe"))
        self._executable = next(
            (str(path) for path in candidates if path and Path(path).is_file()), None
        )
        return self._executable

    @property
    def available(self) -> bool:
        return self._find() is not None

    @property
    def authenticated(self) -> bool:
        exe = self._find()
        if not exe:
            return False
        try:
            result = subprocess.run(
                [exe, "auth", "status", "--compact"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=min(self._timeout, 10),
                check=False,
            )
            value = json.loads(result.stdout or "{}")
            if isinstance(value, list):
                value = value[0] if value else {}
            return (
                result.returncode == 0
                and isinstance(value, dict)
                and bool(value.get("authenticated"))
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    def create_file_with_content(
        self,
        *,
        name: str,
        content: str = "",
        drive_id: str = "",
        parent_id: str = "",
    ) -> dict[str, Any]:
        """Create one WPS file; the filename suffix selects the document type."""
        if not (name := name.strip()):
            raise ValueError("document name is required")
        params: dict[str, Any] = {"name": name, "content": content}
        if drive_id:
            params["drive_id"] = drive_id
        if parent_id:
            params["parent_id"] = parent_id
        data = self._call("drive", "create-file-with-content", params)
        if not isinstance(data, dict):
            raise KdocsCliError("invalid", "create-file-with-content returned no file data")
        return data

    def create_smart_doc(
        self, *, title: str, content: str, parent_id: str = ""
    ) -> dict[str, Any]:
        """Create one Markdown-backed WPS smart document in the keychain drive."""
        title = title.strip()
        if not title:
            raise ValueError("document title is required")
        if any(char in title for char in "/\\\x00\r\n"):
            raise ValueError("document title must be a file name")
        if not content.strip():
            raise ValueError("document content is required")
        name = title if title.lower().endswith(".otl") else f"{title}.otl"
        return self.create_file_with_content(name=name, content=content, parent_id=parent_id)

    def append_smart_doc(self, *, file_id: str, content: str) -> dict[str, Any]:
        if not file_id.strip():
            raise ValueError("document file_id is required")
        if not content.strip():
            raise ValueError("document content is required")
        return self._call(
            "otl",
            "insert-content",
            {"file_id": file_id, "content": content, "format": "markdown", "mode": "append"},
        )

    def search_files(
        self, *, keyword: str, page_size: int = 20, search_type: str = "content"
    ) -> dict[str, Any]:
        if not keyword.strip():
            raise ValueError("search keyword is required")
        return self._call(
            "drive",
            "search-files",
            {"keyword": keyword, "type": search_type, "page_size": page_size},
        )

    def share_file(self, *, file_id: str, scope: str = "anyone") -> dict[str, Any]:
        if not file_id.strip():
            raise ValueError("document file_id is required")
        if scope not in {"anyone", "company"}:
            raise ValueError("share scope must be anyone or company")
        return self._call("drive", "share-file", {"file_id": file_id, "scope": scope})

    def read_file(self, *, url: str = "", file_id: str = "") -> dict[str, Any]:
        if not (url := url.strip()) and not (file_id := file_id.strip()):
            raise ValueError("document url or file_id is required")
        params = {"format": "markdown", **({"url": url} if url else {"file_id": file_id})}
        data = self._call("drive", "read-file", params)
        if not isinstance(data, dict):
            raise KdocsCliError("invalid", "read-file returned no document data")
        return data

    def read_file_by_url(self, url: str) -> dict[str, Any]:
        return self.read_file(url=url)

    def _call(self, service: str, action: str, params: dict[str, Any]) -> Any:
        exe = self._find()
        if not exe:
            raise KdocsCliError("missing", "kdocs-cli binary not found")
        try:
            result = subprocess.run(
                [exe, service, action, "--compact", json.dumps(params, ensure_ascii=False)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError:
            raise KdocsCliError("missing", "kdocs-cli binary not found") from None
        except subprocess.TimeoutExpired:
            raise KdocsCliError("timeout", f"kdocs-cli exceeded {self._timeout:g}s") from None
        if not result.stdout.strip():
            raise KdocsCliError(
                result.returncode or "empty",
                result.stderr.strip() or "kdocs-cli returned no output",
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise KdocsCliError(
                result.returncode or "invalid", f"invalid kdocs-cli JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise KdocsCliError("invalid", "kdocs-cli returned a non-object response")
        code = payload.get("code", 0)
        if code not in (0, "0"):
            raise KdocsCliError(
                code,
                str(payload.get("message") or payload.get("msg") or "unknown error"),
                payload.get("data"),
            )
        data = payload.get("data")
        if isinstance(data, dict) and "code" in data and "data" in data:
            if data.get("code") not in (0, "0"):
                raise KdocsCliError(
                    data.get("code", "invalid"),
                    str(data.get("msg") or data.get("message") or "unknown error"),
                    data.get("data"),
                )
            data = data["data"]
        return data
