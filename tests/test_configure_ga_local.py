from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def load_script():
    path = Path(__file__).parents[1] / "scripts" / "configure_ga_local.py"
    spec = importlib.util.spec_from_file_location("configure_ga_local", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# TEST-CONTRACT: req=LOCAL-CONFIG-01 | rejects=configuration generator overwrites existing credentials by default | gap=no overwrite guard | revert=remove target.exists check | mock=none
def test_copy_config_refuses_existing_target_without_force(tmp_path: Path) -> None:
    module = load_script()
    source = tmp_path / "template.py"
    target = tmp_path / "mykey.py"
    source.write_text("new = True\n", encoding="utf-8")
    target.write_text("secret = 'keep'\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="refusing to overwrite"):
        module.copy_config(source, target, force=False)

    assert target.read_text(encoding="utf-8") == "secret = 'keep'\n"
    module.copy_config(source, target, force=True)
    assert target.read_text(encoding="utf-8") == "new = True\n"
