from __future__ import annotations

import logging
import threading
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
from .memory_plugin import (
    BackfillIndexRequest,
    IngestMessagesRequest,
    InjectContextRequest,
    MatchPhraseRequest,
)
from .memory_provider import MemoryProvider
from .serialization import compact_json
from .timeutil import isoformat, parse_time, utc_now

logger = logging.getLogger("companion_gateway")

_PLUGIN_BATCH_MAX_BYTES = 512 * 1024
_PLUGIN_CONTEXT_MAX_RECORDS = 50
_PLUGIN_CATCH_UP_BATCHES = 20


class StorageUnavailableError(RuntimeError):
    pass


class CompanionService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.storage_enabled = bool(config.storage.enabled)
        self.database = Database(config.database_path)
        self.memory = MemoryProvider(config)
        self._memory_fallback = None
        self._evergreen_fallback = None
        if self.storage_enabled:
            self._memory_fallback = MemoryStore(self.database)
            self._evergreen_fallback = EvergreenStore(self.database)
        self.evergreen = self._evergreen_fallback
        self._ingest_lock = threading.Lock()
        self._plugin_name = str(getattr(config.memory_plugin, "module", "") or "").strip()
        self._plugin_batch_size = max(1, min(int(config.memory_plugin.ingest_batch_size), 500))
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
        self.phrase_matcher = (
            self._phrase_matcher if (self.memory.enabled and self.storage_enabled) else None
        )
        self.composer = ContextComposer(
            prompts=self.prompts,
            budget=config.memory.injection_max_chars,
            identity_text=self.identity_text,
            identity_configured=self.identity_configured,
            identity_revision=self.identity_revision,
            emotions_fingerprint=self.affect.emotions_fingerprint,
            prompts_fingerprint=self.prompts_fingerprint,
            plugin_context_max_chars=config.memory.plugin_context_max_chars,
        )

    def _require_storage(self) -> None:
        if not self.storage_enabled:
            raise StorageUnavailableError("built-in message storage is disabled")

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
        self._require_storage()
        message = self._memory_fallback.ingest(
            harness=harness,
            conversation_id=conversation_id,
            role=role,
            content=content,
            route=route,
            external_id=external_id,
            occurred_at=occurred_at,
            source_payload=source_payload,
        )
        affect = None
        if role == "user" and not message.duplicate:
            stored_message = self._memory_fallback.get(message.id)
            if stored_message is not None:
                message_time = parse_time(stored_message["occurred_at"])
                affect = self.affect.record_user_message(
                    message=stored_message["text"],
                    source_message_id=message.id,
                    decider=self.decision.evaluate if self.decision.enabled else None,
                    instruction=str(self.prompts["decision_instruction"]),
                    now=message_time,
                    phrase_matcher=self.phrase_matcher,
                )
        if not message.duplicate:
            self._push_plugin_batch()
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
        self._require_storage()
        excluded = exclude_message_ids or set()
        internal_conversation = None
        if harness and conversation_id:
            internal_conversation = self._memory_fallback.conversation_id(harness, conversation_id)

        recent = self._memory_fallback.recent(
            conversation_id=internal_conversation,
            limit=self.config.memory.recent_messages,
            exclude_ids=excluded,
        )
        recent_ids = {item["id"] for item in recent}
        hits = (
            self._memory_fallback.search(
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
            _, evergreen_facts = self._evergreen_fallback.render(
                self.config.evergreen.max_items,
                self.config.evergreen.max_chars,
                open_delimiter=evergreen["open_delimiter"],
                close_delimiter=evergreen["close_delimiter"],
            )
        plugin_context = self._plugin_context(query, harness, conversation_id)
        composed = self.composer.compose(
            affect_snapshot=affect_snapshot,
            affect_text=affect_text,
            evergreen_facts=evergreen_facts,
            session_records=records,
            plugin_context=plugin_context,
        )
        return {
            "injection": composed["injection"],
            "affect": affect_snapshot,
            "evergreen_facts": composed["evergreen_facts"],
            "records": composed["records"],
            "search_hits": hits,
            "context": composed["context"],
        }

    def _plugin_context(
        self, query: str, harness: str, conversation_id: str
    ) -> dict[str, Any] | None:
        if not (self.storage_enabled and self.memory.enabled):
            return None
        cap = int(self.config.memory.plugin_context_max_chars)
        if cap <= 0:
            return None
        try:
            result = self.memory.inject_context(
                InjectContextRequest(
                    query=query,
                    scope=conversation_id or harness or "",
                    max_chars=cap,
                    harness=harness,
                    conversation_id=conversation_id,
                )
            )
        except Exception:
            logger.warning("memory plugin context injection failed")
            return None
        if result is None:
            return None
        return self._normalize_plugin_context(result, cap)

    @staticmethod
    def _normalize_plugin_context(result: Any, cap: int) -> dict[str, Any] | None:
        text = result.text if isinstance(getattr(result, "text", None), str) else ""
        if len(text) > cap:
            text = text[:cap]
        records: list[dict[str, Any]] = []
        remaining = max(0, cap - len(text))
        for item in list(getattr(result, "records", None) or [])[:_PLUGIN_CONTEXT_MAX_RECORDS]:
            if not isinstance(item, dict):
                continue
            source = item.get("source")
            body = item.get("text")
            if not isinstance(source, str) or not isinstance(body, str):
                continue
            body = body[:remaining]
            if not body:
                break
            records.append({"source": source[:200], "text": body})
            remaining -= len(body)
        if not text and not records:
            return None
        return {"source": "memory_plugin", "text": text, "records": records}

    def _plugin_cursor(self, db: Any) -> int:
        row = db.execute(
            "SELECT last_acknowledged_id FROM plugin_ingest_state WHERE plugin=?",
            (self._plugin_name,),
        ).fetchone()
        return int(row["last_acknowledged_id"]) if row else 0

    def _plugin_batch(self, cursor: int) -> list[dict[str, Any]]:
        with self.database.connect() as db:
            rows = db.execute(
                """SELECT m.id, m.conversation_id, m.role, m.text, m.occurred_at,
                          m.ingested_at, m.sha256, c.harness,
                          c.external_id AS external_conversation_id
                   FROM messages m JOIN conversations c ON c.id=m.conversation_id
                   WHERE m.id > ? AND m.role IN ('user','assistant')
                     AND m.occurred_at > '1970-01-01T00:00:00+00:00'
                   ORDER BY m.id ASC LIMIT ?""",
                (cursor, self._plugin_batch_size),
            ).fetchall()
        payload: list[dict[str, Any]] = []
        for row in rows:
            candidate = payload + [dict(row)]
            if payload and len(compact_json(candidate)) > _PLUGIN_BATCH_MAX_BYTES:
                break
            payload.append(dict(row))
        return payload

    def _push_plugin_batch_locked(self) -> dict[str, Any]:
        if not (self.storage_enabled and self.memory.enabled and self._plugin_name):
            return {"skipped": True}
        with self.database.connect() as db:
            cursor = self._plugin_cursor(db)
        batch = self._plugin_batch(cursor)
        if not batch:
            return {"cursor": cursor, "pushed": 0, "acknowledged": False}
        try:
            result = self.memory.ingest_messages(IngestMessagesRequest(messages=tuple(batch)))
        except Exception:
            logger.warning("memory plugin message ingestion failed")
            return {"cursor": cursor, "pushed": len(batch), "acknowledged": False}
        highest = getattr(result, "highest_id", None)
        expected = int(batch[-1]["id"])
        if (
            isinstance(highest, bool)
            or not isinstance(highest, int)
            or highest != expected
            or highest <= cursor
        ):
            logger.warning("memory plugin returned an invalid ingestion acknowledgement")
            return {"cursor": cursor, "pushed": len(batch), "acknowledged": False}
        with self.database.connect() as db:
            db.execute(
                """INSERT INTO plugin_ingest_state(plugin, last_acknowledged_id, updated_at)
                   VALUES(?,?,?)
                   ON CONFLICT(plugin) DO UPDATE SET
                     last_acknowledged_id=excluded.last_acknowledged_id,
                     updated_at=excluded.updated_at""",
                (self._plugin_name, int(highest), isoformat(utc_now())),
            )
        return {"cursor": int(highest), "pushed": len(batch), "acknowledged": True}

    def _push_plugin_batch(self) -> dict[str, Any] | None:
        if not (self.storage_enabled and self.memory.enabled and self._plugin_name):
            return None
        with self._ingest_lock:
            return self._push_plugin_batch_locked()

    def catch_up_plugin(self) -> dict[str, Any]:
        if not (self.storage_enabled and self.memory.enabled and self._plugin_name):
            return {"skipped": True}
        pushed = 0
        cursor = 0
        acknowledged = False
        pending = False
        with self._ingest_lock:
            for _ in range(_PLUGIN_CATCH_UP_BATCHES):
                result = self._push_plugin_batch_locked()
                cursor = int(result.get("cursor", cursor))
                if result.get("acknowledged"):
                    pushed += int(result.get("pushed", 0))
                    acknowledged = True
                    continue
                pending = bool(result.get("pushed"))
                break
        if acknowledged:
            logger.info("memory plugin ingestion acknowledged through id %s", cursor)
        elif pending:
            logger.warning("memory plugin ingestion backlog pending")
        return {
            "cursor": cursor,
            "pushed": pushed,
            "acknowledged": acknowledged,
            "pending": pending,
        }

    def memory_index_status(self) -> dict[str, Any]:
        if self.memory.enabled:
            try:
                result = self.memory.status()
                if result is not None and isinstance(result.status, dict):
                    return result.status
            except Exception:
                logger.warning("memory plugin status unavailable")
        return self._core_index_status()

    def _core_index_status(self) -> dict[str, Any]:
        if not self.storage_enabled:
            return {
                "mode": "disabled",
                "enabled": False,
                "configured": False,
                "chunker_key": "",
                "embedding_key": "",
                "messages": 0,
                "chunked_messages": 0,
                "chunks": 0,
                "embedded_chunks": 0,
                "dimensions": [],
                "cooling_down": False,
                "last_error": "",
                "retrieval": "lexical",
                "storage_enabled": False,
                "plugin_configured": False,
                "plugin_enabled": False,
            }
        with self.database.connect() as db:
            messages = int(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        return {
            "mode": "lexical",
            "enabled": False,
            "configured": False,
            "chunker_key": "",
            "embedding_key": "",
            "messages": messages,
            "chunked_messages": 0,
            "chunks": 0,
            "embedded_chunks": 0,
            "dimensions": [],
            "cooling_down": False,
            "last_error": "",
            "retrieval": "lexical",
            "storage_enabled": True,
            "plugin_configured": bool(self._plugin_name),
            "plugin_enabled": self.memory.enabled,
        }

    def memory_index_backfill(self, limit: int | None = None, force: bool = False) -> dict[str, Any]:
        if self.memory.enabled:
            try:
                result = self.memory.backfill_once(BackfillIndexRequest(limit or 100, force))
                if result is not None and isinstance(result.status, dict):
                    return result.status
            except Exception:
                logger.warning("memory plugin backfill unavailable")
        return {"retrieval": "lexical", "enabled": False, "embedded": 0, "chunks_created": 0}

    def memory_index_rebuild(self) -> dict[str, Any]:
        if self.memory.enabled:
            try:
                result = self.memory.rebuild_chunks()
                if result is not None and isinstance(result.status, dict):
                    return result.status
            except Exception:
                logger.warning("memory plugin rebuild unavailable")
        if self._memory_fallback is not None:
            self._memory_fallback.rebuild_index()
        return {"status": "rebuilt", "retrieval": "lexical"}

    def memory_reindex(self) -> dict[str, Any]:
        if self.memory.enabled:
            try:
                self.memory.rebuild_index()
            except Exception:
                logger.warning("memory plugin reindex unavailable")
        if self._memory_fallback is not None:
            self._memory_fallback.rebuild_index()
        return {"status": "rebuilt"}

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

    def _phrase_matcher(self, message: str) -> dict[str, float] | None:
        result = self.memory.match_phrase(MatchPhraseRequest(message=message))
        return result.deltas if result is not None else None

    def backup(self, destination: str) -> str:
        return str(self.database.backup(destination))

    def close(self) -> None:
        self.memory.close()
