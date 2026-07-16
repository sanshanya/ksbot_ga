from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from ga_wps.config import WpsSettings


# TEST-CONTRACT: req=CONFIG-BOUNDARY-02 | rejects=WPS transport config remains in ga_core | gap=no WPS-owned settings test | revert=remove WpsSettings and read transport env in CoreSettings | mock=none
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
