from __future__ import annotations

import contextlib
import json
import logging
import os
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

from . import protocols as _protocols
from .config import PLACEHOLDER, AppConfig, ProxyConfig, UpstreamProfile

logger = logging.getLogger("dusha.proxy")

_ROUTING_HEADERS = frozenset({"x-conversation-id", "x-harness", "x-companion-route"})
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
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
_KEPT = 256


@dataclass(slots=True, frozen=True)
class Turn:
    harness: str
    conversation_id: str
    route: str
    after: int | None

    def transcript(self, messages: list[Any]) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "conversation_id": self.conversation_id,
            "route": self.route,
            "after": self.after,
            "messages": messages,
        }


def _remember(cache: OrderedDict, key: Any, value: Any) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _KEPT:
        cache.popitem(last=False)


class Server:
    def __init__(self, cfg: ProxyConfig):
        token = cfg.server.token
        self.url = cfg.server.url
        self.client = httpx.AsyncClient(
            base_url=cfg.server.url,
            timeout=cfg.server.timeout_seconds,
            headers={"x-companion-token": token} if token else {},
        )
        self.turns: OrderedDict[int, str] = OrderedDict()
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

    async def context(self, turn: Turn, messages: list[Any]) -> tuple[str | None, str]:
        try:
            ingested = await self.post("/state/v1/transcript", turn.transcript(messages))
            user = ingested["message_id"]
            injection = self.turns.get(user) if user is not None else None
            if injection is None:
                context = await self.post(
                    "/state/v1/context",
                    {
                        "query": ingested["query"],
                        "harness": turn.harness,
                        "conversation_id": ingested["conversation_id"],
                        "exclude_message_ids": [user] if user is not None else [],
                        "include_recent": False,
                    },
                )
                injection = str(context["injection"])
            if user is not None:
                _remember(self.turns, user, injection)
        except (httpx.HTTPError, ValueError, KeyError) as error:
            logger.warning("server %s failed, forwarding without context: %s", self.url, error)
            return None, turn.conversation_id
        return injection, ingested["conversation_id"]

    async def archive(self, turn: Turn, messages: list[Any], reply: dict[str, Any], response_id: str) -> None:
        try:
            stored = await self.post("/state/v1/transcript", turn.transcript([*messages, reply]))
        except (httpx.HTTPError, ValueError) as error:
            logger.warning("server %s failed, reply not archived: %s", self.url, error)
            return
        if response_id and stored.get("last_message_id") is not None:
            _remember(self.responses, response_id, stored["last_message_id"])


def _render(value: str, conversation_id: str) -> str:
    def fill(match: Any) -> str:
        token = match[1]
        return conversation_id if token == "conversation_id" else os.environ[token.removeprefix("env:")]

    return PLACEHOLDER.sub(fill, value)


def _upstream_headers(request: Request, profile: UpstreamProfile, conversation_id: str) -> httpx.Headers:
    headers = httpx.Headers(
        {name: value for name, value in request.headers.items() if name not in _DROPPED_HEADERS}
    )
    for name, value in profile.headers.items():
        headers[name] = _render(value, conversation_id)
    return headers


def _upstream_url(request: Request, profile: UpstreamProfile, path: str) -> str:
    url = f"{profile.base_url}/{path}"
    return f"{url}?{request.url.query}" if request.url.query else url


def _unreachable(error: httpx.RequestError) -> HTTPException:
    reason = str(error) or type(error).__name__
    return HTTPException(status_code=502, detail=f"upstream request failed: {reason}")


def _media_type(response: httpx.Response) -> str | None:
    return response.headers.get("content-type")


def create_proxy_app(config: AppConfig) -> FastAPI:
    cfg = config.proxy
    server = Server(cfg)
    upstreams = {
        name: httpx.AsyncClient(timeout=profile.timeout_seconds) for name, profile in cfg.upstreams.items()
    }

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
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
            "upstreams": sorted(cfg.upstreams),
        }

    async def send(request: Request, name: str, path: str, content: bytes, conversation_id: str):
        profile = cfg.upstreams[name]
        client = upstreams[name]
        upstream_request = client.build_request(
            request.method,
            _upstream_url(request, profile, path),
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

    async def model(request: Request, name: str, path: str, protocol: Any) -> Response:
        raw = await request.body()
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        messages = protocol.transcript(body) if isinstance(body, dict) else None
        if messages is None:
            named = request.headers.get("x-conversation-id", "")
            return streamed(await send(request, name, path, raw, named))
        turn = Turn(
            harness=request.headers.get("x-harness") or name,
            conversation_id=request.headers.get("x-conversation-id", ""),
            route=request.headers.get("x-companion-route", ""),
            after=server.responses.get(protocol.previous(body)),
        )
        injection, conversation = await server.context(turn, messages)
        forwarded = protocol.inject(body, injection) if injection else body
        response = await send(request, name, path, json.dumps(forwarded).encode(), conversation)
        if response.status_code >= 400 or injection is None:
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
        if name not in cfg.upstreams:
            raise HTTPException(status_code=404, detail=f"no upstream named {name!r}")
        protocol = _protocols.protocol_for(request.method, path)
        if protocol is not None:
            return await model(request, name, path, protocol)
        conversation_id = request.headers.get("x-conversation-id", "")
        return streamed(await send(request, name, path, await request.body(), conversation_id))

    return app
