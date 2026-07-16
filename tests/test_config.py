from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch


from ga_core.config import CoreSettings


def _make_mykey(ga_root: Path) -> None:
    ga_root.mkdir(parents=True, exist_ok=True)
    config = {
        "apikey": "sk-test-key",
        "apibase": "https://api.example.com/v1",
        "model": "test-model",
    }
    ga_root.joinpath("mykey.py").write_text(
        f"native_oai_config = {config!r}\n", encoding="utf-8"
    )


# TEST-CONTRACT: req=GATE-CONFIG-01 | rejects=gate cannot reuse mykey model config when env is empty | gap=no mykey-to-gate resolution path | revert=skip mykey read in resolve_gate_config | mock=none
def test_resolve_gate_config_reuses_mykey_when_env_empty(tmp_path: Path) -> None:
    ga_root = tmp_path / "GenericAgent"
    _make_mykey(ga_root)
    with patch.dict(os.environ, {
        "GA_ROOT": str(ga_root),
        "GA_GATE_BASE_URL": "",
        "GA_GATE_MODEL": "",
        "GA_GATE_API_KEY": "",
    }, clear=False):
        s = CoreSettings.from_env(tmp_path)
        s = s.resolve_gate_config()
        assert s.gate_base_url == "https://api.example.com/v1"
        assert s.gate_model == "test-model"
        assert s.gate_api_key == "sk-test-key"


# TEST-CONTRACT: req=GATE-CONFIG-02 | rejects=explicit env does not override mykey config | gap=no env-override priority | revert=swap env-or-mykey to mykey-or-env in resolve_gate_config | mock=none
def test_resolve_gate_config_explicit_env_overrides_mykey(tmp_path: Path) -> None:
    ga_root = tmp_path / "GenericAgent"
    _make_mykey(ga_root)
    with patch.dict(os.environ, {
        "GA_ROOT": str(ga_root),
        "GA_GATE_BASE_URL": "https://gate.override.com/v1",
        "GA_GATE_MODEL": "gate-model",
        "GA_GATE_API_KEY": "gate-key",
    }, clear=False):
        s = CoreSettings.from_env(tmp_path)
        s = s.resolve_gate_config()
        assert s.gate_base_url == "https://gate.override.com/v1"
        assert s.gate_model == "gate-model"
        assert s.gate_api_key == "gate-key"


# TEST-CONTRACT: req=GATE-CONFIG-03 | rejects=gate runs with no config (would call empty URL) | gap=no fail-closed case | revert=inject non-empty gate defaults when mykey and env both empty | mock=none
def test_resolve_gate_config_empty_when_no_mykey_and_no_env(tmp_path: Path) -> None:
    with patch.dict(os.environ, {
        "GA_ROOT": str(tmp_path / "GenericAgent"),
        "GA_GATE_BASE_URL": "",
        "GA_GATE_MODEL": "",
        "GA_GATE_API_KEY": "",
    }, clear=False):
        s = CoreSettings.from_env(tmp_path)
        s = s.resolve_gate_config()
        # Fail-closed: all gate fields empty.
        assert s.gate_base_url == ""
        assert s.gate_model == ""
        assert s.gate_api_key == ""


# TEST-CONTRACT: req=CONFIG-BOUNDARY-01 | rejects=core settings contains WPS transport credentials | gap=no package-boundary assertion | revert=move WPS fields back into CoreSettings | mock=none
def test_core_settings_excludes_wps_transport_fields(tmp_path: Path) -> None:
    with patch.dict(os.environ, {}, clear=True):
        settings = CoreSettings.from_env(tmp_path)
    assert not hasattr(settings, "client_id")
    assert not hasattr(settings, "callback_port")
