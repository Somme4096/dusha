from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from . import prompts as _prompts
from . import schema as _schema
from .commands import CommandError, arg, option
from .config import MemoryConfig
from .database import Database
from .hooks import Block, ContextRequest
from .interfaces import GatewayInput, call, errors
from .serialization import compact_json
from .timeutil import isoformat, parse_time, utc_now


def text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {"text", "input_text", "output_text"}:
                parts.append(str(item.get("text", "")))
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return compact_json(content)


def _content_json(content: Any) -> str:
    return compact_json(content)


@dataclass(slots=True)
class StoredMessage:
    id: int
    duplicate: bool
    conversation_id: int


class MemoryStore:

    def __init__(self, database: Database):
        self.database = database

    def ensure_conversation(
        self,
        harness: str,
        external_id: str,
        route: str = "",
        now: datetime | None = None,
    ) -> int:
        timestamp = isoformat(now or utc_now())
        with self.database.connect() as db:
            db.execute(
                """INSERT INTO conversations
                   (harness, external_id, route, created_at, updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(harness, external_id) DO UPDATE SET
                     route=CASE WHEN excluded.route != '' THEN excluded.route ELSE conversations.route END,
                     updated_at=excluded.updated_at""",
                (harness, external_id, route, timestamp, timestamp),
            )
            row = db.execute(
                "SELECT id FROM conversations WHERE harness=? AND external_id=?",
                (harness, external_id),
            ).fetchone()
            return int(row["id"])

    def ingest(
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
    ) -> StoredMessage:
        if role not in {"user", "assistant", "system", "tool", "event"}:
            raise ValueError(f"unsupported role: {role}")
        text = text_from_content(content)
        if not text.strip():
            raise ValueError("message content is empty")
        when = parse_time(occurred_at)
        when_text = isoformat(when)
        ingested = isoformat(utc_now())
        raw_json = _content_json(content if source_payload is None else source_payload)
        internal_conversation = self.ensure_conversation(harness, conversation_id, route, when)
        with self.database.connect() as db:
            if external_id:
                existing = db.execute(
                    "SELECT id FROM messages WHERE conversation_id=? AND external_id=?",
                    (internal_conversation, external_id),
                ).fetchone()
                if existing:
                    return StoredMessage(int(existing["id"]), True, internal_conversation)
            cursor = db.execute(
                """INSERT INTO messages
                   (conversation_id, role, text, content_json, external_id,
                    occurred_at, ingested_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    internal_conversation,
                    role,
                    text,
                    raw_json,
                    external_id or None,
                    when_text,
                    ingested,
                ),
            )
            return StoredMessage(int(cursor.lastrowid), False, internal_conversation)

    def get(self, message_id: int) -> dict[str, Any] | None:
        with self.database.connect() as db:
            row = db.execute(
                """SELECT m.*, c.harness, c.external_id AS external_conversation_id, c.route
                   FROM messages m JOIN conversations c ON c.id=m.conversation_id
                   WHERE m.id=?""",
                (message_id,),
            ).fetchone()
            return self._row(row) if row else None

    def conversation_id(self, harness: str, external_id: str) -> int | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT id FROM conversations WHERE harness=? AND external_id=?",
                (harness, external_id),
            ).fetchone()
            return int(row["id"]) if row else None

    def context(self, message_id: int, radius: int = 1) -> list[dict[str, Any]]:
        message = self.get(message_id)
        if not message:
            return []
        return self._surrounding(message["conversation_id"], message_id, max(0, min(radius, 10)))

    def recent(
        self,
        conversation_id: int | None = None,
        limit: int = 10,
        exclude_ids: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        exclude_ids = exclude_ids or set()
        clause = "1=1"
        args: list[Any] = []
        if conversation_id is not None:
            clause += " AND m.conversation_id=?"
            args.append(conversation_id)
        args.append(max(limit + len(exclude_ids), limit))
        with self.database.connect() as db:
            rows = db.execute(
                f"""SELECT m.*, c.harness, c.external_id AS external_conversation_id, c.route
                    FROM messages m JOIN conversations c ON c.id=m.conversation_id
                    WHERE {clause} ORDER BY m.occurred_at DESC, m.id DESC LIMIT ?""",
                args,
            ).fetchall()
        return [self._row(row) for row in rows if row["id"] not in exclude_ids][:limit][::-1]

    @staticmethod
    def _query_terms(query: str) -> list[str]:
        chunks = re.findall(r"[\w\u3040-\u30ff\u3400-\u9fff]+", query.casefold())
        terms: list[str] = []
        for chunk in chunks:
            if len(chunk) < 3:
                continue
            if re.fullmatch(r"[\u3040-\u30ff\u3400-\u9fff]+", chunk) and len(chunk) > 5:
                terms.extend(chunk[index : index + 3] for index in range(len(chunk) - 2))
            else:
                terms.append(chunk)
        return list(dict.fromkeys(terms))[:24]

    def search(
        self,
        query: str,
        limit: int = 8,
        context_messages: int = 1,
        exclude_ids: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        exclude_ids = exclude_ids or set()
        rows = self._lexical_candidates(query, limit + len(exclude_ids) + 8)
        rows = [row for row in rows if int(row["id"]) not in exclude_ids][:limit]

        results: list[dict[str, Any]] = []
        seen: set[int] = set()
        for row in rows:
            hit_id = int(row["id"])
            if hit_id in exclude_ids or hit_id in seen:
                continue
            hit = self.get(hit_id)
            if not hit:
                continue
            context = self._surrounding(hit["conversation_id"], hit_id, context_messages)
            for item in context:
                seen.add(item["id"])
            results.append(
                {
                    "hit_id": hit_id,
                    "rank": float(row["rank"]),
                    "messages": context,
                }
            )
            if len(results) >= limit:
                break
        return results

    def _lexical_candidates(self, query: str, limit: int) -> list[Any]:
        terms = self._query_terms(query)
        rows: list[Any] = []
        with self.database.connect() as db:
            if terms:
                fts_query = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
                rows = db.execute(
                    """SELECT m.id, bm25(messages_fts) AS rank
                       FROM messages_fts JOIN messages m ON m.id=messages_fts.rowid
                       WHERE messages_fts MATCH ?
                       ORDER BY rank LIMIT ?""",
                    (fts_query, limit),
                ).fetchall()
            if not rows:
                token = (terms[0] if terms else query.strip())[:120]
                rows = db.execute(
                    "SELECT id, 0 AS rank FROM messages WHERE text LIKE ? "
                    "ORDER BY occurred_at DESC, id DESC LIMIT ?",
                    (f"%{token}%", limit),
                ).fetchall()
        return list(rows)

    def _surrounding(self, conversation_id: int, hit_id: int, radius: int) -> list[dict[str, Any]]:
        radius = max(0, radius)
        select = """SELECT m.*, c.harness, c.external_id AS external_conversation_id, c.route
                    FROM messages m JOIN conversations c ON c.id=m.conversation_id"""
        with self.database.connect() as db:
            hit = db.execute(
                select + " WHERE m.conversation_id=? AND m.id=?",
                (conversation_id, hit_id),
            ).fetchone()
            if not hit:
                return []
            before = db.execute(
                select
                + """ WHERE m.conversation_id=? AND
                      (m.occurred_at < ? OR (m.occurred_at=? AND m.id < ?))
                    ORDER BY m.occurred_at DESC, m.id DESC LIMIT ?""",
                (conversation_id, hit["occurred_at"], hit["occurred_at"], hit_id, radius),
            ).fetchall()
            after = db.execute(
                select
                + """ WHERE m.conversation_id=? AND
                      (m.occurred_at > ? OR (m.occurred_at=? AND m.id > ?))
                    ORDER BY m.occurred_at, m.id LIMIT ?""",
                (conversation_id, hit["occurred_at"], hit["occurred_at"], hit_id, radius),
            ).fetchall()
        return [self._row(row) for row in [*reversed(before), hit, *after]]

    def after(self, cursor: int, limit: int) -> list[dict[str, Any]]:
        with self.database.connect() as db:
            rows = db.execute(
                """SELECT m.*, c.harness, c.external_id AS external_conversation_id, c.route
                   FROM messages m JOIN conversations c ON c.id=m.conversation_id
                   WHERE m.id > ? ORDER BY m.id LIMIT ?""",
                (cursor, max(1, limit)),
            ).fetchall()
        return [self._row(row) for row in rows]

    def rebuild_index(self) -> None:
        with self.database.connect() as db:
            db.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["content"] = json.loads(result.pop("content_json"))
        result["external_id"] = result.get("external_id") or ""
        return result


name = "memory"
inject = ("messages", "prompts", "api", "cli", "mcp")


class SearchInput(GatewayInput):
    query: str
    limit: int = 8
    context_messages: int = 1


def _record(message: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "memory_id": message["id"],
        "source": source,
        "time": message["occurred_at"],
        "role": message["role"],
        "text": message["text"],
    }


def recall(
    store: MemoryStore, config: MemoryConfig, request: ContextRequest, instruction: str
) -> Block | None:
    excluded = set(request.exclude_message_ids)
    internal = None
    if request.harness and request.conversation_id:
        internal = store.conversation_id(request.harness, request.conversation_id)
    recent = store.recent(conversation_id=internal, limit=config.recent_messages, exclude_ids=excluded)
    recent_ids = {item["id"] for item in recent}
    hits = (
        store.search(request.query, config.search_hits, config.context_messages, excluded | recent_ids)
        if request.query.strip()
        else []
    )
    records: list[dict[str, Any]] = []
    seen: set[int] = set()
    if request.include_recent:
        for message in recent:
            records.append(_record(message, "recent"))
            seen.add(message["id"])
    else:
        seen.update(recent_ids)
    for hit in hits:
        for message in hit["messages"]:
            if message["id"] not in seen and message["id"] not in excluded:
                records.append(_record(message, "recalled"))
                seen.add(message["id"])
    if not records and not hits and not instruction:
        return None
    return Block(name, "", {"search_hits": hits}, tuple(records), instruction)


def apply(ctx: Any, config: MemoryConfig) -> None:
    store: MemoryStore = ctx.messages
    text = _prompts.section(ctx.prompts, name, {"instruction": str})
    ctx.on("context", lambda request: recall(store, config, request, text["instruction"]))
    _routes(ctx.api, store)
    _commands(ctx.cli, store)
    _tools(ctx.mcp, store)


def _routes(router: APIRouter, store: MemoryStore) -> None:
    @router.post(
        "/state/v1/memory/search",
        tags=["state"],
        response_model=_schema.SearchResponse,
        summary="Search archived memory",
        responses=errors(503),
    )
    async def search_memory(body: SearchInput) -> dict[str, Any]:
        results = await call(
            store.search,
            body.query,
            max(1, min(body.limit, 50)),
            max(0, min(body.context_messages, 10)),
        )
        return {"results": results}

    @router.get(
        "/state/v1/memory/{message_id}",
        tags=["state"],
        response_model=_schema.MemoryContextResponse,
        responses=errors(404, 503),
    )
    async def memory_context(
        message_id: int, context_messages: int = Query(1, ge=0, le=10)
    ) -> dict[str, Any]:
        messages = await call(store.context, message_id, context_messages)
        if not messages:
            raise HTTPException(status_code=404, detail="memory record not found")
        return {"messages": messages}


def _commands(cli: Any, store: MemoryStore) -> None:
    memory = cli.group("memory", "Inspect canonical memory")
    memory.command(
        "search",
        lambda query, limit: {"results": store.search(query, limit)},
        help="Search the archive",
        args=[arg("query"), option("limit", int, default=8)],
    )
    memory.command(
        "show",
        lambda message_id: _show(store, message_id),
        help="Show one message",
        args=[arg("message_id", int)],
    )
    memory.command(
        "recent",
        lambda limit: {"messages": store.recent(limit=limit)},
        help="Show the latest messages",
        args=[option("limit", int, default=20)],
    )
    memory.command("reindex", lambda: _reindex(store), help="Rebuild the search index")


def _tools(mcp: Any, store: MemoryStore) -> None:
    mcp.tool(
        "memory_search",
        lambda query, limit, context_messages: {
            "results": store.search(query, max(1, min(limit, 50)), max(0, min(context_messages, 10)))
        },
        help="Search the companion's conversation archive for past exchanges.",
        args=[
            arg("query", help="Search text."),
            option("limit", int, default=8, help="Maximum number of hits."),
            option("context_messages", int, default=1, help="Neighbor messages to include with each hit."),
        ],
    )
    mcp.tool(
        "memory_get",
        lambda message_id, context_messages: _context(store, message_id, context_messages),
        help="Read one archived message with its neighbors.",
        args=[
            arg("message_id", int, help="Message id from a search hit."),
            option("context_messages", int, default=1, help="Neighbor messages on each side."),
        ],
        not_found="memory record not found",
    )


def _context(store: MemoryStore, message_id: int, context_messages: int) -> dict[str, Any]:
    messages = store.context(message_id, max(0, min(context_messages, 10)))
    if not messages:
        raise KeyError(message_id)
    return {"messages": messages}


def _show(store: MemoryStore, message_id: int) -> dict[str, Any]:
    message = store.get(message_id)
    if message is None:
        raise CommandError("message not found")
    return message


def _reindex(store: MemoryStore) -> dict[str, Any]:
    store.rebuild_index()
    return {"status": "rebuilt"}
