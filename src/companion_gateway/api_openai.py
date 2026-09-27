from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .config import AppConfig
from .context import ContextBudgetError
from .memory import text_from_content
from .serialization import canonical
from .service import CompanionService

_ERROR_DOCS = {
    401: {"description": "https://github.com/Somme4096/sophia"},
}


def create_openai_router(cfg: AppConfig, service: CompanionService, authorized) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/v1/models",
        tags=["proxy"],
        dependencies=[Depends(authorized)],
        responses={401: _ERROR_DOCS[401]},
    )
    async def models(request: Request) -> Response:
        return await _proxy_passthrough(request, cfg, "/models")

    @router.post(
        "/v1/chat/completions",
        tags=["proxy"],
        dependencies=[Depends(authorized)],
        responses={401: _ERROR_DOCS[401]},
    )
    async def chat_completions(request: Request) -> Response:
        if not cfg.upstream.base_url:
            raise HTTPException(status_code=503, detail="upstream.base_url is not configured")
        body = await request.json()
        messages = body.get("messages")
        if not isinstance(messages, list):
            raise HTTPException(status_code=422, detail="messages must be a list")

        conversation_id = request.headers.get("x-conversation-id", "default")
        harness = request.headers.get("x-harness", "openai")
        route = request.headers.get("x-companion-route", "")
        transcript_key, current_message_id, current_query = await _ingest_transcript(
            service,
            messages,
            harness=harness,
            conversation_id=conversation_id,
            route=route,
        )

        try:
            state_context = await asyncio.to_thread(
                service.build_context,
                query=current_query,
                harness=harness,
                conversation_id=conversation_id,
                exclude_message_ids={current_message_id} if current_message_id else set(),
                include_recent=False,
            )
        except ContextBudgetError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        body["messages"] = _inject_context(messages, state_context["injection"])
        if body.get("stream"):
            return await _proxy_stream(
                request,
                cfg,
                body,
                service,
                harness,
                conversation_id,
                route,
                transcript_key,
            )
        response = await _upstream_request(request, cfg, "/chat/completions", body)
        if response.status_code < 400:
            with contextlib.suppress(Exception):
                data = response.json()
                response_message = data["choices"][0]["message"]
                await _ingest_assistant_response(
                    service,
                    transcript_key=transcript_key,
                    harness=harness,
                    conversation_id=conversation_id,
                    route=route,
                    message=response_message,
                    source_payload=response_message,
                )
        return _passthrough_response(response)

    return router

def _inject_context(messages: list[dict[str, Any]], injection: str) -> list[dict[str, Any]]:
    output = [dict(message) for message in messages]
    position = next(
        (index for index, message in enumerate(output) if message.get("role") != "system"),
        len(output),
    )
    output.insert(position, {"role": "system", "content": injection})
    return output


def _upstream_headers(request: Request, cfg: AppConfig) -> dict[str, str]:
    key = os.getenv(cfg.upstream.api_key_env, "") if cfg.upstream.api_key_env else ""
    authorization = f"Bearer {key}" if key else request.headers.get("authorization", "")
    headers = {"content-type": "application/json"}
    if authorization:
        headers["authorization"] = authorization
    return headers


def _upstream_url(cfg: AppConfig, path: str) -> str:
    return cfg.upstream.base_url.rstrip("/") + path


async def _upstream_request(request: Request, cfg: AppConfig, path: str, body: Any) -> httpx.Response:
    async with httpx.AsyncClient(timeout=cfg.upstream.timeout_seconds) as client:
        kwargs: dict[str, Any] = {"headers": _upstream_headers(request, cfg)}
        if body is not None:
            kwargs["json"] = body
        return await client.request(request.method, _upstream_url(cfg, path), **kwargs)


def _passthrough_response(
    response: httpx.Response, *, media_type: str | None = "application/json"
) -> Response:
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type", media_type),
    )


async def _proxy_passthrough(request: Request, cfg: AppConfig, path: str) -> Response:
    if not cfg.upstream.base_url:
        raise HTTPException(status_code=503, detail="upstream.base_url is not configured")
    response = await _upstream_request(request, cfg, path, None)
    return _passthrough_response(response)


async def _proxy_stream(
    request: Request,
    cfg: AppConfig,
    body: dict[str, Any],
    service: CompanionService,
    harness: str,
    conversation_id: str,
    route: str,
    transcript_key: str,
) -> Response:
    client = httpx.AsyncClient(timeout=cfg.upstream.timeout_seconds)
    upstream_request = client.build_request(
        "POST",
        _upstream_url(cfg, "/chat/completions"),
        headers=_upstream_headers(request, cfg),
        json=body,
    )
    response = await client.send(upstream_request, stream=True)
    if response.status_code >= 400:
        await response.aread()
        await response.aclose()
        await client.aclose()
        return _passthrough_response(response, media_type=None)

    async def chunks() -> AsyncIterator[bytes]:
        pending = ""
        assistant_parts: list[str] = []
        try:
            async for chunk in response.aiter_bytes():
                pending += chunk.decode("utf-8", errors="replace")
                lines = pending.split("\n")
                pending = lines.pop()
                for line in lines:
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    with contextlib.suppress(Exception):
                        data = json.loads(line[6:])
                        value = data["choices"][0]["delta"].get("content")
                        if isinstance(value, str):
                            assistant_parts.append(value)
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()
            text = "".join(assistant_parts)
            if text:
                await _ingest_assistant_response(
                    service,
                    transcript_key=transcript_key,
                    harness=harness,
                    conversation_id=conversation_id,
                    route=route,
                    message={"role": "assistant", "content": text},
                )

    return StreamingResponse(chunks(), status_code=response.status_code, media_type="text/event-stream")


def _next_transcript_key(previous: str, message: dict[str, Any]) -> str:
    return hashlib.sha256(f"{previous}\n{canonical(message)}".encode()).hexdigest()


async def _ingest_transcript(
    service: CompanionService,
    messages: list[Any],
    *,
    harness: str,
    conversation_id: str,
    route: str,
) -> tuple[str, int | None, str]:
    transcript_key = ""
    current_message_id: int | None = None
    current_query = ""
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {
            "user",
            "assistant",
            "tool",
        }:
            continue
        transcript_key = _next_transcript_key(transcript_key, message)
        message_content = message.get("content")
        if not text_from_content(message_content).strip():
            message_content = message
        result = await asyncio.to_thread(
            service.ingest_message,
            harness=harness,
            conversation_id=conversation_id,
            role=message["role"],
            content=message_content,
            route=route,
            external_id=f"proxy:{transcript_key}",
            source_payload=message,
        )
        if message["role"] == "user":
            current_message_id = result["id"]
            current_query = service.memory.get(current_message_id)["text"]
    return transcript_key, current_message_id, current_query


async def _ingest_assistant_response(
    service: CompanionService,
    *,
    transcript_key: str,
    harness: str,
    conversation_id: str,
    route: str,
    message: dict[str, Any],
    source_payload: Any | None = None,
) -> None:
    content = message.get("content")
    if not text_from_content(content).strip():
        content = message
    response_key = _next_transcript_key(transcript_key, message)
    await asyncio.to_thread(
        service.ingest_message,
        harness=harness,
        conversation_id=conversation_id,
        role="assistant",
        content=content,
        route=route,
        external_id=f"proxy:{response_key}",
        source_payload=source_payload,
    )
