from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from . import schema as _schema
from .context import ContextBudgetError
from .evergreen import EvergreenConflict, EvergreenStore
from .memory import MemoryStore
from .proactive import ProactiveEngine
from .service import CompanionService

_ERROR_DOCS = {
    401: {"model": _schema.ErrorDetail, "description": "https://github.com/Somme4096/dusha"},
    404: {"model": _schema.ErrorDetail, "description": "https://github.com/Somme4096/dusha"},
    409: {"model": _schema.ErrorDetail, "description": "https://github.com/Somme4096/dusha"},
    422: {"model": _schema.ErrorDetail, "description": "https://github.com/Somme4096/dusha"},
    503: {"model": _schema.ErrorDetail, "description": "https://github.com/Somme4096/dusha"},
}

_STORAGE_DISABLED = "built-in message storage is disabled"

def _raise_http(
    error: Exception,
    *,
    not_found: str | None = None,
    conflict: type[Exception] | tuple[type[Exception], ...] = (),
    invalid: int = 422,
) -> None:
    if isinstance(error, KeyError):
        raise HTTPException(status_code=404, detail=not_found or "resource not found") from error
    if isinstance(error, conflict):
        raise HTTPException(status_code=409, detail=str(error)) from error
    if isinstance(error, ValueError):
        raise HTTPException(status_code=invalid, detail=str(error)) from error
    raise error

async def _service_call(
    function,
    /,
    *args,
    errors: tuple[type[Exception], ...],
    not_found: str | None = None,
    conflict: type[Exception] | tuple[type[Exception], ...] = (),
    invalid: int = 422,
    **kwargs,
):
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except Exception as error:
        if not isinstance(error, errors):
            raise
        _raise_http(error, not_found=not_found, conflict=conflict, invalid=invalid)

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


class AckInput(GatewayInput):
    consumer: str
    outcome: str
    text: str = ""
    external_id: str = ""
    error: str = ""


class MemoAddInput(GatewayInput):
    text: str


class MemoDoneInput(GatewayInput):
    reason: str


