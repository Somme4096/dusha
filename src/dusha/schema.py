from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class ExtraAllow(BaseModel):
    model_config = ConfigDict(extra="allow")


class ErrorDetail(ExtraAllow):
    detail: str | list[dict[str, Any]]


class HealthResponse(ExtraAllow):
    status: str
    companion: str = ""
    database: str
    plugins: list[str]


class MessageResponse(ExtraAllow):
    id: int
    conversation_id: int
    role: str
    text: str
    content: object
    external_id: str
    occurred_at: str
    ingested_at: str
    harness: str
    external_conversation_id: str
    route: str


class PluginsStatus(ExtraAllow):
    pass


class IngestResponse(ExtraAllow):
    id: int
    duplicate: bool
    conversation_id: int


class TranscriptResponse(ExtraAllow):
    conversation_id: str
    message_id: int | None
    last_message_id: int | None
    query: str


class AffectStatusResponse(ExtraAllow):
    base: dict
    mood: dict
    last_updated_at: str
    last_user_message_at: str | None
    last_proactive_sent_at: str | None
    unanswered_proactive: int


class SearchResponse(ExtraAllow):
    results: list


class MemoryContextResponse(ExtraAllow):
    messages: list


class FactEnvelope(ExtraAllow):
    fact: dict


class FactsEnvelope(ExtraAllow):
    facts: list


class RevisionsEnvelope(ExtraAllow):
    revisions: list


class MemoEnvelope(ExtraAllow):
    memo: dict


class MemosEnvelope(ExtraAllow):
    memos: list


class ProactiveEvaluateResponse(ExtraAllow):
    event: dict | None


class ProactivePollResponse(ExtraAllow):
    events: list


class AckResponse(ExtraAllow):
    id: str
    outcome: str


class ContextMetadata(ExtraAllow):
    version: int
    instructions: dict
    session: list
    blocks: dict


class ContextResponse(ExtraAllow):
    injection: str
    records: list
    search_hits: list
    context: ContextMetadata
