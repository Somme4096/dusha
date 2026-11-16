from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import legacy
from ..jsonfile import write_json_atomic
from ..providers import Provider, http_url, parse
from ..resources import loads_strict, object_of, port_of, timeout_of

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


def _typed(raw: dict[str, Any], key: str, kind: type, label: str, default: Any) -> Any:
    value = raw.get(key, default)
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if not isinstance(value, kind) or isinstance(value, bool) is not (kind is bool):
        raise ValueError(f"{label} must be {kind.__name__}")
    return value


def _read(path: Path, *, warn: bool = False) -> dict[str, Any]:
    raw, found = legacy.upgrade_config(loads_strict(path.read_text(encoding="utf-8"), source=str(path)))
    if found and warn:
        legacy.warn_config(logger, path, found)
    return raw


def _connection(raw: dict[str, Any]) -> Connection:
    proxy = object_of(raw.get("proxy"), "proxy", {"host", "port", "server"})
    server = object_of(proxy.get("server"), "proxy.server", {"url", "token", "timeout_seconds"})
    url = _typed(server, "url", str, "proxy.server.url", "")
    token = _typed(server, "token", str, "proxy.server.token", "")
    timeout = timeout_of(server.get("timeout_seconds", TIMEOUT_SECONDS), "proxy.server.timeout_seconds")
    local = _local_url(raw)
    url = http_url(url, "proxy.server.url") if url else local
    if url == local and not token:
        token = _typed(raw, "api_token", str, "api_token", "")
    return Connection(url, token, timeout)


def _local_url(raw: dict[str, Any]) -> str:
    host = _typed(raw, "host", str, "host", "127.0.0.1")
    port = port_of(raw.get("port", 8765), "port")
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
    if not host.strip():
        raise ValueError("proxy.host must not be empty")
    if not server.token and host not in LOOPBACK_HOSTS:
        raise ValueError(f"proxy.host must be one of {sorted(LOOPBACK_HOSTS)} without a token, got {host!r}")
    return Settings(
        data_dir=_data_dir(raw, path.parent),
        host=host,
        port=port_of(proxy.get("port", PROXY_PORT), "proxy.port"),
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
    write_json_atomic(path, raw, exclusive=True)
    return path


def _write(path: Path, raw: dict[str, Any]) -> Path:
    write_json_atomic(path, raw, before_replace=lambda temp: load(temp, environ=False))
    return path


def _proxy_section(raw: dict[str, Any]) -> dict[str, Any]:
    return dict(object_of(raw.get("proxy"), "proxy", {"host", "port", "server"}))


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


def saved_server(path: Path) -> tuple[str, str] | None:
    if not path.exists():
        return None
    proxy = _read(path).get("proxy")
    server = proxy.get("server") if isinstance(proxy, dict) else None
    if not isinstance(server, dict) or not server.get("url"):
        return None
    token = server.get("token", "")
    return str(server["url"]), token if isinstance(token, str) else ""


def server_saved(path: Path) -> bool:
    return saved_server(path) is not None


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
    server = dict(object_of(proxy.get("server"), "proxy.server", {"url", "token", "timeout_seconds"}))
    server.update(url=url, token=token)
    raw["proxy"] = {**proxy, "server": server}
    return _write(path, raw)
