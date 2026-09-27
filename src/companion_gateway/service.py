from __future__ import annotations

from datetime import datetime
from typing import Any

from . import identity as _identity
from . import prompts as _prompts
from .affect import AffectEngine
from .config import AppConfig
from .context import ContextComposer
from .database import Database
from .decision import DecisionProvider
from .memory_plugin import (
    ConversationIdRequest,
    EnsureMessageChunksRequest,
    GetMessageRequest,
    IngestMessageRequest,
    MatchPhraseRequest,
    RecentMessagesRequest,
    RenderFactsRequest,
    SearchRequest,
    GetMessageResult,
    ConversationIdResult,
    RecentMessagesResult,
    SearchResult,
    RenderFactsResult,
)
from .memory_provider import MemoryProvider
from .memory import MemoryStore
from .semantic import SemanticIndex
from .evergreen import EvergreenStore
from .timeutil import parse_time


class CompanionService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.database = Database(config.database_path)
        self.memory = MemoryProvider(config)
        self.semantic = None
        self._memory_fallback = None
        self._evergreen_fallback = None
        self._semantic_fallback = None
        if not self.memory.enabled:
            self.semantic = SemanticIndex(self.database, config.memory)
            self._semantic_fallback = self.semantic
            self._memory_fallback = MemoryStore(self.database, self.semantic)
            self._evergreen_fallback = EvergreenStore(self.database)
        self.memory.fallback = self._memory_fallback
        self.evergreen = self._evergreen_fallback
        self.prompts = _prompts.resolve_prompts(config.prompts)
        self.prompts_fingerprint = _prompts.fingerprint(self.prompts)
        self.identity_text, self.identity_revision = _identity.load_identity(
            config.identity_prompt
        )
        self.identity_configured = bool(config.identity_prompt.path)
        self.decision = DecisionProvider(config)
        self.affect = AffectEngine(
            self.database,
            config.affect,
            prompts=self.prompts,
            decision_increment=config.decision.increment,
        )
        self.phrase_matcher = self._phrase_matcher if self.memory.enabled else None
        self.composer = ContextComposer(
            prompts=self.prompts,
            budget=config.memory.injection_max_chars,
            identity_text=self.identity_text,
            identity_configured=self.identity_configured,
            identity_revision=self.identity_revision,
            emotions_fingerprint=self.affect.emotions_fingerprint,
            prompts_fingerprint=self.prompts_fingerprint,
        )

    def ingest_message(
        self,
        *,
        harness: str,
        conversation_id: str,
        role: str,
        content: Any,
        route: str = "",
        external_id: str = "",
        occurred_at: str | datetime | None = None,
        source_payload: Any | None = None,
    ) -> dict[str, Any]:
        request = IngestMessageRequest(
                harness=harness,
                conversation_id=conversation_id,
                role=role,
                content=content,
                route=route,
                external_id=external_id,
                occurred_at=occurred_at,
                source_payload=source_payload,
            )
        message = self._memory_call(
            "ingest", request,
            lambda: self._memory_fallback.ingest(
                harness=request.harness, conversation_id=request.conversation_id, role=request.role,
                content=request.content, route=request.route, external_id=request.external_id,
                occurred_at=request.occurred_at, source_payload=request.source_payload,
            ),
        )
        if message is not None and not message.duplicate:
            if self.memory.enabled:
                self._memory_call(
                    "ensure_message_chunks", EnsureMessageChunksRequest(message.id),
                    lambda: self._semantic_fallback.ensure_message_chunks(message.id),
                )
            else:
                self._semantic_fallback.ensure_message_chunks(message.id)
        if not self.memory.enabled:
            stored_message = self._memory_fallback.get(message.id)
            affect = None
            if role == "user" and stored_message is not None and not message.duplicate:
                message_time = parse_time(stored_message["occurred_at"])
                affect = self.affect.record_user_message(
                    message=stored_message["text"], source_message_id=message.id,
                    decider=self.decision.evaluate if self.decision.enabled else None,
                    instruction=str(self.prompts["decision_instruction"]), now=message_time,
                    phrase_matcher=self.phrase_matcher,
                )
            return {"id": message.id, "duplicate": message.duplicate,
                    "conversation_id": message.conversation_id, "sha256": message.sha256,
                    "affect": affect}
        affect = None
        if role == "user" and not message.duplicate:
            stored_result = self._memory_call(
                "get", GetMessageRequest(message.id),
                lambda: GetMessageResult(self._memory_fallback.get(message.id)),
            )
            stored_message = stored_result.message
            if stored_message is None:
                return {
                    "id": message.id,
                    "duplicate": message.duplicate,
                    "conversation_id": message.conversation_id,
                    "sha256": message.sha256,
                    "affect": None,
                }
            message_time = parse_time(stored_message["occurred_at"])
            affect = self.affect.record_user_message(
                message=stored_message["text"],
                source_message_id=message.id,
                decider=self.decision.evaluate if self.decision.enabled else None,
                instruction=str(self.prompts["decision_instruction"]),
                now=message_time,
                phrase_matcher=self.phrase_matcher,
            )
        return {
            "id": message.id,
            "duplicate": message.duplicate,
            "conversation_id": message.conversation_id,
            "sha256": message.sha256,
            "affect": affect,
        }

    def build_context(
        self,
        *,
        query: str,
        harness: str = "",
        conversation_id: str = "",
        exclude_message_ids: set[int] | None = None,
        include_recent: bool = True,
    ) -> dict[str, Any]:
        excluded = exclude_message_ids or set()
        internal_conversation = None
        if harness and conversation_id:
            conversation_result = self._memory_call(
                "conversation_id", ConversationIdRequest(harness, conversation_id),
                lambda: ConversationIdResult(self._memory_fallback.conversation_id(harness, conversation_id)),
            )
            internal_conversation = conversation_result.conversation_id

        if not self.memory.enabled:
            recent = self._memory_fallback.recent(
                conversation_id=internal_conversation, limit=self.config.memory.recent_messages,
                exclude_ids=excluded,
            )
            hits = self._memory_fallback.search(
                query, self.config.memory.search_hits, self.config.memory.context_messages, excluded | {item["id"] for item in recent}
            ) if query.strip() else []
        else:
            recent_result = self._memory_call("recent", RecentMessagesRequest(
                harness=harness,
                conversation_id=conversation_id,
                limit=self.config.memory.recent_messages,
                exclude_ids=tuple(excluded),
            ), lambda: RecentMessagesResult(self._memory_fallback.recent(
                conversation_id=internal_conversation, limit=self.config.memory.recent_messages,
                exclude_ids=excluded,
            )))
            recent = recent_result.messages
        recent_ids = {item["id"] for item in recent}
        hits = hits if not self.memory.enabled else (
            self._memory_call(
                "search", SearchRequest(
                    query=query,
                    limit=self.config.memory.search_hits,
                    context_messages=self.config.memory.context_messages,
                    exclude_ids=tuple(excluded | recent_ids),
                ), lambda: SearchResult(self._memory_fallback.search(
                    query, self.config.memory.search_hits, self.config.memory.context_messages,
                    excluded | recent_ids,
                ))
            ).results
            if query.strip()
            else []
        )

        records: list[dict[str, Any]] = []
        seen: set[int] = set()
        if include_recent:
            for message in recent:
                if message["id"] not in seen:
                    records.append(self._record(message, "recent"))
                    seen.add(message["id"])
        else:
            seen.update(recent_ids)
        for hit in hits:
            for message in hit["messages"]:
                if message["id"] not in seen and message["id"] not in excluded:
                    records.append(self._record(message, "recalled"))
                    seen.add(message["id"])

        affect_snapshot = self.affect.status()
        affect_text = self.affect.describe(affect_snapshot)
        evergreen_facts: list[dict[str, Any]] = []
        if self.config.evergreen.enabled:
            evergreen = self.prompts["evergreen"]
            if not self.memory.enabled:
                _, evergreen_facts = self._evergreen_fallback.render(
                    self.config.evergreen.max_items, self.config.evergreen.max_chars,
                    open_delimiter=evergreen["open_delimiter"], close_delimiter=evergreen["close_delimiter"],
                )
            else:
                evergreen_facts = self._memory_call(
                    "render_facts",
                RenderFactsRequest(
                    max_items=self.config.evergreen.max_items,
                    max_chars=self.config.evergreen.max_chars,
                    open_delimiter=evergreen["open_delimiter"],
                    close_delimiter=evergreen["close_delimiter"],
                ), lambda: RenderFactsResult(
                    *self._evergreen_fallback.render(
                        self.config.evergreen.max_items, self.config.evergreen.max_chars,
                        open_delimiter=evergreen["open_delimiter"],
                        close_delimiter=evergreen["close_delimiter"],
                    )
                )
                ).facts
        composed = self.composer.compose(
            affect_snapshot=affect_snapshot,
            affect_text=affect_text,
            evergreen_facts=evergreen_facts,
            session_records=records,
        )
        return {
            "injection": composed["injection"],
            "affect": affect_snapshot,
            "evergreen_facts": composed["evergreen_facts"],
            "records": composed["records"],
            "search_hits": hits,
            "context": composed["context"],
        }

    @staticmethod
    def _record(message: dict[str, Any], source: str) -> dict[str, Any]:
        return {
            "memory_id": message["id"],
            "source": source,
            "time": message["occurred_at"],
            "role": message["role"],
            "text": message["text"],
        }

    def latest_route(self) -> dict[str, Any] | None:
        with self.database.connect() as db:
            row = db.execute(
                """SELECT id, harness, external_id, route
                   FROM conversations
                   WHERE route != ''
                   ORDER BY updated_at DESC, id DESC LIMIT 1""",
            ).fetchone()
        return dict(row) if row else None

    def _memory_call(self, method: str, request: Any, fallback: Any) -> Any:
        if not self.memory.enabled:
            return fallback()
        return getattr(self.memory, method)(request)

    def _phrase_matcher(self, message: str) -> dict[str, float] | None:
        result = self.memory.match_phrase(MatchPhraseRequest(message=message))
        return result.deltas if result is not None else None

    def backup(self, destination: str) -> str:
        return str(self.database.backup(destination))

    def close(self) -> None:
        self.memory.close()
        if self.semantic is not None:
            self.semantic.close()
