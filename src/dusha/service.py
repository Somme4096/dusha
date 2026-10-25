from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException

from . import affect, evergreen, identity, memo, memory, proactive
from . import external as _external
from . import prompts as _prompts
from .config import AppConfig
from .context import ContextComposer
from .database import Database
from .hooks import ContextRequest, Registry
from .interfaces import CommandError, Commands
from .memory import MemoryStore, text_from_content
from .transcript import ROLES, TranscriptTree, transcript_key

logger = logging.getLogger("dusha")

BUILTINS = {plugin.name: plugin for plugin in (identity, memory, evergreen, memo, affect, proactive)}


class Disabled:
    def __init__(self, name: str):
        self.name = name

    def apply(self, ctx: Any, config: Any) -> None:
        return None


class CompanionService:
    def __init__(self, config: AppConfig, commands: Commands | None = None):
        self.config = config
        self.database = Database(config.database_path)
        self.prompts = _prompts.resolve_prompts(config.prompts)
        self.messages = MemoryStore(self.database)
        self.transcripts = TranscriptTree(self.database)
        self._transcript_lock = threading.Lock()
        self.composer = ContextComposer(prompts=self.prompts)
        self.router = APIRouter()
        self.commands = commands or Commands()
        self.registry = Registry(
            {
                "settings": config,
                "database": self.database,
                "prompts": self.prompts,
                "messages": self.messages,
                "companion": self,
                "api": self.router,
                "cli": self.commands,
            },
            config.data_dir / "plugins",
        )
        plugins_dir = _external.resolve_plugins_dir(config)
        for name, entry in config.plugins.items():
            if entry.disabled:
                self.registry.plugin(Disabled(name), entry.config).state = "disabled"
            elif name in BUILTINS:
                self.registry.plugin(BUILTINS[name], entry.config)
            else:
                self.registry.plugin(_external.load(name, plugins_dir), entry.config.options)
        self.registry.start()
        for runtime in self.registry.runtimes:
            if runtime.state in {"disabled", "inactive"}:
                self._placeholder(runtime.name, runtime.state, runtime.error)

    def _placeholder(self, name: str, state: str, error: str) -> None:
        detail = f"the {name} plugin is {state}" + (f", {error}" if error else "")

        async def unavailable() -> None:
            raise HTTPException(status_code=503, detail=detail)

        for path in (f"/state/v1/{name}", f"/state/v1/{name}/{{rest:path}}"):
            self.router.add_api_route(
                path, unavailable, methods=["GET", "POST"], include_in_schema=False
            )

        def refuse(args: Any) -> None:
            raise CommandError(detail)

        parser = self.commands.command(name, f"Unavailable, {detail}")
        parser.add_argument("rest", nargs="*")
        parser.set_defaults(run=refuse)

    def get(self, name: str) -> Any:
        return self.registry.get(name)

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
                results = self.registry.collect("message", stored)
        return {
            **results,
            "id": message.id,
            "duplicate": message.duplicate,
            "conversation_id": message.conversation_id,
            "sha256": message.sha256,
        }

    def ingest_transcript(
        self,
        *,
        harness: str,
        messages: list[Any],
        conversation_id: str = "",
        route: str = "",
    ) -> dict[str, Any]:
        turns = [item for item in messages if isinstance(item, dict) and item.get("role") in ROLES]
        conversation = conversation_id
        parent: int | None = None
        user: int | None = None
        with self._transcript_lock:
            for message in turns:
                key = transcript_key(message)
                found = None
                if parent is not None:
                    found = self.transcripts.child(parent, key)
                elif root := self.transcripts.root(harness, key, conversation_id):
                    found, conversation = root
                if found is None:
                    conversation = conversation or f"auto-{uuid.uuid4().hex[:12]}"
                    content = message.get("content")
                    result = self.ingest_message(
                        harness=harness,
                        conversation_id=conversation,
                        role=message["role"],
                        content=content if text_from_content(content).strip() else message,
                        route=route,
                        source_payload=message,
                    )
                    found = result["id"]
                    self.transcripts.add(found, parent, result["conversation_id"], key)
                parent = found
                if message["role"] == "user":
                    user = found
        stored = self.messages.get(user) if user is not None else None
        return {
            "conversation_id": conversation,
            "message_id": user,
            "query": stored["text"] if stored else "",
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
        request = ContextRequest(
            query=query,
            harness=harness,
            conversation_id=conversation_id,
            exclude_message_ids=tuple(sorted(exclude_message_ids or ())),
            include_recent=include_recent,
        )
        composed = self.composer.compose(list(self.registry.collect("context", request).values()))
        hits: list[Any] = []
        for block in composed["context"]["blocks"].values():
            if isinstance(block, dict) and isinstance(block.get("search_hits"), list):
                hits.extend(block["search_hits"])
        return {**composed, "search_hits": hits}

    def latest_route(self) -> dict[str, Any] | None:
        with self.database.connect() as db:
            row = db.execute(
                """SELECT id, harness, external_id, route
                   FROM conversations
                   WHERE route != ''
                   ORDER BY updated_at DESC, id DESC LIMIT 1""",
            ).fetchone()
        return dict(row) if row else None

    def plugins_status(self) -> dict[str, Any]:
        return self.registry.status()

    def tick(self) -> None:
        self.registry.run_jobs()

    def backup(self, destination: str) -> str:
        return str(self.database.backup(destination))

    def close(self) -> None:
        self.registry.close()
