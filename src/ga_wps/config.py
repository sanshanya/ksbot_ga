from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ga_core.config import CoreSettings, env_bool, env_int, load_dotenv


@dataclass(frozen=True)
class WpsSettings:
    core: CoreSettings
    callback_host: str
    callback_port: int
    callback_secret: str
    api_base: str
    client_id: str
    client_secret: str
    max_workers: int
    recent_history_messages: int
    seen_events_limit: int
    approval_timeout_seconds: int
    launch_bridge: bool
    bridge_node: str

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> "WpsSettings":
        root = (project_root or Path.cwd()).resolve()
        load_dotenv(root / ".env")
        core = CoreSettings.from_env(root).resolve_gate_config()
        return cls(
            core=core,
            callback_host=os.getenv("GA_WPS_CALLBACK_HOST", "127.0.0.1"),
            callback_port=env_int("GA_WPS_CALLBACK_PORT", 23883),
            callback_secret=os.getenv("GA_WPS_CALLBACK_SECRET", ""),
            api_base=os.getenv("WPS365_API_BASE", "https://openapi.wps.cn"),
            client_id=os.getenv("WPS365_CLIENT_ID", ""),
            client_secret=os.getenv("WPS365_CLIENT_SECRET", ""),
            max_workers=env_int("GA_WPS_MAX_WORKERS", 8),
            recent_history_messages=env_int("GA_WPS_RECENT_HISTORY_MESSAGES", 30),
            seen_events_limit=env_int("GA_WPS_SEEN_EVENTS_LIMIT", 2048),
            approval_timeout_seconds=env_int("GA_WPS_APPROVAL_TIMEOUT_SECONDS", 300),
            launch_bridge=env_bool("GA_WPS_LAUNCH_BRIDGE", True),
            bridge_node=os.getenv("GA_WPS_NODE", "node"),
        )

    def validate(self) -> None:
        self.core.validate()
        if not self.client_id or not self.client_secret:
            raise RuntimeError("WPS365_CLIENT_ID and WPS365_CLIENT_SECRET are required")
        if self.recent_history_messages > 50:
            raise RuntimeError("GA_WPS_RECENT_HISTORY_MESSAGES must be <= 50")
