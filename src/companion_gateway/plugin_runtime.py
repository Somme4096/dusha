from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .config import default_config_dir

MODULE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REQUIRED_FILES = ("README.md", "main.py", "pyproject.toml", "uv.lock")
SYNC_TIMEOUT = 120.0
MODS_DIR_ENV = "COMPANION_GATEWAY_MODS_DIR"
BASE_ENV = frozenset({"PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT"})


def resolve_mods_dir(config: Any, section: str) -> Path:
    env = os.getenv(MODS_DIR_ENV, "")
    if env:
        return Path(env).expanduser()
    configured = str(getattr(getattr(config, section, None), "mods_dir", "") or "")
    return Path(configured).expanduser() if configured else default_config_dir() / "mods"


def plugin_environment(passthrough: Any = ()) -> dict[str, str]:
    allowed = BASE_ENV | {str(name) for name in passthrough or ()}
    result = {key: value for key, value in os.environ.items() if key in allowed}
    result["PYTHONNOUSERSITE"] = "1"
    return result


def inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
