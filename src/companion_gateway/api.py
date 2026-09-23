from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .affect import AffectClassificationConflict
from .config import AppConfig, load_config
from .context import ContextBudgetError
from .evergreen import EvergreenConflict
from .memory import text_from_content
from .proactive import ProactiveEngine
from .service import CompanionService
from .timeutil import parse_time

logger = logging.getLogger("companion_gateway")


class GatewayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageInput(GatewayInput):
    harness: str = "api"
    conversation_id: str = "default"
    role: str
    content: Any
    route: str = ""
    external_id: str = ""
    occurred_at: str | None = None
    affect_label: str = ""


class ContextInput(GatewayInput):
    query: str = ""
    harness: str = ""
    conversation_id: str = ""
    exclude_message_ids: list[int] = Field(default_factory=list)
    include_recent: bool = True


class SearchInput(GatewayInput):
    query: str
    limit: int = 8
    context_messages: int = 1


class RememberFactInput(GatewayInput):
    key: str
    text: str
    priority: int = Field(default=50, ge=0, le=100)
    source_message_id: int | None = None
    reason: str = ""
    review_after: str | None = None
    expires_at: str | None = None


class ReviseFactInput(GatewayInput):
    expected_revision: int = Field(ge=1)
    text: str
    priority: int | None = Field(default=None, ge=0, le=100)
    source_message_id: int | None = None
    reason: str = ""
    review_after: str | None = None
    expires_at: str | None = None


class ForgetFactInput(GatewayInput):
    expected_revision: int = Field(ge=1)
    reason: str
    source_message_id: int | None = None


class AffectEventInput(GatewayInput):
    label: str
    note: str = ""
    occurred_at: str | None = None
    follow_up_minutes: int | None = None


class RecordAffectInput(GatewayInput):
    label: str


class AckInput(GatewayInput):
    consumer: str
    outcome: str
    text: str = ""
    external_id: str = ""
    error: str = ""


async def _scheduler(proactive: ProactiveEngine, interval: int) -> None:
    while True:
        try:
            await asyncio.to_thread(proactive.service.affect.finalize_due)
            await asyncio.to_thread(proactive.evaluate)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("proactive evaluation failed")
        await asyncio.sleep(max(5, interval))


