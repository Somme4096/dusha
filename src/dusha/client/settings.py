from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..homes import COMPANION_NAME
from ..resources import loads_strict

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
PROXY_ROUTES = frozenset({"health"})
PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
SERVER_URL = "http://127.0.0.1:8765"
PROXY_PORT = 8766
TIMEOUT_SECONDS = 10.0
MANIFEST_CACHE = "cli-manifest.json"
PROTOCOLS = frozenset({"chat", "messages", "responses"})
AUTH_STYLES = frozenset({"bearer", "x-api-key"})


@dataclass(frozen=True, slots=True)
class Connection:
    url: str = SERVER_URL
    token: str = ""
    timeout_seconds: float = TIMEOUT_SECONDS

    def headers(self) -> dict[str, str]:
        return {"x-companion-token": self.token} if self.token else {}


@dataclass(frozen=True, slots=True)
class Upstream:
    base_url: str
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 120.0
    endpoints: dict[str, str] = field(default_factory=dict)
    key: str = ""
    auth: str = "bearer"


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = PROXY_PORT
    server: Connection = Connection()
    upstreams: dict[str, Upstream] = field(default_factory=dict)


def http_url(value: Any, label: str) -> str:
    parts = urlsplit(value) if isinstance(value, str) else None
    plain = parts and parts.scheme in {"http", "https"} and parts.hostname
    if not plain or parts.query or parts.fragment:
        raise ValueError(f"{label} must be an http or https URL without a query, got {value!r}")
    if "@" in parts.netloc:
        raise ValueError(f"{label} must not carry credentials")
    return value.rstrip("/")


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


def _read(path: Path) -> dict[str, Any]:
    return loads_strict(path.read_text(encoding="utf-8"), source=str(path))


def _connection(raw: dict[str, Any]) -> Connection:
    proxy = _object(raw.get("proxy"), "proxy", {"host", "port", "server", "upstreams"})
    server = _object(proxy.get("server"), "proxy.server", {"url", "token", "timeout_seconds"})
    url = _typed(server, "url", str, "proxy.server.url", "")
    token = _typed(server, "token", str, "proxy.server.token", "")
    timeout = _timeout(server, "proxy.server", TIMEOUT_SECONDS)
    if url:
        return Connection(http_url(url, "proxy.server.url"), token, timeout)
    host = _typed(raw, "host", str, "host", "127.0.0.1")
    port = _port(raw, "port", 8765)
    host = "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host
    local = f"http://{f'[{host}]' if ':' in host else host}:{port}"
    return Connection(local, token or _typed(raw, "api_token", str, "api_token", ""), timeout)


def _data_dir(raw: dict[str, Any], home: Path) -> Path:
    value = Path(_typed(raw, "data_dir", str, "data_dir", "data")).expanduser()
    return value if value.is_absolute() else home / value


def connection(path: Path | None) -> tuple[Connection, Path | None]:
    if path is None:
        return Connection(), None
    raw = _read(path)
    return _connection(raw), _data_dir(raw, path.parent) / MANIFEST_CACHE


def _placeholders(value: str, label: str, *, conversation: bool, environ: bool) -> None:
    for token in PLACEHOLDER.findall(value):
        variable = token.removeprefix("env:")
        if conversation and token == "conversation_id":
            continue
        if token == variable or not variable:
            raise ValueError(f"{label} has an unknown placeholder {{{token}}}")
        if environ and variable not in os.environ:
            raise ValueError(f"{label} needs the environment variable {variable}")


def _upstream(name: str, raw: Any, environ: bool = True) -> Upstream:
    label = f"proxy.upstreams.{name}"
    if not COMPANION_NAME.fullmatch(name) or name in PROXY_ROUTES:
        raise ValueError(
            f"upstream name must be letters, digits, dot, dash, or underscore and not one of "
            f"{sorted(PROXY_ROUTES)}, got {name!r}"
        )
    allowed = {"base_url", "headers", "timeout_seconds", "endpoints", "key", "auth"}
    profile = _object(raw, label, allowed)
    headers = profile.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValueError(f"{label}.headers must be an object")
    for header, value in headers.items():
        if not isinstance(value, str):
            raise ValueError(f"{label}.headers.{header} must be str")
        _placeholders(value, f"{label}.headers.{header}", conversation=True, environ=environ)
    endpoints = _object(profile.get("endpoints"), f"{label}.endpoints", set(PROTOCOLS))
    key = _typed(profile, "key", str, f"{label}.key", "")
    _placeholders(key, f"{label}.key", conversation=False, environ=environ)
    auth = _typed(profile, "auth", str, f"{label}.auth", "bearer")
    if auth not in AUTH_STYLES:
        raise ValueError(f"{label}.auth must be one of {sorted(AUTH_STYLES)}, got {auth!r}")
    return Upstream(
        base_url=http_url(profile.get("base_url", ""), f"{label}.base_url"),
        headers=dict(headers),
        timeout_seconds=_timeout(profile, label, 120.0),
        endpoints={kind: http_url(url, f"{label}.endpoints.{kind}") for kind, url in endpoints.items()},
        key=key,
        auth=auth,
    )


def load(path: Path, *, environ: bool = True) -> Settings:
    raw = _read(path)
    server = _connection(raw)
    proxy = _object(raw.get("proxy"), "proxy", {"host", "port", "server", "upstreams"})
    host = _typed(proxy, "host", str, "proxy.host", "127.0.0.1")
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"proxy.host must be one of {sorted(LOOPBACK_HOSTS)}, got {host!r}")
    upstreams = proxy.get("upstreams") or {}
    if not isinstance(upstreams, dict):
        raise ValueError("proxy.upstreams must be an object")
    return Settings(
        data_dir=_data_dir(raw, path.parent),
        host=host,
        port=_port(proxy, "proxy.port", PROXY_PORT),
        server=server,
        upstreams={str(name): _upstream(str(name), value, environ) for name, value in upstreams.items()},
    )


def create(path: Path, url: str, token: str) -> Path:
    raw = {
        "proxy": {
            "host": "127.0.0.1",
            "port": PROXY_PORT,
            "server": {"url": url, "token": token, "timeout_seconds": int(TIMEOUT_SECONDS)},
            "upstreams": {},
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as file:
        file.write(json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
    return path


def save_upstream(path: Path, name: str, profile: dict[str, Any] | None) -> Path:
    raw = _read(path)
    proxy = dict(_object(raw.get("proxy"), "proxy", {"host", "port", "server", "upstreams"}))
    upstreams = proxy.get("upstreams") or {}
    if not isinstance(upstreams, dict):
        raise ValueError("proxy.upstreams must be an object")
    upstreams = dict(upstreams)
    if profile is None:
        upstreams.pop(name, None)
    else:
        upstreams[name] = profile
    raw["proxy"] = {**proxy, "upstreams": upstreams}
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
