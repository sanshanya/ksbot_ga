from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ga_core.config import load_dotenv  # noqa: E402
from ga_wps.history import download, history, message_id, pages  # noqa: E402,F401
from ga_wps.client import WpsClient  # noqa: E402
from ga_wps.kdocs import KdocsCli  # noqa: E402

SCRIPT = Path(__file__).resolve()


def context() -> dict[str, Any]:
    path = Path.cwd() / ".wps_context.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not value.get("chat_id"):
        raise RuntimeError(f"invalid WPS runtime context: {path}")
    return value


def client() -> WpsClient:
    load_dotenv(ROOT / ".env")
    client_id = os.getenv("WPS365_CLIENT_ID", "")
    secret = os.getenv("WPS365_CLIENT_SECRET", "")
    if not client_id or not secret:
        raise RuntimeError("WPS365_CLIENT_ID and WPS365_CLIENT_SECRET are required")
    return WpsClient(
        api_base=os.getenv("WPS365_API_BASE", "https://openapi.wps.cn"),
        client_id=client_id,
        client_secret=secret,
    )


def kdocs() -> KdocsCli:
    cli = KdocsCli()
    if not cli.available:
        raise RuntimeError("kdocs-cli is not installed")
    if not cli.authenticated:
        raise RuntimeError(
            "kdocs-cli is not authenticated; authentication command: `kdocs-cli auth login`"
        )
    return cli


def document(*, url: str = "", file_id: str = "") -> str:
    cli = kdocs()
    result = cli.read_file(url=url, file_id=file_id)
    title = str(result.get("name") or "Untitled document")
    content = str(result.get("content") or "")
    source = url or f"file_id:{file_id}"
    return f"WPS document capability result\nSource: {source}\nTitle: {title}\n\n{content or '(empty document)'}"


def create_document(*, title: str, content: str, parent_id: str = "") -> str:
    cli = kdocs()
    result = cli.create_smart_doc(title=title, content=content, parent_id=parent_id)
    file_id = str(result.get("file_id") or result.get("id") or "")
    if not file_id:
        raise RuntimeError(
            "document creation returned no file_id; visibility status is unverified"
        )
    try:
        shared = cli.share_file(file_id=file_id, scope="anyone")
    except Exception as exc:
        raise RuntimeError(
            f"document was created (file_id={file_id}) but sharing failed: {exc}; "
            "recovery operation: document-share"
        ) from exc
    url = str(shared.get("url") or shared.get("link_url") or "")
    if not url:
        raise RuntimeError(
            f"document was created (file_id={file_id}) but share URL was not returned; "
            "link status is unavailable"
        )
    return (
        "WPS smart document created and shared\n"
        f"Title: {title.strip()}\n"
        f"File ID: {file_id}\n"
        f"URL: {url}\n"
        "Access: anyone with the link can view\n"
        "Delivery: one create operation; a second create operation may duplicate the document"
    )


def content_file(value: str, path_value: str) -> str:
    if path_value:
        path = Path(path_value).expanduser().resolve()
        if not path.is_relative_to(Path.cwd().resolve()):
            raise RuntimeError("content file is outside the current workspace")
        if not path.is_file():
            raise RuntimeError(f"content file not found: {path}")
        return path.read_text(encoding="utf-8")
    return value or ""


def append_document(*, file_id: str, content: str) -> str:
    kdocs().append_smart_doc(file_id=file_id, content=content)
    return f"WPS smart document updated\nFile ID: {file_id}\nOperation: append"


def share_document(*, file_id: str, scope: str = "anyone") -> str:
    result = kdocs().share_file(file_id=file_id, scope=scope)
    url = str(result.get("url") or result.get("link_url") or "")
    if not url:
        raise RuntimeError(f"document {file_id} was shared but no URL was returned")
    return f"WPS document shared\nFile ID: {file_id}\nScope: {scope}\nURL: {url}"


def search_documents(*, keyword: str, limit: int = 20, search_type: str = "content") -> str:
    result = kdocs().search_files(keyword=keyword, page_size=limit, search_type=search_type)
    files = result.get("files") or result.get("items") or []
    if not files:
        return f"WPS document search\nKeyword: {keyword}\nType: {search_type}\nNo documents found"
    lines = [f"WPS document search\nKeyword: {keyword}\nType: {search_type}\nResults: {len(files)}"]
    for item in files:
        if isinstance(item, dict):
            file = item.get("file") or item
            file_id = file.get("file_id") or file.get("id") or ""
            name = file.get("name") or "Untitled document"
            url = file.get("link_url") or file.get("url") or ""
            lines.append(f"- {name} (file_id: {file_id})" + (f" [open]({url})" if url else ""))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Use WPS chat capabilities bound to this workspace.")
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("history")
    read.add_argument("--limit", type=int, default=30)
    read.add_argument("--participant", default="")
    read.add_argument("--keyword", default="")
    latest = commands.add_parser("download-latest")
    latest.add_argument("--attachment", type=int, default=1)
    specific = commands.add_parser("download")
    specific.add_argument("--message-id", required=True)
    specific.add_argument("--attachment", type=int, default=1)
    document_command = commands.add_parser("document")
    source = document_command.add_mutually_exclusive_group(required=True)
    source.add_argument("--url")
    source.add_argument("--file-id")
    create_command = commands.add_parser("document-create")
    create_command.add_argument("--title", required=True)
    create_content = create_command.add_mutually_exclusive_group(required=True)
    create_content.add_argument("--content")
    create_content.add_argument("--content-file")
    create_command.add_argument("--parent-id", default="")
    append_command = commands.add_parser("document-append")
    append_command.add_argument("--file-id", required=True)
    append_content = append_command.add_mutually_exclusive_group(required=True)
    append_content.add_argument("--content")
    append_content.add_argument("--content-file")
    share_command = commands.add_parser("document-share")
    share_command.add_argument("--file-id", required=True)
    share_command.add_argument("--scope", choices=("anyone", "company"), default="anyone")
    search_command = commands.add_parser("document-search")
    search_command.add_argument("--keyword", required=True)
    search_command.add_argument("--limit", type=int, default=20)
    search_command.add_argument("--type", choices=("content", "file_name"), default="content")
    args = parser.parse_args(argv)
    try:
        if args.command == "document":
            result = document(url=args.url or "", file_id=args.file_id or "")
        elif args.command == "document-create":
            body = content_file(args.content or "", args.content_file or "")
            result = create_document(title=args.title, content=body, parent_id=args.parent_id)
        elif args.command == "document-append":
            body = content_file(args.content or "", args.content_file or "")
            result = append_document(file_id=args.file_id, content=body)
        elif args.command == "document-share":
            result = share_document(file_id=args.file_id, scope=args.scope)
        elif args.command == "document-search":
            result = search_documents(keyword=args.keyword, limit=args.limit, search_type=args.type)
        else:
            ctx, api = context(), client()
            if args.command == "history":
                result = history(api, ctx, args.limit, args.participant, args.keyword, script=SCRIPT)
            elif args.command == "download":
                result = download(api, ctx, args.message_id, args.attachment)
            else:
                result = download(api, ctx, "", args.attachment)
        print(result)
        return 0
    except Exception as exc:
        print(
            f"WPS capability exists, but this invocation failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
