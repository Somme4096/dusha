from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .database import Database
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
    return json.dumps(content, ensure_ascii=False, separators=(",", ":"))


def _content_json(content: Any) -> str:
    return json.dumps(content, ensure_ascii=False, separators=(",", ":"))


@dataclass(slots=True)
class StoredMessage:
    id: int
    duplicate: bool
    conversation_id: int
    sha256: str


class MemoryStore:
    """Canonical raw message storage with a disposable FTS5 index."""

    def __init__(self, database: Database):
        self.database = database

    def ensure_companion(self, companion_id: str, now: datetime | None = None) -> None:
        timestamp = isoformat(now or utc_now())
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO companions(id, created_at, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at",
                (companion_id, timestamp, timestamp),
            )

    def ensure_conversation(
        self,
        companion_id: str,
        harness: str,
        external_id: str,
        route: str = "",
        now: datetime | None = None,
    ) -> int:
        timestamp = isoformat(now or utc_now())
        self.ensure_companion(companion_id, now)
        with self.database.connect() as db:
            db.execute(
                """INSERT INTO conversations
                   (companion_id, harness, external_id, route, created_at, updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(companion_id, harness, external_id) DO UPDATE SET
                     route=CASE WHEN excluded.route != '' THEN excluded.route ELSE conversations.route END,
                     updated_at=excluded.updated_at""",
                (companion_id, harness, external_id, route, timestamp, timestamp),
            )
            row = db.execute(
                "SELECT id FROM conversations WHERE companion_id=? AND harness=? AND external_id=?",
                (companion_id, harness, external_id),
            ).fetchone()
            return int(row["id"])

    def ingest(
        self,
        *,
        companion_id: str,
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
        digest = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
        internal_conversation = self.ensure_conversation(companion_id, harness, conversation_id, route, when)
        with self.database.connect() as db:
            if external_id:
                existing = db.execute(
                    "SELECT id, sha256 FROM messages WHERE conversation_id=? AND external_id=?",
                    (internal_conversation, external_id),
                ).fetchone()
                if existing:
                    return StoredMessage(
                        int(existing["id"]), True, internal_conversation, str(existing["sha256"])
                    )
            cursor = db.execute(
                """INSERT INTO messages
                   (companion_id, conversation_id, role, text, content_json, external_id,
                    occurred_at, ingested_at, sha256)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    companion_id,
                    internal_conversation,
                    role,
                    text,
                    raw_json,
                    external_id or None,
                    when_text,
                    ingested,
                    digest,
                ),
            )
            return StoredMessage(int(cursor.lastrowid), False, internal_conversation, digest)

    def get(self, message_id: int) -> dict[str, Any] | None:
        with self.database.connect() as db:
            row = db.execute(
                """SELECT m.*, c.harness, c.external_id AS external_conversation_id, c.route
                   FROM messages m JOIN conversations c ON c.id=m.conversation_id
                   WHERE m.id=?""",
                (message_id,),
            ).fetchone()
            return self._row(row) if row else None

    def context(self, companion_id: str, message_id: int, radius: int = 1) -> list[dict[str, Any]]:
        message = self.get(message_id)
        if not message or message["companion_id"] != companion_id:
            return []
        return self._surrounding(message["conversation_id"], message_id, max(0, min(radius, 10)))

    def recent(
        self,
        companion_id: str,
        conversation_id: int | None = None,
        limit: int = 10,
        exclude_ids: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        exclude_ids = exclude_ids or set()
        clause = "m.companion_id=?"
        args: list[Any] = [companion_id]
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
        companion_id: str,
        query: str,
        limit: int = 8,
        context_messages: int = 1,
        exclude_ids: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        exclude_ids = exclude_ids or set()
        terms = self._query_terms(query)
        rows: list[Any] = []
        with self.database.connect() as db:
            if terms:
                fts_query = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
                rows = db.execute(
                    """SELECT m.id, bm25(messages_fts) AS rank
                       FROM messages_fts JOIN messages m ON m.id=messages_fts.rowid
                       WHERE messages_fts MATCH ? AND m.companion_id=?
                       ORDER BY rank LIMIT ?""",
                    (fts_query, companion_id, limit + len(exclude_ids) + 8),
                ).fetchall()
            if not rows:
                token = (terms[0] if terms else query.strip())[:120]
                rows = db.execute(
                    "SELECT id, 0 AS rank FROM messages WHERE companion_id=? AND text LIKE ? "
                    "ORDER BY occurred_at DESC, id DESC LIMIT ?",
                    (companion_id, f"%{token}%", limit + len(exclude_ids) + 8),
                ).fetchall()

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
            results.append({"hit_id": hit_id, "rank": float(row["rank"]), "messages": context})
            if len(results) >= limit:
                break
        return results

    def _surrounding(self, conversation_id: int, hit_id: int, radius: int) -> list[dict[str, Any]]:
        with self.database.connect() as db:
            ids = [
                int(row["id"])
                for row in db.execute(
                    "SELECT id FROM messages WHERE conversation_id=? ORDER BY occurred_at, id",
                    (conversation_id,),
                ).fetchall()
            ]
        try:
            index = ids.index(hit_id)
        except ValueError:
            return []
        selected = ids[max(0, index - radius) : index + radius + 1]
        return [item for item in (self.get(message_id) for message_id in selected) if item]

    def rebuild_index(self) -> None:
        with self.database.connect() as db:
            db.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["content"] = json.loads(result.pop("content_json"))
        result["external_id"] = result.get("external_id") or ""
        return result
