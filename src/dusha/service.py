from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from . import identity as _identity
from . import mods as _mods
from . import prompts as _prompts
from .affect import AffectEngine
from .config import AppConfig
from .context import ContextComposer
from .database import Database
from .evergreen import EvergreenStore
from .hooks import ContextRequest, Contributor, Registry
from .memo import MemoStore
from .memory import MemoryStore
from .proactive import ProactiveEngine

logger = logging.getLogger("dusha")


class FeatureDisabledError(RuntimeError):
    def __init__(self, name: str):
        super().__init__(f"the {name} feature is disabled")


class CompanionService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.features = config.features
        self.database = Database(config.database_path)
        self.prompts = _prompts.resolve_prompts(config.prompts)
        self.identity_text, self.identity_revision = "", ""
        if self.features.identity:
            self.identity_text, self.identity_revision = _identity.load_identity(
                config.identity_prompt
            )
        self.identity_configured = bool(self.features.identity and config.identity_prompt.path)
        self.composer = ContextComposer(
            prompts=self.prompts,
            identity_text=self.identity_text,
            identity_configured=self.identity_configured,
            identity_revision=self.identity_revision,
        )
        self.messages = MemoryStore(self.database)
        self.evergreen = EvergreenStore(self.database, config.evergreen, self.prompts)
        self.memo = MemoStore(self.database, config.memo, self.prompts)
        self.affect = AffectEngine(self.database, config, self.prompts) if self.features.affect else None
        self.proactive = ProactiveEngine(self, config)
        mods_dir = _mods.resolve_mods_dir(config)
        self.mods = [
            _mods.Mod(name, settings, mods_dir, self.database, self.prompts)
            for name, settings in config.mods.modules.items()
        ]
        self.hooks = Registry()
        if self.features.evergreen:
            self.hooks.add(self.evergreen)
        for mod in self.mods:
            self.hooks.add(mod)
        for enabled, contributor in (
            (self.features.memo, self.memo),
            (self.features.affect, self.affect),
            (self.features.proactive, self.proactive),
        ):
            if enabled:
                self.hooks.add(contributor)
        if self.affect is not None:
            self.affect.registry = self.hooks
        self.hooks.start()

    def register(self, contributor: Contributor) -> None:
        self.hooks.add(contributor)

    def require(self, name: str) -> None:
        if not getattr(self.features, name):
            raise FeatureDisabledError(name)

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
        message = self.messages.ingest(
            harness=harness,
            conversation_id=conversation_id,
            role=role,
            content=content,
            route=route,
            external_id=external_id,
            occurred_at=occurred_at,
            source_payload=source_payload,
        )
        results: dict[str, Any] = {}
        if not message.duplicate:
            stored = self.messages.get(message.id)
            if stored is not None:
                results = self.hooks.dispatch_message(stored)
        return {
            "id": message.id,
            "duplicate": message.duplicate,
            "conversation_id": message.conversation_id,
            "sha256": message.sha256,
            "affect": results.get("affect"),
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
            internal_conversation = self.messages.conversation_id(harness, conversation_id)

        recent: list[dict[str, Any]] = []
        if self.features.memory:
            recent = self.messages.recent(
                conversation_id=internal_conversation,
                limit=self.config.memory.recent_messages,
                exclude_ids=excluded,
            )
        recent_ids = {item["id"] for item in recent}
        hits = (
            self.messages.search(
                query,
                self.config.memory.search_hits,
                self.config.memory.context_messages,
                excluded | recent_ids,
            )
            if self.features.memory and query.strip()
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

        blocks = self.hooks.dispatch_context(
            ContextRequest(query=query, harness=harness, conversation_id=conversation_id)
        )
        composed = self.composer.compose(session_records=records, blocks=blocks)
        return {
            "injection": composed["injection"],
            "records": composed["records"],
            "search_hits": hits,
            "context": composed["context"],
        }

    def mods_status(self) -> dict[str, Any]:
        return {mod.name: mod.status() for mod in self.mods}

    def tick_mods(self) -> dict[str, Any]:
        return {mod.name: mod.tick() for mod in self.mods}

    def memory_reindex(self) -> dict[str, Any]:
        self.messages.rebuild_index()
        return {"status": "rebuilt"}

    def memo_add(self, text: str, now: datetime | None = None) -> dict[str, Any]:
        self.require("memo")
        return self.memo.add(text, now)

    def memo_list(self, status: str = "active", limit: int = 20) -> list[dict[str, Any]]:
        self.require("memo")
        return self.memo.list(status, limit)

    def memo_done(self, note_id: int, reason: str, now: datetime | None = None) -> dict[str, Any]:
        self.require("memo")
        return self.memo.done(note_id, reason, now)

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
        self.hooks.close()
