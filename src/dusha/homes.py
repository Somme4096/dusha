from __future__ import annotations

import os
import re
from pathlib import Path

DOCS = "https://github.com/Somme4096/dusha"
CONFIG_FILE = "config.json"
HOME_ENV = "DUSHA_HOME"
COMPANION_ENV = "DUSHA_COMPANION"
COMPANION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def config_root() -> Path:
    base = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "dusha"


def companion_home(name: str) -> Path:
    if not COMPANION_NAME.fullmatch(name):
        raise ValueError(f"companion name must be letters, digits, dot, dash, or underscore, got {name!r}")
    return config_root() / name


def list_companions() -> list[str]:
    root = config_root()
    if not root.is_dir():
        return []
    return sorted(item.name for item in root.iterdir() if (item / CONFIG_FILE).is_file())


def _expand(value: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


def config_file(explicit: str | Path) -> Path:
    path = _expand(explicit)
    if path.suffix.lower() != ".json":
        raise ValueError(f"unsupported configuration format: {path}. Use a JSON file")
    if not path.exists():
        raise FileNotFoundError(f"configuration file not found: {path}")
    return path


def resolve_home(home: str | Path | None = None, companion: str | None = None) -> Path | None:
    if home:
        return _expand(home)
    if companion:
        return companion_home(companion)
    if os.getenv(HOME_ENV):
        return _expand(os.environ[HOME_ENV])
    if os.getenv(COMPANION_ENV):
        return companion_home(os.environ[COMPANION_ENV])
    names = list_companions()
    if len(names) > 1:
        raise ValueError(f"several companions exist: {names}. Name one")
    return companion_home(names[0]) if names else None


def named_home(home: str | Path | None, companion: str | None) -> Path | None:
    if home:
        return _expand(home)
    return companion_home(companion) if companion else None


def locate(
    path: str | Path | None = None, *, home: str | Path | None = None, companion: str | None = None
) -> Path | None:
    if path is not None:
        return config_file(path)
    selected = resolve_home(home, companion)
    return None if selected is None else config_file(selected / CONFIG_FILE)
