from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .database import Database
from .serialization import compact_json
from .timeutil import isoformat, parse_time, utc_now

if TYPE_CHECKING:
    from .semantic import SemanticIndex


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
    sha256: str


class MemoryStore:

    def __init__(self, database: Database, semantic: SemanticIndex | None = None):
        self.database = database
        self.semantic = semantic

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
        digest = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
        internal_conversation = self.ensure_conversation(harness, conversation_id, route, when)
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
                   (conversation_id, role, text, content_json, external_id,
                    occurred_at, ingested_at, sha256)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
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
        candidate_limit = limit
        if self.semantic and self.semantic.enabled:
            candidate_limit = max(limit, self.semantic.config.lexical_candidates)
        rows = self._lexical_candidates(
            query,
            candidate_limit + len(exclude_ids) + 8,
        )
        rows = [row for row in rows if int(row["id"]) not in exclude_ids][:candidate_limit]
        semantic_rows = (
            self.semantic.semantic_candidates(
                query,
                max(limit, self.semantic.config.semantic_candidates),
                exclude_ids,
            )
            if self.semantic and self.semantic.enabled
            else []
        )
        if semantic_rows:
            candidates = self._fuse_candidates(rows, semantic_rows, self.semantic.config.rrf_k)
        else:
            candidates = [
                {"id": int(row["id"]), "rank": float(row["rank"]), "retrieval": "lexical"}
                for row in rows
            ]

        results: list[dict[str, Any]] = []
        seen: set[int] = set()
        for candidate in candidates:
            hit_id = int(candidate["id"])
            if hit_id in exclude_ids or hit_id in seen:
                continue
            hit = self.get(hit_id)
            if not hit:
                continue
            context = self._surrounding(hit["conversation_id"], hit_id, context_messages)
            for item in context:
                seen.add(item["id"])
            result = {
                "hit_id": hit_id,
                "rank": float(candidate["rank"]),
                "messages": context,
            }
            if candidate.get("fusion_score") is not None:
                result["retrieval"] = {
                    "method": "hybrid",
                    "fusion_score": candidate["fusion_score"],
                    "lexical_rank": candidate.get("lexical_rank"),
                    "semantic_rank": candidate.get("semantic_rank"),
                    "semantic_similarity": candidate.get("semantic_similarity"),
                }
            results.append(result)
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

    @staticmethod
    def _fuse_candidates(
        lexical_rows: list[Any], semantic_rows: list[dict[str, Any]], rrf_k: int
    ) -> list[dict[str, Any]]:
        candidates: dict[int, dict[str, Any]] = {}
        for position, row in enumerate(lexical_rows, start=1):
            message_id = int(row["id"])
            item = candidates.setdefault(
                message_id,
                {"id": message_id, "fusion_score": 0.0, "rank": float(row["rank"])},
            )
            item["fusion_score"] += 1.0 / (rrf_k + position)
            item["lexical_rank"] = position
        for position, row in enumerate(semantic_rows, start=1):
            message_id = int(row["message_id"])
            item = candidates.setdefault(
                message_id,
                {"id": message_id, "fusion_score": 0.0, "rank": 0.0},
            )
            item["fusion_score"] += 1.0 / (rrf_k + position)
            item["semantic_rank"] = position
            item["semantic_similarity"] = float(row["similarity"])
        ordered = sorted(
            candidates.values(),
            key=lambda item: (
                -float(item["fusion_score"]),
                int(item.get("lexical_rank", 1_000_000)),
                int(item.get("semantic_rank", 1_000_000)),
                int(item["id"]),
            ),
        )
        for item in ordered:
            item["rank"] = -float(item["fusion_score"])
        return ordered

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

    def rebuild_index(self) -> None:
        with self.database.connect() as db:
            db.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["content"] = json.loads(result.pop("content_json"))
        result["external_id"] = result.get("external_id") or ""
        return result