def create_state_router(service: CompanionService, proactive: ProactiveEngine, authorized) -> APIRouter:
    router = APIRouter()
    memory_store: MemoryStore | None = service._memory_fallback
    evergreen_store: EvergreenStore | None = service._evergreen_fallback

    async def require_storage() -> None:
        if not service.storage_enabled:
            raise HTTPException(status_code=503, detail=_STORAGE_DISABLED)

    @router.get(
        "/state/v1/memory/index",
        tags=["state"],
        dependencies=[Depends(authorized), Depends(require_storage)],
        responses={
            401: _ERROR_DOCS[401],
            503: _ERROR_DOCS[503],
            200: {"model": _schema.MemoryIndexStatus, "description": "https://github.com/Somme4096/dusha"},
        },
    )
    async def memory_index_status() -> dict[str, Any]:
        return await asyncio.to_thread(service.memory_index_status)

    @router.post(
        "/state/v1/messages",
        tags=["state"],
        response_model=_schema.IngestResponse,
        summary="Ingest one message",
        dependencies=[Depends(authorized), Depends(require_storage)],
        responses={401: _ERROR_DOCS[401], 422: _ERROR_DOCS[422], 503: _ERROR_DOCS[503]},
    )
    async def ingest_message(body: MessageInput) -> dict[str, Any]:
        return await _service_call(
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
        dependencies=[Depends(authorized), Depends(require_storage)],
        responses={
            401: _ERROR_DOCS[401],
            404: _ERROR_DOCS[404],
            503: _ERROR_DOCS[503],
            200: {"model": _schema.MessageResponse, "description": "https://github.com/Somme4096/dusha"},
        },
    )
    async def get_message(message_id: int) -> dict[str, Any]:
        message = await asyncio.to_thread(memory_store.get, message_id)
        if not message:
            raise HTTPException(status_code=404, detail="message not found")
        return message

    @router.post(
        "/state/v1/memory/search",
        tags=["state"],
        response_model=_schema.SearchResponse,
        summary="Search archived memory",
        dependencies=[Depends(authorized), Depends(require_storage)],
        responses={401: _ERROR_DOCS[401], 503: _ERROR_DOCS[503]},
    )
    async def search_memory(body: SearchInput) -> dict[str, Any]:
        results = await asyncio.to_thread(
            memory_store.search,
            body.query,
            max(1, min(body.limit, 50)),
            max(0, min(body.context_messages, 10)),
        )
        return {"results": results}

    @router.get(
        "/state/v1/memory/{message_id}",
        tags=["state"],
        response_model=_schema.MemoryContextResponse,
        dependencies=[Depends(authorized), Depends(require_storage)],
        responses={401: _ERROR_DOCS[401], 404: _ERROR_DOCS[404], 503: _ERROR_DOCS[503]},
    )
    async def memory_context(
        message_id: int,
        context_messages: int = Query(1, ge=0, le=10),
    ) -> dict[str, Any]:
        messages = await asyncio.to_thread(memory_store.context, message_id, context_messages)
        if not messages:
            raise HTTPException(status_code=404, detail="memory record not found")
        return {"messages": messages}

    @router.post(
        "/state/v1/evergreen/facts",
        tags=["state"],
        response_model=_schema.FactEnvelope,
        summary="Remember an evergreen fact",
        dependencies=[Depends(authorized), Depends(require_storage)],
        responses={401: _ERROR_DOCS[401], 409: _ERROR_DOCS[409], 422: _ERROR_DOCS[422], 503: _ERROR_DOCS[503]},
    )
    async def remember_fact(body: RememberFactInput) -> dict[str, Any]:
        fact = await _service_call(
            evergreen_store.remember,
            key=body.key,
            text=body.text,
            priority=body.priority,
            source_message_id=body.source_message_id,
            reason=body.reason,
            review_after=body.review_after,
            expires_at=body.expires_at,
            created_by="agent",
            errors=(EvergreenConflict, ValueError),
            conflict=EvergreenConflict,
        )
        return {"fact": fact}

    @router.get(
        "/state/v1/evergreen/facts",
        tags=["state"],
        response_model=_schema.FactsEnvelope,
        dependencies=[Depends(authorized), Depends(require_storage)],
        responses={401: _ERROR_DOCS[401], 503: _ERROR_DOCS[503]},
    )
    async def list_facts(
        include_inactive: bool = False,
        due_only: bool = False,
        limit: int = Query(100, ge=1, le=500),
    ) -> dict[str, Any]:
        facts = await asyncio.to_thread(
            evergreen_store.list_current,
            include_inactive=include_inactive,
            due_only=due_only,
            limit=limit,
        )
        return {"facts": facts}

    @router.get(
        "/state/v1/evergreen/facts/{fact_id}/history",
        tags=["state"],
        response_model=_schema.RevisionsEnvelope,
        dependencies=[Depends(authorized), Depends(require_storage)],
        responses={401: _ERROR_DOCS[401], 404: _ERROR_DOCS[404], 503: _ERROR_DOCS[503]},
    )
    async def fact_history(fact_id: str) -> dict[str, Any]:
        revisions = await _service_call(
            evergreen_store.history,
            fact_id,
            errors=(KeyError,),
            not_found="evergreen fact not found",
        )
        return {"revisions": revisions}

    @router.post(
        "/state/v1/evergreen/facts/{fact_id}/revisions",
        tags=["state"],
        response_model=_schema.FactEnvelope,
        summary="Revise an evergreen fact",
        dependencies=[Depends(authorized), Depends(require_storage)],
        responses={
            401: _ERROR_DOCS[401],
            404: _ERROR_DOCS[404],
            409: _ERROR_DOCS[409],
            422: _ERROR_DOCS[422],
            503: _ERROR_DOCS[503],
        },
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
        fact = await _service_call(
            evergreen_store.revise,
            **kwargs,
            errors=(KeyError, EvergreenConflict, ValueError),
            not_found="evergreen fact not found",
            conflict=EvergreenConflict,
        )
        return {"fact": fact}

    @router.post(
        "/state/v1/evergreen/facts/{fact_id}/forget",
        tags=["state"],
        response_model=_schema.FactEnvelope,
        dependencies=[Depends(authorized), Depends(require_storage)],
        responses={
            401: _ERROR_DOCS[401],
            404: _ERROR_DOCS[404],
            409: _ERROR_DOCS[409],
            422: _ERROR_DOCS[422],
            503: _ERROR_DOCS[503],
        },
    )
    async def forget_fact(fact_id: str, body: ForgetFactInput) -> dict[str, Any]:
        fact = await _service_call(
            evergreen_store.forget,
            fact_id=fact_id,
            expected_revision=body.expected_revision,
            reason=body.reason,
            source_message_id=body.source_message_id,
            created_by="agent",
            errors=(KeyError, EvergreenConflict, ValueError),
            not_found="evergreen fact not found",
            conflict=EvergreenConflict,
        )
        return {"fact": fact}

    @router.post(
        "/state/v1/memo/add",
        tags=["state"],
        response_model=_schema.MemoEnvelope,
        summary="Add an agent memo",
        dependencies=[Depends(authorized), Depends(require_storage)],
        responses={401: _ERROR_DOCS[401], 422: _ERROR_DOCS[422], 503: _ERROR_DOCS[503]},
    )
    async def memo_add(body: MemoAddInput) -> dict[str, Any]:
        memo = await _service_call(service.memo_add, body.text, errors=(ValueError,))
        return {"memo": memo}

    @router.get(
        "/state/v1/memo/list",
        tags=["state"],
        response_model=_schema.MemosEnvelope,
        dependencies=[Depends(authorized), Depends(require_storage)],
        responses={401: _ERROR_DOCS[401], 422: _ERROR_DOCS[422], 503: _ERROR_DOCS[503]},
    )
    async def memo_list(
        status: str = "active",
        limit: int = Query(20, ge=1, le=500),
    ) -> dict[str, Any]:
        memos = await _service_call(
            service.memo_list, status=status, limit=limit, errors=(ValueError,)
        )
        return {"memos": memos}

    @router.post(
        "/state/v1/memo/{note_id}/done",
        tags=["state"],
        response_model=_schema.MemoEnvelope,
        summary="Archive an agent memo with a reason",
        dependencies=[Depends(authorized), Depends(require_storage)],
        responses={
            401: _ERROR_DOCS[401],
            404: _ERROR_DOCS[404],
            422: _ERROR_DOCS[422],
            503: _ERROR_DOCS[503],
        },
    )
    async def memo_done(note_id: int, body: MemoDoneInput) -> dict[str, Any]:
        memo = await _service_call(
            service.memo_done,
            note_id,
            body.reason,
            errors=(KeyError, ValueError),
            not_found="memo not found",
        )
        return {"memo": memo}

    @router.post(
        "/state/v1/context",
        tags=["state"],
        response_model=_schema.ContextResponse,
        summary="Build provider-neutral context",
        dependencies=[Depends(authorized), Depends(require_storage)],
        responses={401: _ERROR_DOCS[401], 422: _ERROR_DOCS[422], 503: _ERROR_DOCS[503]},
    )
    async def context(body: ContextInput) -> dict[str, Any]:
        return await _service_call(
            service.build_context,
            query=body.query,
            harness=body.harness,
            conversation_id=body.conversation_id,
            exclude_message_ids=set(body.exclude_message_ids),
            include_recent=body.include_recent,
            errors=(ContextBudgetError,),
        )

    @router.get(
        "/state/v1/affect",
        tags=["state"],
        response_model=_schema.AffectStatusResponse,
        summary="Read affect state (advances decay)",
        dependencies=[Depends(authorized)],
        responses={401: _ERROR_DOCS[401]},
    )
    async def affect_status() -> dict[str, Any]:
        return await asyncio.to_thread(service.affect.status)

    @router.post(
        "/state/v1/proactive/evaluate",
        tags=["state"],
        response_model=_schema.ProactiveEvaluateResponse,
        dependencies=[Depends(authorized)],
        responses={401: _ERROR_DOCS[401]},
    )
    async def evaluate_proactive() -> dict[str, Any]:
        event = await asyncio.to_thread(proactive.evaluate)
        return {"event": event}

    @router.get(
        "/state/v1/proactive/events",
        tags=["state"],
        response_model=_schema.ProactivePollResponse,
        summary="Poll and lease proactive events",
        dependencies=[Depends(authorized)],
        responses={401: _ERROR_DOCS[401]},
    )
    async def poll_proactive(
        consumer: str = Query(..., min_length=1),
        harness: str = "",
        limit: int = Query(1, ge=1, le=20),
    ) -> dict[str, Any]:
        events = await asyncio.to_thread(proactive.poll, consumer, limit, None, harness)
        return {"events": events}

    @router.post(
        "/state/v1/proactive/events/{event_id}/ack",
        tags=["state"],
        response_model=_schema.AckResponse,
        dependencies=[Depends(authorized)],
        responses={401: _ERROR_DOCS[401], 404: _ERROR_DOCS[404], 409: _ERROR_DOCS[409]},
    )
    async def acknowledge_proactive(event_id: str, body: AckInput) -> dict[str, Any]:
        return await _service_call(
            proactive.acknowledge,
            event_id,
            body.consumer,
            body.outcome,
            text=body.text,
            external_id=body.external_id,
            error=body.error,
            errors=(KeyError, ValueError),
            not_found="event not found",
            invalid=409,
        )
    return router
