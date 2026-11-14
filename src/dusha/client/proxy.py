from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import metadata as _metadata
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from .. import legacy as _legacy
from ..homes import COMPANION_NAME
from ..providers import Provider
from . import protocols as _protocols
from .outbox import Outbox, retryable
from .settings import Connection, Settings

logger = logging.getLogger("dusha.proxy")

_ROUTING_HEADERS = frozenset({"x-conversation-id", "x-harness", "x-companion-route", "x-companion-plugins"})
_DROPPED_HEADERS = _ROUTING_HEADERS | {
    "host",
    "content-length",
    "accept-encoding",
    "connection",
    "keep-alive",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "x-companion-token",
}
_KEY_HEADERS = frozenset({"authorization", "x-api-key", "api-key"})
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
_PLUGIN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_KEPT = 256
_FLUSH_SECONDS = 2.0


@dataclass(slots=True, frozen=True)
class Turn:
    harness: str
    conversation_id: str
    route: str
    after: int | None
    plugins: tuple[str, ...] | None = None

    def transcript(self, messages: list[Any]) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "conversation_id": self.conversation_id,
            "route": self.route,
            "after": self.after,
            "messages": messages,
        }


def _credential(request: Request) -> str:
    token = request.headers.get("x-companion-token")
    if token:
        return token
    authorization = request.headers.get("authorization", "")
    if authorization:
        if authorization.lower().startswith("bearer "):
            return authorization[len("bearer ") :]
        return authorization
    return request.headers.get("x-api-key") or request.headers.get("api-key") or ""


def _credential_harness(raw: str, expected: str) -> str:
    if not expected:
        if ":" in raw:
            prefix, _ = raw.split(":", 1)
            return prefix if COMPANION_NAME.fullmatch(prefix) else ""
        return ""
    if raw and secrets.compare_digest(raw.encode("utf-8"), expected.encode("utf-8")):
        return ""
    if ":" in raw:
        prefix, rest = raw.split(":", 1)
        matching = COMPANION_NAME.fullmatch(prefix)
        if matching and secrets.compare_digest(rest.encode("utf-8"), expected.encode("utf-8")):
            return prefix
    raise HTTPException(status_code=401, detail="invalid companion token")


def _plugins_header(value: str | None) -> tuple[str, ...] | None:
    if value is None or not value.strip():
        return None
    items = [part.strip() for part in value.split(",")]
    if any(not item for item in items):
        raise HTTPException(
            status_code=422, detail="X-Companion-Plugins must be comma-separated plugin names or 'none'"
        )
    if [item.casefold() for item in items] == ["none"]:
        return ()
    for item in items:
        if item.casefold() == "none" or not _PLUGIN_NAME.fullmatch(item):
            detail = f"invalid plugin name in X-Companion-Plugins: {item!r}"
            raise HTTPException(status_code=422, detail=detail)
    return tuple(dict.fromkeys(items))


def _remember(cache: OrderedDict, key: Any, value: Any) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _KEPT:
        cache.popitem(last=False)


class Server:
    def __init__(self, connection: Connection, outbox: Outbox):
        options = {
            "base_url": connection.url,
            "timeout": connection.timeout_seconds,
            "headers": connection.headers(),
        }
        self.url = connection.url
        self.client = httpx.AsyncClient(**options)
        self.flusher = httpx.Client(**options)
        self.outbox = outbox
        self.turns: OrderedDict[Any, str] = OrderedDict()
        self.responses: OrderedDict[str, int] = OrderedDict()

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.post(path, json=body)
        response.raise_for_status()
        return response.json()

    async def status(self) -> str:
        try:
            response = await self.client.get("/state/v1/plugins")
        except httpx.RequestError:
            return "unreachable"
        if response.status_code == 401:
            return "unauthorized"
        return "ok" if response.status_code < 400 else f"http {response.status_code}"

    def queue(self, turn: Turn, messages: list[Any], error: Exception) -> None:
        if isinstance(error, httpx.HTTPError) and retryable(error):
            self.outbox.put("/state/v1/transcript", turn.transcript(messages))
            logger.warning("server %s unavailable, queued the transcript: %s", self.url, error)
        else:
            logger.warning("server %s failed: %s", self.url, error)

    async def flush(self) -> None:
        while True:
            await asyncio.sleep(_FLUSH_SECONDS)
            try:
                if self.outbox.size():
                    await asyncio.to_thread(self.outbox.flush, self.flusher)
            except Exception:
                logger.exception("outbox flush failed")

    async def context(self, turn: Turn, messages: list[Any]) -> tuple[str | None, str]:
        try:
            ingested = await self.post("/state/v1/transcript", turn.transcript(messages))
            user = ingested["message_id"]
            key = (user, turn.plugins)
            injection = self.turns.get(key) if user is not None else None
            if injection is None:
                body: dict[str, Any] = {
                    "query": ingested["query"],
                    "harness": turn.harness,
                    "conversation_id": ingested["conversation_id"],
                    "exclude_message_ids": [user] if user is not None else [],
                    "include_recent": False,
                }
                if turn.plugins is not None:
                    body["plugins"] = list(turn.plugins)
                context = await self.post("/state/v1/context", body)
                injection = str(context["injection"])
            if user is not None:
                _remember(self.turns, key, injection)
        except (httpx.HTTPError, ValueError, KeyError) as error:
            self.queue(turn, messages, error)
            return None, turn.conversation_id
        return injection, ingested["conversation_id"]

    async def archive(self, turn: Turn, messages: list[Any], reply: dict[str, Any], response_id: str) -> None:
        try:
            stored = await self.post("/state/v1/transcript", turn.transcript([*messages, reply]))
        except (httpx.HTTPError, ValueError) as error:
            self.queue(turn, [*messages, reply], error)
            return
        if response_id and stored.get("last_message_id") is not None:
            _remember(self.responses, response_id, stored["last_message_id"])


