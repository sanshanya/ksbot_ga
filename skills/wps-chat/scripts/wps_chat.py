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
    args = parser.parse_args(argv)
    try:
        ctx, api = context(), client()
        result = (
            history(api, ctx, args.limit, args.participant, args.keyword, script=SCRIPT)
            if args.command == "history"
            else download(api, ctx, getattr(args, "message_id", ""), args.attachment)
        )
        print(result)
        return 0
    except Exception as exc:
        print(
            f"WPS chat capability exists, but this invocation failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
