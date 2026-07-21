from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ga_wps.config import WpsSettings


def test_wps_settings_owns_transport_configuration(tmp_path: Path) -> None:
    with patch.dict(
        os.environ,
        {
            "WPS365_CLIENT_ID": "client",
            "WPS365_CLIENT_SECRET": "secret",
            "GA_WPS_CALLBACK_PORT": "24000",
        },
        clear=True,
    ):
        settings = WpsSettings.from_env(tmp_path)
    assert settings.client_id == "client"
    assert settings.client_secret == "secret"
    assert settings.callback_port == 24000
    assert settings.core.project_root == tmp_path.resolve()


def _settings_for_callback(host: str, secret: str) -> WpsSettings:
    return WpsSettings(
        core=SimpleNamespace(validate=lambda: None),
        callback_host=host,
        callback_port=23883,
        callback_secret=secret,
        api_base="https://wps.example",
        client_id="client",
        client_secret="secret",
        max_workers=1,
        recent_history_messages=30,
        seen_events_limit=10,
        approval_timeout_seconds=300,
        launch_bridge=False,
        bridge_node="node",
    )


@pytest.mark.parametrize("secret", ["", "change-me"])
def test_non_loopback_callback_requires_real_secret(secret: str) -> None:
    with pytest.raises(RuntimeError, match="callback secret"):
        _settings_for_callback("0.0.0.0", secret).validate()


def test_loopback_callback_may_use_empty_secret() -> None:
    _settings_for_callback("127.0.0.1", "").validate()