def _upstream_headers(request: Request, profile: Provider, conversation_id: str) -> httpx.Headers:
    dropped = _DROPPED_HEADERS | _KEY_HEADERS
    headers = httpx.Headers({name: value for name, value in request.headers.items() if name not in dropped})
    headers.update(profile.request_headers(conversation_id))
    return headers


def _upstream_url(request: Request, profile: Provider, path: str, kind: str | None) -> str:
    base = profile.url(kind)
    if base.endswith("/v1") and path.startswith("v1/"):
        path = path.removeprefix("v1/")
    url = f"{base}/{path}"
    return f"{url}?{request.url.query}" if request.url.query else url


def _unreachable(error: httpx.RequestError) -> HTTPException:
    reason = str(error) or type(error).__name__
    return HTTPException(status_code=502, detail=f"upstream request failed: {reason}")


def _media_type(response: httpx.Response) -> str | None:
    return response.headers.get("content-type")


def create_proxy_app(cfg: Settings) -> FastAPI:
    server = Server(cfg.server, Outbox(cfg.data_dir))
    upstreams = {
        name: httpx.AsyncClient(timeout=profile.timeout_seconds) for name, profile in cfg.providers.items()
    }

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        flushing = asyncio.create_task(server.flush(), name="outbox-flush")
        try:
            yield
        finally:
            flushing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await flushing
            server.flusher.close()
            await server.client.aclose()
            for client in upstreams.values():
                await client.aclose()

    app = FastAPI(
        title="Dusha proxy",
        version=_metadata.version("dusha"),
        description="https://github.com/Somme4096/dusha",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = cfg

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "server": {"url": server.url, "status": await server.status()},
            "providers": sorted(cfg.providers),
            **_legacy.proxy_health(cfg.providers),
        }

    async def send(
        request: Request, name: str, path: str, content: bytes, conversation_id: str, kind: str | None = None
    ):
        profile = cfg.providers[name]
        client = upstreams[name]
        upstream_request = client.build_request(
            request.method,
            _upstream_url(request, profile, path, kind),
            headers=_upstream_headers(request, profile, conversation_id),
            content=content,
        )
        try:
            return await client.send(upstream_request, stream=True)
        except httpx.RequestError as error:
            raise _unreachable(error) from error

    def streamed(response: httpx.Response, on_end: Any = None) -> StreamingResponse:
        async def chunks() -> AsyncIterator[bytes]:
            pending = ""
            lines: list[str] = []
            try:
                async for chunk in response.aiter_bytes():
                    if on_end is not None:
                        pending += chunk.decode("utf-8", errors="replace")
                        *complete, pending = pending.split("\n")
                        lines.extend(complete)
                    yield chunk
            finally:
                await response.aclose()
                if on_end is not None:
                    await on_end(lines)

        return StreamingResponse(chunks(), status_code=response.status_code, media_type=_media_type(response))

    async def model(
        request: Request,
        name: str,
        path: str,
        protocol: Any,
        credential_harness: str,
        plugins: tuple[str, ...] | None,
    ) -> Response:
        raw = await request.body()
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        messages = protocol.transcript(body) if isinstance(body, dict) else None
        if messages is None:
            named = request.headers.get("x-conversation-id", "")
            return streamed(await send(request, name, path, raw, named, protocol.kind))
        turn = Turn(
            harness=request.headers.get("x-harness") or credential_harness or name,
            conversation_id=request.headers.get("x-conversation-id", ""),
            route=request.headers.get("x-companion-route", ""),
            after=server.responses.get(protocol.previous(body)),
            plugins=plugins,
        )
        injection, conversation = await server.context(turn, messages)
        prepared = protocol.inject(body, injection) if injection else body
        if isinstance(protocol, _protocols.ChatCompletions):
            prepared = protocol.sanitize(prepared)
        forwarded = json.dumps(prepared).encode()
        response = await send(request, name, path, forwarded, conversation, protocol.kind)
        if response.status_code >= 400:
            return streamed(response)

        async def archive(reply: dict[str, Any] | None, response_id: str) -> None:
            if reply is not None:
                await server.archive(turn, messages, reply, response_id)

        if body.get("stream"):

            async def archive_stream(lines: list[str]) -> None:
                parsed = _protocols.events(lines)
                await archive(protocol.stream_reply(parsed), protocol.response_id(parsed))

            return streamed(response, archive_stream)
        content = await response.aread()
        await response.aclose()
        data = None
        with contextlib.suppress(ValueError):
            data = json.loads(content)
        return Response(
            content=content,
            status_code=response.status_code,
            media_type=_media_type(response),
            background=BackgroundTask(archive, protocol.reply(data), protocol.response_id(data)),
        )

    @app.api_route("/{name}/{path:path}", methods=_METHODS)
    async def forward(name: str, path: str, request: Request) -> Response:
        if name not in cfg.providers:
            raise HTTPException(status_code=404, detail=f"no provider named {name!r}")
        credential_harness = _credential_harness(_credential(request), cfg.server.token)
        plugins = _plugins_header(request.headers.get("x-companion-plugins"))
        protocol = _protocols.protocol_for(request.method, path)
        if protocol is not None:
            return await model(request, name, path, protocol, credential_harness, plugins)
        conversation_id = request.headers.get("x-conversation-id", "")
        return streamed(await send(request, name, path, await request.body(), conversation_id))

    return app
