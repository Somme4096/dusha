from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import legacy
from ..providers import Provider, http_url, parse
from ..resources import loads_strict

logger = logging.getLogger("dusha")

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
SERVER_URL = "http://127.0.0.1:8765"
PROXY_PORT = 8766
TIMEOUT_SECONDS = 10.0
MANIFEST_CACHE = "cli-manifest.json"
CLIENT_KEYS = frozenset({"proxy", "providers"})


@dataclass(frozen=True, slots=True)
class Connection:
    url: str = SERVER_URL
    token: str = ""
    timeout_seconds: float = TIMEOUT_SECONDS

    def headers(self) -> dict[str, str]:
        return {"x-companion-token": self.token} if self.token else {}


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = PROXY_PORT
    server: Connection = Connection()
    providers: dict[str, Provider] = field(default_factory=dict)


Settings.upstreams = property(legacy.upstreams)


def __getattr__(name: str) -> Any:
    return legacy.alias(__name__, name)


def _object(value: Any, label: str, allowed: set[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown field(s) under {label}: {unknown}")
    return value


def _typed(raw: dict[str, Any], key: str, kind: type, label: str, default: Any) -> Any:
    value = raw.get(key, default)
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if not isinstance(value, kind) or isinstance(value, bool) is not (kind is bool):
        raise ValueError(f"{label} must be {kind.__name__}")
    return value


def _timeout(raw: dict[str, Any], label: str, default: float) -> float:
    value = _typed(raw, "timeout_seconds", float, f"{label}.timeout_seconds", default)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label}.timeout_seconds must be above 0")
    return value


def _port(raw: dict[str, Any], label: str, default: int) -> int:
    value = _typed(raw, "port", int, label, default)
    if not 0 <= value <= 65535:
        raise ValueError(f"{label} must be at least 0 and at most 65535")
    return value


def _read(path: Path, *, warn: bool = False) -> dict[str, Any]:
    raw, found = legacy.upgrade_config(loads_strict(path.read_text(encoding="utf-8"), source=str(path)))
    if found and warn:
        legacy.warn_config(logger, path, found)
    return raw


def _connection(raw: dict[str, Any]) -> Connection:
    proxy = _object(raw.get("proxy"), "proxy", {"host", "port", "server"})
    server = _object(proxy.get("server"), "proxy.server", {"url", "token", "timeout_seconds"})
    url = _typed(server, "url", str, "proxy.server.url", "")
    token = _typed(server, "token", str, "proxy.server.token", "")
    timeout = _timeout(server, "proxy.server", TIMEOUT_SECONDS)
    local = _local_url(raw)
    url = http_url(url, "proxy.server.url") if url else local
    if url == local and not token:
        token = _typed(raw, "api_token", str, "api_token", "")
    return Connection(url, token, timeout)


def _local_url(raw: dict[str, Any]) -> str:
    host = _typed(raw, "host", str, "host", "127.0.0.1")
    port = _port(raw, "port", 8765)
    host = "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host
    return f"http://{f'[{host}]' if ':' in host else host}:{port}"


def _data_dir(raw: dict[str, Any], home: Path) -> Path:
    value = Path(_typed(raw, "data_dir", str, "data_dir", "data")).expanduser()
    return value if value.is_absolute() else home / value


def connection(path: Path | None) -> tuple[Connection, Path | None]:
    if path is None:
        return Connection(), None
    raw = _read(path)
    return _connection(raw), _data_dir(raw, path.parent) / MANIFEST_CACHE


def load(path: Path, *, environ: bool = True) -> Settings:
    raw = _read(path, warn=environ)
    server = _connection(raw)
    proxy = _proxy_section(raw)
    host = _typed(proxy, "host", str, "proxy.host", "127.0.0.1")
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"proxy.host must be one of {sorted(LOOPBACK_HOSTS)}, got {host!r}")
    return Settings(
        data_dir=_data_dir(raw, path.parent),
        host=host,
        port=_port(proxy, "proxy.port", PROXY_PORT),
        server=server,
        providers=parse(raw.get("providers"), environ=environ),
    )


def create(path: Path, url: str, token: str) -> Path:
    raw = {
        "proxy": {
            "host": "127.0.0.1",
            "port": PROXY_PORT,
            "server": {"url": url, "token": token, "timeout_seconds": int(TIMEOUT_SECONDS)},
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as file:
        file.write(json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
    return path


def _write(path: Path, raw: dict[str, Any]) -> Path:
    temp = path.with_name(f".{path.name}.tmp")
    temp.unlink(missing_ok=True)
    with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as file:
        file.write(json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
    try:
        load(temp, environ=False)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path


def _proxy_section(raw: dict[str, Any]) -> dict[str, Any]:
    return dict(_object(raw.get("proxy"), "proxy", {"host", "port", "server"}))


def save_provider(path: Path, name: str, profile: dict[str, Any] | None) -> Path:
    raw = _read(path)
    providers = raw.get("providers") or {}
    if not isinstance(providers, dict):
        raise ValueError("providers must be an object")
    providers = dict(providers)
    if profile is None:
        providers.pop(name, None)
    else:
        providers[name] = profile
    raw["providers"] = providers
    return _write(path, raw)


def server_saved(path: Path) -> bool:
    proxy = _read(path).get("proxy")
    server = proxy.get("server") if isinstance(proxy, dict) else None
    return isinstance(server, dict) and bool(server.get("url"))


def local_server(path: Path) -> Connection | None:
    if not path.exists():
        return None
    raw = _read(path)
    if set(raw) <= CLIENT_KEYS:
        return None
    return _connection({key: value for key, value in raw.items() if key != "proxy"})


def save_server(path: Path, url: str, token: str) -> Path:
    if not path.exists():
        return create(path, url, token)
    raw = _read(path)
    proxy = _proxy_section(raw)
    server = dict(_object(proxy.get("server"), "proxy.server", {"url", "token", "timeout_seconds"}))
    server.update(url=url, token=token)
    raw["proxy"] = {**proxy, "server": server}
    return _write(path, raw)
