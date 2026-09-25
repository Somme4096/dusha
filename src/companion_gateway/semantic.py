from __future__ import annotations

import logging
import math
import os
import threading
import time
from datetime import datetime
from typing import Any

import httpx

from .config import EmbeddingConfig, MemoryConfig
from .database import Database
from .serialization import short_fingerprint
from .timeutil import isoformat, utc_now

logger = logging.getLogger("companion_gateway.semantic")


class OpenAIEmbeddingClient:
    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self._client: httpx.Client | None = None
        self._lock = threading.Lock()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self._get_client().post(
            self.config.base_url.rstrip("/") + "/embeddings",
            headers=self._headers(),
            json=self._payload(texts),
        )
        response.raise_for_status()
        body = response.json()
        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise ValueError("embedding endpoint returned an unexpected item count")
        if not all(isinstance(item, dict) for item in data):
            raise ValueError("embedding endpoint returned an invalid data item")
        indices = [int(item.get("index", -1)) for item in data]
        if sorted(indices) != list(range(len(texts))):
            raise ValueError("embedding endpoint returned invalid item indices")
        ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
        vectors = [self._normalize(item.get("embedding")) for item in ordered]
        dimensions = {len(vector) for vector in vectors}
        if len(dimensions) != 1:
            raise ValueError("embedding endpoint returned inconsistent dimensions")
        return vectors

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def _get_client(self) -> httpx.Client:
        with self._lock:
            if self._client is None:
                self._client = httpx.Client(timeout=self.config.timeout_seconds)
            return self._client

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        key = os.getenv(self.config.api_key_env, "") if self.config.api_key_env else ""
        if key:
            headers["authorization"] = f"Bearer {key}"
        return headers

    def _payload(self, texts: list[str]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "input": texts,
            "encoding_format": "float",
        }
        if self.config.dimensions is not None:
            payload["dimensions"] = self.config.dimensions
        return payload

    @staticmethod
    def _normalize(value: Any) -> list[float]:
        if not isinstance(value, list) or not value:
            raise ValueError("embedding endpoint returned an invalid vector")
        vector = [float(item) for item in value]
        if not all(math.isfinite(item) for item in vector):
            raise ValueError("embedding endpoint returned a non-finite vector")
        norm = math.sqrt(math.fsum(item * item for item in vector))
        if norm == 0:
            raise ValueError("embedding endpoint returned a zero vector")
        return [item / norm for item in vector]


class SQLiteVectorAdapter:
    """Isolates the pre-v1 sqlite-vec API from the retrieval code."""

    @staticmethod
    def prepare(db: Any) -> Any:
        import sqlite_vec

        db.enable_load_extension(True)
        try:
            sqlite_vec.load(db)
        finally:
            db.enable_load_extension(False)
        return sqlite_vec

    @classmethod
    def serialize(cls, vector: list[float]) -> bytes:
        sqlite_vec = __import__("sqlite_vec")
        return bytes(sqlite_vec.serialize_float32(vector))


