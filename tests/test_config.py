from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from ga_core.config import CoreSettings


def make_mykey(root: Path) -> None:
    root.mkdir(parents=True)
    root.joinpath("mykey.py").write_text(
        "native_oai_config = "
        "{'apikey':'sk-test','apibase':'https://api.example/v1','model':'test-model'}\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "with_mykey,overrides,expected",
    [
        (True, {}, ("https://api.example/v1", "test-model", "sk-test")),
        (
            True,
            {
                "GA_GATE_BASE_URL": "https://override/v1",
                "GA_GATE_MODEL": "gate-model",
                "GA_GATE_API_KEY": "gate-key",
            },
            ("https://override/v1", "gate-model", "gate-key"),
        ),
        (False, {}, ("", "", "")),
    ],
)
def test_gate_config_uses_explicit_env_then_mykey(
    tmp_path: Path, with_mykey: bool, overrides: dict[str, str], expected: tuple[str, str, str]
) -> None:
    ga_root = tmp_path / "GenericAgent"
    if with_mykey:
        make_mykey(ga_root)
    env = {
        "GA_ROOT": str(ga_root),
        "GA_GATE_BASE_URL": "",
        "GA_GATE_MODEL": "",
        "GA_GATE_API_KEY": "",
        **overrides,
    }
    with patch.dict(os.environ, env, clear=False):
        settings = CoreSettings.from_env(tmp_path).resolve_gate_config()
    assert (settings.gate_base_url, settings.gate_model, settings.gate_api_key) == expected


def test_core_settings_excludes_wps_transport(tmp_path: Path) -> None:
    with patch.dict(os.environ, {}, clear=True):
        settings = CoreSettings.from_env(tmp_path)
    assert not hasattr(settings, "client_id") and not hasattr(settings, "callback_port")
