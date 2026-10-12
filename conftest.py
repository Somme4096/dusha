from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_config_root(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg")))
    for name in ("DUSHA_HOME", "DUSHA_COMPANION", "DUSHA_MODS_DIR"):
        monkeypatch.delenv(name, raising=False)
