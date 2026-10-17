from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from . import schema as _schema
from .interfaces import DOCS, GatewayInput, call, errors
from .service import CompanionService


class MessageInput(GatewayInput):
    harness: str = "api"
    conversation_id: str = "default"
    role: str
    content: Any
    route: str = ""
    external_id: str = ""
    occurred_at: str | None = None


class ContextInput(GatewayInput):
    query: str = ""
    harness: str = ""
    conversation_id: str = ""
    exclude_message_ids: list[int] = Field(default_factory=list)
    include_recent: bool = True


def create_state_router(service: CompanionService, authorized) -> APIRouter:
    router = APIRouter(dependencies=[Depends(authorized)])

    @router.get(
        "/state/v1/plugins",
        tags=["state"],
        responses={**errors(), 200: {"model": _schema.PluginsStatus, "description": DOCS}},
    )
    async def plugins_status() -> dict[str, Any]:
        return await asyncio.to_thread(service.plugins_status)

    @router.post(
        "/state/v1/messages",
        tags=["state"],
        response_model=_schema.IngestResponse,
        summary="Ingest one message",
        responses=errors(422),
    )
    async def ingest_message(body: MessageInput) -> dict[str, Any]:
        return await call(
            service.ingest_message,
            harness=body.harness,
            conversation_id=body.conversation_id,
            role=body.role,
            content=body.content,
            route=body.route,
            external_id=body.external_id,
            occurred_at=body.occurred_at,
            errors=(ValueError,),
        )

    @router.get(
        "/state/v1/messages/{message_id}",
        tags=["state"],
        responses={**errors(404), 200: {"model": _schema.MessageResponse, "description": DOCS}},
    )
    async def get_message(message_id: int) -> dict[str, Any]:
        message = await asyncio.to_thread(service.messages.get, message_id)
        if not message:
            raise HTTPException(status_code=404, detail="message not found")
        return message

    @router.post(
        "/state/v1/context",
        tags=["state"],
        response_model=_schema.ContextResponse,
        summary="Build provider-neutral context",
        responses=errors(422),
    )
    async def context(body: ContextInput) -> dict[str, Any]:
        return await call(
            service.build_context,
            query=body.query,
            harness=body.harness,
            conversation_id=body.conversation_id,
            exclude_message_ids=set(body.exclude_message_ids),
            include_recent=body.include_recent,
        )

    router.include_router(service.router)
    return router
