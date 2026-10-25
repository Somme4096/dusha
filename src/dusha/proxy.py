from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import metadata as _metadata
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

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


@dataclass(slots=True, frozen=True)
class Turn:
    harness: str
    conversation_id: str
    route: str

    def transcript(self, messages: list[Any]) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "conversation_id": self.conversation_id,
            "route": self.route,
            "messages": messages,
        }


class Server:
    def __init__(self, cfg: ProxyConfig):
        token = cfg.server.token
        self.url = cfg.server.url
        self.client = httpx.AsyncClient(
            base_url=cfg.server.url,
            timeout=cfg.server.timeout_seconds,
            headers={"x-companion-token": token} if token else {},
        )

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

    async def inject(self, turn: Turn, messages: list[Any]) -> list[Any] | None:
        try:
            ingested = await self.post("/state/v1/transcript", turn.transcript(messages))
            context = await self.post(
                "/state/v1/context",
                {
                    "query": ingested["query"],
                    "harness": turn.harness,
                    "conversation_id": turn.conversation_id,
                    "exclude_message_ids": [ingested["message_id"]] if ingested["message_id"] else [],
                    "include_recent": False,
                },
            )
        except (httpx.HTTPError, ValueError, KeyError) as error:
            logger.warning("server %s failed, forwarding without context: %s", self.url, error)
            return None
        return _inject_context(messages, context["injection"])

    async def archive(self, turn: Turn, messages: list[Any], reply: dict[str, Any]) -> None:
        try:
            await self.post("/state/v1/transcript", turn.transcript([*messages, reply]))
        except (httpx.HTTPError, ValueError) as error:
            logger.warning("server %s failed, reply not archived: %s", self.url, error)


def _inject_context(messages: list[Any], injection: str) -> list[Any]:
    output = list(messages)
    position = next(
        (
            index
            for index, message in enumerate(output)
            if not isinstance(message, dict) or message.get("role") != "system"
        ),
        len(output),
    )
    output.insert(position, {"role": "system", "content": injection})
    return output


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


def _reply_from_stream(lines: list[str]) -> dict[str, Any] | None:
    parts: list[str] = []
    for line in lines:
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        with contextlib.suppress(Exception):
            value = json.loads(line[6:])["choices"][0]["delta"].get("content")
            if isinstance(value, str):
                parts.append(value)
    text = "".join(parts)
    return {"role": "assistant", "content": text} if text else None


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

    async def chat(request: Request, name: str, path: str) -> Response:
        try:
            body = json.loads(await request.body())
        except ValueError as error:
            raise HTTPException(status_code=422, detail="request body must be JSON") from error
        messages = body.get("messages") if isinstance(body, dict) else None
        if not isinstance(messages, list):
            raise HTTPException(status_code=422, detail="messages must be a list")
        turn = Turn(
            harness=request.headers.get("x-harness") or name,
            conversation_id=request.headers.get("x-conversation-id") or "default",
            route=request.headers.get("x-companion-route", ""),
        )
        injected = await server.inject(turn, messages)
        forwarded = {**body, "messages": injected} if injected is not None else body
        response = await send(request, name, path, json.dumps(forwarded).encode(), turn.conversation_id)
        if response.status_code >= 400 or injected is None:
            return streamed(response)
        if body.get("stream"):

            async def archive_stream(lines: list[str]) -> None:
                reply = _reply_from_stream(lines)
                if reply is not None:
                    await server.archive(turn, messages, reply)

            return streamed(response, archive_stream)
        content = await response.aread()
        await response.aclose()
        reply = None
        with contextlib.suppress(Exception):
            reply = json.loads(content)["choices"][0]["message"]
        return Response(
            content=content,
            status_code=response.status_code,
            media_type=_media_type(response),
            background=BackgroundTask(server.archive, turn, messages, reply) if reply else None,
        )

    @app.api_route("/{name}/{path:path}", methods=_METHODS)
    async def forward(name: str, path: str, request: Request) -> Response:
        if name not in cfg.upstreams:
            raise HTTPException(status_code=404, detail=f"no upstream named {name!r}")
        if request.method == "POST" and path.rstrip("/").endswith("chat/completions"):
            return await chat(request, name, path)
        conversation_id = request.headers.get("x-conversation-id") or "default"
        return streamed(await send(request, name, path, await request.body(), conversation_id))

    return app