async def _semantic_scheduler(service: CompanionService, interval: int) -> None:
    while True:
        try:
            result = await asyncio.to_thread(service.semantic.backfill_once)
            if result.get("error") and not result.get("cooling_down"):
                logger.warning("embedding backfill unavailable: %s", result["error"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("embedding backfill failed")
        await asyncio.sleep(max(1, interval))


def create_app(config: AppConfig | None = None) -> FastAPI:
    cfg = config or load_config()
    service = CompanionService(cfg)
    proactive = ProactiveEngine(service, cfg)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        tasks = [
            asyncio.create_task(
                _scheduler(proactive, cfg.proactive.poll_interval_seconds),
                name="proactive-evaluator",
            )
        ]
        if service.semantic.enabled:
            tasks.append(
                asyncio.create_task(
                    _semantic_scheduler(service, cfg.memory.embedding.backfill_interval_seconds),
                    name="memory-embedding-backfill",
                )
            )
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            service.close()

    app = FastAPI(title="Companion State Gateway", version="0.1.0", lifespan=lifespan)
    app.state.config = cfg
    app.state.service = service
    app.state.proactive = proactive

    async def authorized(x_companion_token: str = Header(default="")) -> None:
        if cfg.api_token_env:
            expected = os.getenv(cfg.api_token_env, "")
            if not expected or x_companion_token != expected:
                raise HTTPException(status_code=401, detail="invalid companion token")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "database": service.database.integrity_check(),
            "upstream_configured": bool(cfg.upstream.base_url),
            "memory_index": service.semantic.status(),
        }

    @app.get("/state/v1/memory/index", dependencies=[Depends(authorized)])
    async def memory_index_status() -> dict[str, Any]:
        return await asyncio.to_thread(service.semantic.status)

    @app.post("/state/v1/messages", dependencies=[Depends(authorized)])
    async def ingest_message(body: MessageInput) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                service.ingest_message,
                harness=body.harness,
                conversation_id=body.conversation_id,
                role=body.role,
                content=body.content,
                route=body.route,
                external_id=body.external_id,
                occurred_at=body.occurred_at,
                affect_label=body.affect_label,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/state/v1/messages/{message_id}", dependencies=[Depends(authorized)])
    async def get_message(message_id: int) -> dict[str, Any]:
        message = await asyncio.to_thread(service.memory.get, message_id)
        if not message:
            raise HTTPException(status_code=404, detail="message not found")
        return message

    @app.post("/state/v1/memory/search", dependencies=[Depends(authorized)])
    async def search_memory(body: SearchInput) -> dict[str, Any]:
        results = await asyncio.to_thread(
            service.memory.search,
            body.query,
            max(1, min(body.limit, 50)),
            max(0, min(body.context_messages, 10)),
        )
        return {"results": results}

    @app.get("/state/v1/memory/{message_id}", dependencies=[Depends(authorized)])
    async def memory_context(
        message_id: int,
        context_messages: int = Query(1, ge=0, le=10),
    ) -> dict[str, Any]:
        messages = await asyncio.to_thread(
            service.memory.context,
            message_id,
            context_messages,
        )
        if not messages:
            raise HTTPException(status_code=404, detail="memory record not found")
        return {"messages": messages}

    @app.post("/state/v1/evergreen/facts", dependencies=[Depends(authorized)])
    async def remember_fact(body: RememberFactInput) -> dict[str, Any]:
        try:
            fact = await asyncio.to_thread(
                service.evergreen.remember,
                key=body.key,
                text=body.text,
                priority=body.priority,
                source_message_id=body.source_message_id,
                reason=body.reason,
                review_after=body.review_after,
                expires_at=body.expires_at,
                created_by="agent",
            )
        except EvergreenConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"fact": fact}

    @app.get("/state/v1/evergreen/facts", dependencies=[Depends(authorized)])
    async def list_facts(
        include_inactive: bool = False,
        due_only: bool = False,
        limit: int = Query(100, ge=1, le=500),
    ) -> dict[str, Any]:
        facts = await asyncio.to_thread(
            service.evergreen.list_current,
            include_inactive=include_inactive,
            due_only=due_only,
            limit=limit,
        )
        return {"facts": facts}

    @app.get("/state/v1/evergreen/facts/{fact_id}/history", dependencies=[Depends(authorized)])
    async def fact_history(fact_id: str) -> dict[str, Any]:
        try:
            revisions = await asyncio.to_thread(
                service.evergreen.history,
                fact_id,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="evergreen fact not found") from error
        return {"revisions": revisions}

    @app.post(
        "/state/v1/evergreen/facts/{fact_id}/revisions",
        dependencies=[Depends(authorized)],
    )
    async def revise_fact(fact_id: str, body: ReviseFactInput) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "fact_id": fact_id,
            "expected_revision": body.expected_revision,
            "text": body.text,
            "priority": body.priority,
            "source_message_id": body.source_message_id,
            "reason": body.reason,
            "created_by": "agent",
        }
        if "review_after" in body.model_fields_set:
            kwargs["review_after"] = body.review_after
        if "expires_at" in body.model_fields_set:
            kwargs["expires_at"] = body.expires_at
        try:
            fact = await asyncio.to_thread(service.evergreen.revise, **kwargs)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="evergreen fact not found") from error
        except EvergreenConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"fact": fact}

    @app.post("/state/v1/evergreen/facts/{fact_id}/forget", dependencies=[Depends(authorized)])
    async def forget_fact(fact_id: str, body: ForgetFactInput) -> dict[str, Any]:
        try:
            fact = await asyncio.to_thread(
                service.evergreen.forget,
                fact_id=fact_id,
                expected_revision=body.expected_revision,
                reason=body.reason,
                source_message_id=body.source_message_id,
                created_by="agent",
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="evergreen fact not found") from error
        except EvergreenConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"fact": fact}

    @app.post("/state/v1/context", dependencies=[Depends(authorized)])
    async def context(body: ContextInput) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                service.build_context,
                query=body.query,
                harness=body.harness,
                conversation_id=body.conversation_id,
                exclude_message_ids=set(body.exclude_message_ids),
                include_recent=body.include_recent,
            )
        except ContextBudgetError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/state/v1/affect", dependencies=[Depends(authorized)])
    async def affect_status() -> dict[str, Any]:
        return await asyncio.to_thread(service.affect.status)

    @app.post(
        "/state/v1/messages/{message_id}/affect",
        dependencies=[Depends(authorized)],
    )
    async def record_message_affect(message_id: int, body: RecordAffectInput) -> dict[str, Any]:
        try:
            event = await asyncio.to_thread(
                service.affect.record_agent_label,
                message_id,
                body.label,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="affect classification not found") from error
        except AffectClassificationConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"event": event}

    @app.post("/state/v1/affect/events", dependencies=[Depends(authorized)])
    async def affect_event(body: AffectEventInput) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(
                service.affect.apply_label,
                body.label,
                now=parse_time(body.occurred_at),
                note=body.note,
                follow_up_minutes=body.follow_up_minutes,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"label": result.label, "event_id": result.event_id, "state": result.state}

    @app.post("/state/v1/proactive/evaluate", dependencies=[Depends(authorized)])
    async def evaluate_proactive() -> dict[str, Any]:
        event = await asyncio.to_thread(proactive.evaluate)
        return {"event": event}

    @app.get("/state/v1/proactive/events", dependencies=[Depends(authorized)])
    async def poll_proactive(
        consumer: str = Query(..., min_length=1),
        harness: str = "",
        limit: int = Query(1, ge=1, le=20),
    ) -> dict[str, Any]:
        events = await asyncio.to_thread(proactive.poll, consumer, limit, None, harness)
        return {"events": events}

    @app.post("/state/v1/proactive/events/{event_id}/ack", dependencies=[Depends(authorized)])
    async def acknowledge_proactive(event_id: str, body: AckInput) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                proactive.acknowledge,
                event_id,
                body.consumer,
                body.outcome,
                text=body.text,
                external_id=body.external_id,
                error=body.error,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="event not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        return await _proxy_simple(request, cfg, "/models")

    @app.post("/v1/chat/completions")
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
        transcript_key = ""
        current_message_id = None
        current_query = ""
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in {
                "user",
                "assistant",
                "tool",
            }:
                continue
            canonical = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            transcript_key = hashlib.sha256(f"{transcript_key}\n{canonical}".encode()).hexdigest()
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
                content = response_message.get("content")
                if not text_from_content(content).strip():
                    content = response_message
                response_key = _next_transcript_key(transcript_key, response_message)
                await asyncio.to_thread(
                    service.ingest_message,
                    harness=harness,
                    conversation_id=conversation_id,
                    role="assistant",
                    content=content,
                    route=route,
                    external_id=f"proxy:{response_key}",
                    source_payload=response_message,
                )
        return Response(
            content=response.content,
            status_code=response.status_code,
            media_type=response.headers.get("content-type", "application/json"),
        )

    return app


def _inject_context(messages: list[dict[str, Any]], injection: str) -> list[dict[str, Any]]:
    output = [dict(message) for message in messages]
    position = 0
    while position < len(output) and output[position].get("role") == "system":
        position += 1
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


async def _proxy_simple(request: Request, cfg: AppConfig, path: str) -> Response:
    if not cfg.upstream.base_url:
        raise HTTPException(status_code=503, detail="upstream.base_url is not configured")
    response = await _upstream_request(request, cfg, path, None)
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "application/json"),
    )


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
        content = await response.aread()
        await response.aclose()
        await client.aclose()
        return Response(
            content=content,
            status_code=response.status_code,
            media_type=response.headers.get("content-type"),
        )

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
                response_key = _next_transcript_key(transcript_key, {"role": "assistant", "content": text})
                await asyncio.to_thread(
                    service.ingest_message,
                    harness=harness,
                    conversation_id=conversation_id,
                    role="assistant",
                    content=text,
                    route=route,
                    external_id=f"proxy:{response_key}",
                )

    return StreamingResponse(chunks(), status_code=response.status_code, media_type="text/event-stream")


def _next_transcript_key(previous: str, message: dict[str, Any]) -> str:
    canonical = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{previous}\n{canonical}".encode()).hexdigest()
