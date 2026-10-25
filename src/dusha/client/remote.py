from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

import httpx

from .settings import Connection

MANIFEST = "/cli/v1/commands"
RUN = "/cli/v1/run/"
PLUGINS = "/state/v1/plugins"


class RemoteError(Exception):
    pass


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if detail is None:
        return response.text.strip() or f"http {response.status_code}"
    return detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)


def request(connection: Connection, method: str, path: str, body: Any = None, *, wait: bool = False) -> Any:
    timeout = httpx.Timeout(connection.timeout_seconds, read=None if wait else connection.timeout_seconds)
    try:
        response = httpx.request(
            method, connection.url + path, json=body, headers=connection.headers(), timeout=timeout
        )
    except httpx.RequestError as error:
        reason = str(error) or type(error).__name__
        raise RemoteError(f"cannot reach the server at {connection.url}: {reason}") from error
    if response.status_code >= 400:
        raise RemoteError(_detail(response))
    try:
        return response.json()
    except ValueError as error:
        raise RemoteError(f"the server at {connection.url} did not answer with JSON") from error


def _cached(cache: Path | None, url: str) -> dict[str, Any] | None:
    if cache is None:
        return None
    try:
        saved = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return saved.get("manifest") if isinstance(saved, dict) and saved.get("url") == url else None


def _store(cache: Path | None, url: str, manifest: dict[str, Any]) -> None:
    if cache is None:
        return
    with contextlib.suppress(OSError):
        cache.parent.mkdir(parents=True, exist_ok=True)
        partial = cache.with_suffix(".partial")
        saved = {"url": url, "manifest": manifest}
        partial.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        os.replace(partial, cache)


def manifest(connection: Connection, cache: Path | None) -> tuple[dict[str, Any], str]:
    try:
        fetched = request(connection, "GET", MANIFEST)
    except RemoteError as error:
        cached = _cached(cache, connection.url)
        if cached is None:
            raise
        return cached, f"using the cached command list, {error}"
    _store(cache, connection.url, fetched)
    return fetched, ""


def run(connection: Connection, path: list[str], values: dict[str, Any]) -> Any:
    return request(connection, "POST", RUN + "/".join(path), values, wait=True)


def plugins(connection: Connection) -> Any:
    return request(connection, "GET", PLUGINS)
