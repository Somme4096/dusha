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
    upstream_configured: bool
    memory_index: dict


class MessageResponse(ExtraAllow):
    id: int
    conversation_id: int
    role: str
    text: str
    content: object
    external_id: str
    occurred_at: str
    ingested_at: str
    sha256: str
    harness: str
    external_conversation_id: str
    route: str


class MemoryIndexStatus(ExtraAllow):
    mode: str
    enabled: bool
    configured: bool
    chunker_key: str
    embedding_key: str
    messages: int
    chunked_messages: int
    chunks: int
    embedded_chunks: int
    dimensions: list
    cooling_down: bool
    last_error: str


class IngestResponse(ExtraAllow):
    id: int
    duplicate: bool
    conversation_id: int
    sha256: str
    affect: dict | None


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


class IdentityMetadata(ExtraAllow):
    configured: bool
    text: str
    revision: str


class EmotionMetadata(ExtraAllow):
    values: dict
    description: str
    preface: str
    fingerprints: dict


class MemoryMetadata(ExtraAllow):
    evergreen: list
    session: list


class ContextMetadata(ExtraAllow):
    version: int
    identity: IdentityMetadata
    instructions: dict
    memory: MemoryMetadata
    emotion: EmotionMetadata


class ContextResponse(ExtraAllow):
    injection: str
    affect: dict
    evergreen_facts: list
    records: list
    search_hits: list
    context: ContextMetadata