from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

URL = "https://models.dev/api.json"
CACHE_FILE = "models-dev.json"
MAX_AGE_SECONDS = 86400


class CatalogError(RuntimeError):
    pass


def cache_path() -> Path:
    base = os.getenv("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "dusha" / CACHE_FILE


def _cached(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _fetch(path: Path) -> dict[str, Any]:
    response = httpx.get(URL, timeout=30, follow_redirects=True)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("the catalog is not an object")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temp, path)
    return data


def load(*, refresh: bool = False) -> tuple[dict[str, Any], str | None]:
    path = cache_path()
    cached = _cached(path)
    if cached is not None and not refresh and time.time() - path.stat().st_mtime < MAX_AGE_SECONDS:
        return cached, None
    try:
        return _fetch(path), None
    except (httpx.HTTPError, ValueError, OSError) as error:
        reason = str(error) or type(error).__name__
        if cached is None:
            raise CatalogError(f"cannot load the models.dev catalog: {reason}") from error
        return cached, f"using the cached models.dev catalog, {reason}"
