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
from .evergreen import EvergreenStore
from .memory import MemoryStore
from .semantic import SemanticIndex
from .timeutil import parse_time


class CompanionService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.database = Database(config.database_path)
        self.semantic = SemanticIndex(self.database, config.memory)
        self.memory = MemoryStore(self.database, self.semantic)
        self.evergreen = EvergreenStore(self.database)
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
        message = self.memory.ingest(
            harness=harness,
            conversation_id=conversation_id,
            role=role,
            content=content,
            route=route,
            external_id=external_id,
            occurred_at=occurred_at,
            source_payload=source_payload,
        )
        if self.semantic.enabled and not message.duplicate:
            self.semantic.ensure_message_chunks(message.id)
        affect = None
        if role == "user" and not message.duplicate:
            stored_message = self.memory.get(message.id)
            message_time = parse_time(stored_message["occurred_at"])
            affect = self.affect.record_user_message(
                message=stored_message["text"],
                source_message_id=message.id,
                decider=self.decision.evaluate if self.decision.enabled else None,
                instruction=str(self.prompts["decision_instruction"]),
                now=message_time,
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
            internal_conversation = self.memory.conversation_id(harness, conversation_id)

        recent = self.memory.recent(
            internal_conversation,
            self.config.memory.recent_messages,
            excluded,
        )
        recent_ids = {item["id"] for item in recent}
        hits = (
            self.memory.search(
                query,
                self.config.memory.search_hits,
                self.config.memory.context_messages,
                excluded | recent_ids,
            )
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
            _, evergreen_facts = self.evergreen.render(
                self.config.evergreen.max_items,
                self.config.evergreen.max_chars,
                open_delimiter=evergreen["open_delimiter"],
                close_delimiter=evergreen["close_delimiter"],
            )
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

    def backup(self, destination: str) -> str:
        return str(self.database.backup(destination))

    def close(self) -> None:
        self.semantic.close()