class SemanticIndex:
    """Disposable child chunks and vectors backed by canonical message rows."""

    def __init__(self, database: Database, config: MemoryConfig):
        self.database = database
        self.config = config
        self.embedding = config.embedding
        self.mode = config.retrieval_mode.strip().casefold()
        self._validate_config()
        self.chunker_key = self._fingerprint(
            {"version": 1, "chars": config.child_chars, "overlap": config.child_overlap_chars}
        )
        self.embedding_key = self._fingerprint(
            {
                "format_version": 1,
                "base_url": self.embedding.base_url.rstrip("/"),
                "model": self.embedding.model,
                "dimensions": self.embedding.dimensions,
                "chunker_key": self.chunker_key,
            }
        )
        self.client = OpenAIEmbeddingClient(self.embedding)
        self._failure_lock = threading.Lock()
        self._failure_until = 0.0
        self._last_error = ""

    @property
    def enabled(self) -> bool:
        return self.mode == "hybrid" and bool(self.embedding.base_url and self.embedding.model)

    def close(self) -> None:
        self.client.close()

    def ensure_message_chunks(self, message_id: int, now: datetime | None = None) -> int:
        timestamp = isoformat(now or utc_now())
        with self.database.connect() as db:
            row = db.execute("SELECT id, text FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row:
                return 0
            return self._insert_chunks(db, int(row["id"]), str(row["text"]), timestamp)

    def ensure_chunks(self, limit: int | None = 256, now: datetime | None = None) -> dict[str, int]:
        timestamp = isoformat(now or utc_now())
        limit_sql = "" if limit is None else " LIMIT ?"
        args: list[Any] = [self.chunker_key]
        if limit is not None:
            args.append(max(1, limit))
        with self.database.connect() as db:
            rows = db.execute(
                """SELECT m.id, m.text FROM messages m
                   WHERE NOT EXISTS (
                     SELECT 1 FROM memory_chunks c
                     WHERE c.message_id=m.id AND c.chunker_key=?
                   )
                   ORDER BY m.id"""
                + limit_sql,
                args,
            ).fetchall()
            chunks = sum(
                self._insert_chunks(db, int(row["id"]), str(row["text"]), timestamp)
                for row in rows
            )
        return {"messages": len(rows), "chunks": chunks}

    def backfill_once(self, limit: int | None = None, force: bool = False) -> dict[str, Any]:
        batch_limit = min(2_048, max(1, limit or self.embedding.batch_size))
        if not self.enabled:
            return {"enabled": False, "embedded": 0, "chunks_created": 0}
        if self._cooling_down() and not force:
            return {
                "enabled": True,
                "embedded": 0,
                "chunks_created": 0,
                "cooling_down": True,
                "error": self.last_error,
            }
        created = self.ensure_chunks(batch_limit)
        with self.database.connect() as db:
            rows = db.execute(
                """SELECT c.id, c.start_char, c.end_char, m.text
                   FROM memory_chunks c
                   JOIN messages m ON m.id=c.message_id
                   WHERE c.chunker_key=? AND NOT EXISTS (
                     SELECT 1 FROM memory_chunk_embeddings e
                     WHERE e.chunk_id=c.id AND e.embedding_key=?
                   )
                   ORDER BY c.id LIMIT ?""",
                (self.chunker_key, self.embedding_key, batch_limit),
            ).fetchall()
        if not rows:
            self._clear_error()
            return {
                "enabled": True,
                "embedded": 0,
                "chunks_created": created["chunks"],
                "complete": True,
            }
        texts = [str(row["text"])[int(row["start_char"]) : int(row["end_char"])] for row in rows]
        try:
            vectors = self.client.embed(texts)
            dimensions = len(vectors[0])
            if self.embedding.dimensions is not None and dimensions != self.embedding.dimensions:
                raise ValueError(
                    f"embedding endpoint returned {dimensions} dimensions. "
                    f"Configuration requires {self.embedding.dimensions}"
                )
            timestamp = isoformat(utc_now())
            with self.database.connect() as db:
                db.executemany(
                    """INSERT OR IGNORE INTO memory_chunk_embeddings
                       (chunk_id, embedding_key, dimensions, embedding, created_at)
                       VALUES(?,?,?,?,?)""",
                    [
                        (
                            int(row["id"]),
                            self.embedding_key,
                            dimensions,
                            SQLiteVectorAdapter.serialize(vector),
                            timestamp,
                        )
                        for row, vector in zip(rows, vectors, strict=True)
                    ],
                )
            self._clear_error()
            return {
                "enabled": True,
                "embedded": len(vectors),
                "chunks_created": created["chunks"],
                "dimensions": dimensions,
                "complete": len(rows) < batch_limit,
            }
        except Exception as error:
            self._record_error(error)
            return {
                "enabled": True,
                "embedded": 0,
                "chunks_created": created["chunks"],
                "error": str(error),
            }

    def semantic_candidates(
        self,
        query: str,
        limit: int,
        exclude_ids: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        if not self.enabled or not query.strip() or self._cooling_down():
            return []
        dimensions = self._indexed_dimensions()
        if not dimensions:
            return []
        try:
            query_vector = self.client.embed([query])[0]
            query_dimensions = len(query_vector)
            if query_dimensions not in dimensions:
                self._discard_embedding_key()
                raise ValueError("embedding dimensions changed. The semantic index will rebuild")
            query_blob = SQLiteVectorAdapter.serialize(query_vector)
            excluded = sorted(exclude_ids or set())
            excluded_clause = ""
            if excluded:
                placeholders = ",".join("?" for _ in excluded)
                excluded_clause = f" AND c.message_id NOT IN ({placeholders})"
            args: list[Any] = [
                query_blob,
                self.embedding_key,
                query_dimensions,
                self.chunker_key,
                *excluded,
                1.0 - self.config.semantic_min_similarity,
                max(1, limit),
            ]
            with self.database.connect() as db:
                SQLiteVectorAdapter.prepare(db)
                rows = db.execute(
                    """WITH parent_distances AS (
                         SELECT c.message_id,
                                MIN(vec_distance_cosine(e.embedding, ?)) AS distance
                         FROM memory_chunk_embeddings e
                         JOIN memory_chunks c ON c.id=e.chunk_id
                         WHERE e.embedding_key=? AND e.dimensions=?
                           AND c.chunker_key=?"""
                    + excluded_clause
                    + """
                         GROUP BY c.message_id
                       )
                       SELECT message_id, distance FROM parent_distances
                       WHERE distance <= ?
                       ORDER BY distance, message_id LIMIT ?""",
                    args,
                ).fetchall()
            self._clear_error()
            return [
                {
                    "message_id": int(row["message_id"]),
                    "distance": float(row["distance"]),
                    "similarity": 1.0 - float(row["distance"]),
                }
                for row in rows
            ]
        except Exception as error:
            self._record_error(error)
            logger.warning("semantic search unavailable: %s", error)
            return []

    def status(self) -> dict[str, Any]:
        with self.database.connect() as db:
            message_count = int(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
            chunked_messages = int(
                db.execute(
                    "SELECT COUNT(DISTINCT message_id) FROM memory_chunks WHERE chunker_key=?",
                    (self.chunker_key,),
                ).fetchone()[0]
            )
            chunk_count = int(
                db.execute(
                    "SELECT COUNT(*) FROM memory_chunks WHERE chunker_key=?",
                    (self.chunker_key,),
                ).fetchone()[0]
            )
            embedded_count = int(
                db.execute(
                    "SELECT COUNT(*) FROM memory_chunk_embeddings WHERE embedding_key=?",
                    (self.embedding_key,),
                ).fetchone()[0]
            )
            dimensions = [
                int(row["dimensions"])
                for row in db.execute(
                    """SELECT DISTINCT dimensions FROM memory_chunk_embeddings
                       WHERE embedding_key=? ORDER BY dimensions""",
                    (self.embedding_key,),
                ).fetchall()
            ]
        return {
            "mode": self.mode,
            "enabled": self.enabled,
            "configured": bool(self.embedding.base_url and self.embedding.model),
            "chunker_key": self.chunker_key,
            "embedding_key": self.embedding_key,
            "messages": message_count,
            "chunked_messages": chunked_messages,
            "chunks": chunk_count,
            "embedded_chunks": embedded_count,
            "dimensions": dimensions,
            "cooling_down": self._cooling_down(),
            "last_error": self.last_error,
        }

    def rebuild_chunks(self) -> dict[str, Any]:
        with self.database.connect() as db:
            db.execute("DELETE FROM memory_chunks")
        created = self.ensure_chunks(limit=None)
        return {"status": "rebuilt", **created, "embedded": 0}

    @property
    def last_error(self) -> str:
        with self._failure_lock:
            return self._last_error

    def _indexed_dimensions(self) -> set[int]:
        with self.database.connect() as db:
            return {
                int(row["dimensions"])
                for row in db.execute(
                    """SELECT DISTINCT dimensions FROM memory_chunk_embeddings
                       WHERE embedding_key=?""",
                    (self.embedding_key,),
                ).fetchall()
            }

    def _discard_embedding_key(self) -> None:
        with self.database.connect() as db:
            db.execute(
                "DELETE FROM memory_chunk_embeddings WHERE embedding_key=?",
                (self.embedding_key,),
            )

    def _insert_chunks(self, db: Any, message_id: int, text: str, timestamp: str) -> int:
        offsets = self.chunk_offsets(text, self.config.child_chars, self.config.child_overlap_chars)
        before = db.total_changes
        db.executemany(
            """INSERT OR IGNORE INTO memory_chunks
               (message_id, chunker_key, ordinal, start_char, end_char, created_at)
               VALUES(?,?,?,?,?,?)""",
            [
                (message_id, self.chunker_key, ordinal, start, end, timestamp)
                for ordinal, (start, end) in enumerate(offsets)
            ],
        )
        return db.total_changes - before

    @staticmethod
    def chunk_offsets(text: str, size: int, overlap: int) -> list[tuple[int, int]]:
        offsets: list[tuple[int, int]] = []
        start = 0
        while start < len(text):
            end = min(len(text), start + size)
            offsets.append((start, end))
            if end == len(text):
                break
            start = end - overlap
        return offsets

    def _record_error(self, error: Exception) -> None:
        with self._failure_lock:
            self._last_error = str(error)[:500]
            self._failure_until = time.monotonic() + self.embedding.failure_cooldown_seconds

    def _clear_error(self) -> None:
        with self._failure_lock:
            self._last_error = ""
            self._failure_until = 0.0

    def _cooling_down(self) -> bool:
        with self._failure_lock:
            return time.monotonic() < self._failure_until

    def _validate_config(self) -> None:
        if self.mode not in {"lexical", "hybrid"}:
            raise ValueError("memory.retrieval_mode must be lexical or hybrid")
        if self.config.child_chars < 64:
            raise ValueError("memory.child_chars must be at least 64")
        if not 0 <= self.config.child_overlap_chars < self.config.child_chars:
            raise ValueError("memory.child_overlap_chars must be smaller than child_chars")
        if self.config.lexical_candidates < 1 or self.config.semantic_candidates < 1:
            raise ValueError("memory candidate limits must be positive")
        if self.config.rrf_k < 1:
            raise ValueError("memory.rrf_k must be positive")
        if not -1.0 <= self.config.semantic_min_similarity <= 1.0:
            raise ValueError("memory.semantic_min_similarity must be between -1 and 1")
        if self.embedding.dimensions is not None and self.embedding.dimensions < 1:
            raise ValueError("memory.embedding.dimensions must be positive")
        if self.embedding.batch_size < 1:
            raise ValueError("memory.embedding.batch_size must be positive")
        if self.embedding.timeout_seconds <= 0:
            raise ValueError("memory.embedding.timeout_seconds must be positive")
        if self.embedding.backfill_interval_seconds < 1:
            raise ValueError("memory.embedding.backfill_interval_seconds must be positive")
        if self.embedding.failure_cooldown_seconds < 0:
            raise ValueError("memory.embedding.failure_cooldown_seconds cannot be negative")

    @staticmethod
    def _fingerprint(value: dict[str, Any]) -> str:
        # Legacy json.dumps default (ensure_ascii=True) must be preserved so
        # existing index keys are stable for every input, including non-ASCII.
        return short_fingerprint(value, ensure_ascii=True)
