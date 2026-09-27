from __future__ import annotations

import re
import math
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from typing import Any

import httpx


_SAFE = re.compile(r"^[a-zA-Z0-9_.@+-]+$")
_OPTIONS = {
    "url", "app_id", "project_id", "instance_namespace", "user_sender_id",
    "assistant_sender_id", "timeout_seconds", "flush_after_ingest",
}
_RPC_TIMEOUT_ENV = "SOPHIA_MEMORY_RPC_TIMEOUT_SECONDS"
_MIN_RPC_TIMEOUT = 0.5


class EverOSMirror:
    def __init__(self, database: Any, options: dict[str, Any]):
        unknown = set(options) - _OPTIONS
        if unknown:
            raise ValueError("unknown EverOS option")
        self.database = database
        self.url = self._url(options.get("url", "http://127.0.0.1:8000"))
        self.app_id = self._id(options.get("app_id", "default"))
        self.project_id = self._id(options.get("project_id", "default"))
        if "instance_namespace" not in options:
            raise ValueError("instance_namespace is required")
        if "user_sender_id" not in options or "assistant_sender_id" not in options:
            raise ValueError("user_sender_id and assistant_sender_id are required")
        self.instance_namespace = self._id(options["instance_namespace"])
        self.user_sender_id = self._id(options["user_sender_id"])
        self.assistant_sender_id = self._id(options["assistant_sender_id"])
        if self.user_sender_id == self.assistant_sender_id:
            raise ValueError("user_sender_id and assistant_sender_id must differ")
        rpc_timeout_text = os.environ.get(_RPC_TIMEOUT_ENV)
        if rpc_timeout_text is None:
            raise ValueError("memory plugin RPC timeout is unavailable")
        try:
            rpc_timeout = float(rpc_timeout_text)
        except (TypeError, ValueError) as error:
            raise ValueError("memory plugin RPC timeout must be finite and positive") from error
        if not math.isfinite(rpc_timeout) or rpc_timeout < _MIN_RPC_TIMEOUT:
            raise ValueError("memory plugin RPC timeout is too low")
        timeout = options.get("timeout_seconds", 5)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout_seconds must be finite, positive, and no more than 60 percent of RPC timeout")
        self.timeout = float(timeout)
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= rpc_timeout * 0.6:
            raise ValueError("timeout_seconds must be finite, positive, and no more than 60 percent of RPC timeout")
        self.client = httpx.Client(
            timeout=httpx.Timeout(self.timeout), follow_redirects=False, trust_env=False,
        )
        flush_after_ingest = options.get("flush_after_ingest", False)
        if not isinstance(flush_after_ingest, bool):
            raise ValueError("flush_after_ingest must be a boolean")
        self.flush_after_ingest = flush_after_ingest
        if self.flush_after_ingest:
            self._schedule_buffered()

    @staticmethod
    def _id(value: Any) -> str:
        value = str(value)
        if not value or len(value) > 128 or value in {".", ".."} or not _SAFE.fullmatch(value):
            raise ValueError("invalid EverOS scope identifier")
        return value

    @staticmethod
    def _url(value: Any) -> str:
        parsed = urlsplit(str(value))
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("EverOS URL must be loopback HTTP")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("EverOS URL contains unsupported components")
        if parsed.path not in {"", "/"}:
            raise ValueError("EverOS URL must not include a path")
        return str(value).rstrip("/")

    def ensure_session(self, db: Any, conversation_id: int) -> str:
        row = db.execute(
            "SELECT session_id FROM everos_sessions WHERE conversation_id=?", (conversation_id,)
        ).fetchone()
        if row:
            return str(row[0])
        session_id = f"{self.instance_namespace}-sophia-{conversation_id}"
        if len(session_id) > 128:
            raise ValueError("EverOS session identifier is too long")
        db.execute(
            "INSERT INTO everos_sessions(conversation_id, session_id, app_id, project_id) VALUES(?,?,?,?)",
            (conversation_id, session_id, self.app_id, self.project_id),
        )
        return session_id

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _error(kind: str) -> str:
        return f"EverOS {kind} failed"

    def _schedule_buffered(self) -> None:
        now = self._now().isoformat()
        with self.database.connect() as db:
            rows = db.execute(
                """SELECT id, app_id, project_id, session_id
                   FROM everos_outbox
                   WHERE delivered_at IS NOT NULL AND flush_generation=0
                   ORDER BY id"""
            ).fetchall()
            for row in rows:
                self._schedule_row(db, row, now)

    @staticmethod
    def _schedule_row(db: Any, row: Any, due_at: str) -> int:
        db.execute(
            """INSERT INTO everos_flush_state(app_id, project_id, session_id, due_at)
               VALUES(?,?,?,?) ON CONFLICT(app_id, project_id, session_id) DO NOTHING""",
            (row["app_id"], row["project_id"], row["session_id"], due_at),
        )
        state = db.execute(
            """SELECT requested_generation FROM everos_flush_state
               WHERE app_id=? AND project_id=? AND session_id=?""",
            (row["app_id"], row["project_id"], row["session_id"]),
        ).fetchone()
        generation = int(state[0]) + 1
        db.execute(
            """UPDATE everos_flush_state SET requested_generation=?, due_at=?
               WHERE app_id=? AND project_id=? AND session_id=?""",
            (generation, due_at, row["app_id"], row["project_id"], row["session_id"]),
        )
        db.execute(
            "UPDATE everos_outbox SET flush_generation=? WHERE id=? AND flush_generation=0",
            (generation, row["id"]),
        )
        return generation

    def _deliver_row(self, row: Any) -> bool:
        payload = {
            "session_id": row["session_id"],
            "app_id": row["app_id"],
            "project_id": row["project_id"],
            "messages": [{
                "sender_id": row["sender_id"],
                "role": row["role"],
                "timestamp": row["timestamp"],
                "content": row["text"],
            }],
            "defer_extraction": True,
        }
        error = None
        try:
            response = self.client.post(self.url + "/api/v2/memory/add", json=payload)
            response.raise_for_status()
            body = response.json()
            data = body.get("data") if isinstance(body, dict) else None
            if response.status_code != 200 or not isinstance(data, dict):
                raise ValueError("invalid EverOS delivery response")
            if data.get("message_count") != 1 or data.get("status") != "accumulated":
                raise ValueError("invalid EverOS delivery response")
        except Exception:
            error = self._error("delivery")
        with self.database.connect() as db:
            if error is None:
                now = self._now().isoformat()
                db.execute(
                    "UPDATE everos_outbox SET delivered_at=?, last_error=NULL WHERE id=?",
                    (now, row["id"]),
                )
                if self.flush_after_ingest:
                    self._schedule_row(db, row, now)
                return True
            db.execute(
                "UPDATE everos_outbox SET attempts=attempts+1, last_error=? WHERE id=?",
                (error, row["id"]),
            )
            return False

    def deliver_pending(self, limit: int = 100) -> dict[str, Any]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM everos_outbox WHERE delivered_at IS NULL ORDER BY attempts, id LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        delivered = sum(self._deliver_row(row) for row in rows)
        return {"attempted": len(rows), "delivered": delivered, "failed": len(rows) - delivered}

    def _flush_row(self, row: Any) -> bool:
        payload = {
            "app_id": row["app_id"],
            "project_id": row["project_id"],
            "session_id": row["session_id"],
        }
        error = None
        result = None
        try:
            response = self.client.post(self.url + "/api/v2/memory/flush", json=payload)
            response.raise_for_status()
            body = response.json()
            data = body.get("data") if isinstance(body, dict) else None
            result = data.get("status") if isinstance(data, dict) else None
            if response.status_code != 200 or result not in {"extracted", "no_extraction"}:
                raise ValueError("invalid EverOS flush response")
        except Exception:
            error = self._error("flush")
        with self.database.connect() as db:
            if error is not None:
                state = db.execute(
                    "SELECT attempts FROM everos_flush_state WHERE app_id=? AND project_id=? AND session_id=?",
                    (row["app_id"], row["project_id"], row["session_id"]),
                ).fetchone()
                attempts = int(state[0]) + 1
                due = (self._now() + timedelta(seconds=min(60, 2 ** min(attempts, 6)))).isoformat()
                db.execute(
                    """UPDATE everos_flush_state SET attempts=?, last_error=?, due_at=?
                       WHERE app_id=? AND project_id=? AND session_id=?""",
                    (attempts, error, due, row["app_id"], row["project_id"], row["session_id"]),
                )
                return False
            captured = int(row["requested_generation"])
            now = self._now().isoformat()
            db.execute(
                """UPDATE everos_flush_state SET processed_generation=?, attempts=0,
                   last_error=NULL, last_result=?,
                   due_at=CASE WHEN requested_generation > ? THEN ? ELSE NULL END
                   WHERE app_id=? AND project_id=? AND session_id=?
                   AND processed_generation < ?""",
                (captured, result, captured, now, row["app_id"], row["project_id"], row["session_id"], captured),
            )
            db.execute(
                """UPDATE everos_outbox SET processed_at=?
                   WHERE app_id=? AND project_id=? AND session_id=?
                   AND flush_generation > 0 AND flush_generation <= ? AND processed_at IS NULL""",
                (now, row["app_id"], row["project_id"], row["session_id"], captured),
            )
            return True

    def backfill_once(self) -> dict[str, Any]:
        now = self._now().isoformat()
        with self.database.connect() as db:
            add = db.execute(
                """SELECT o.*, m.ingested_at AS queued_at
                   FROM everos_outbox o JOIN messages m ON m.id=o.message_id
                   WHERE o.delivered_at IS NULL ORDER BY o.attempts, o.id LIMIT 1"""
            ).fetchone()
            flush = db.execute(
                """SELECT * FROM everos_flush_state
                   WHERE requested_generation > processed_generation AND due_at IS NOT NULL AND due_at <= ?
                   ORDER BY attempts, due_at, session_id LIMIT 1""",
                (now,),
            ).fetchone()
        if not self.flush_after_ingest:
            flush = None
        if add is None and flush is None:
            return {"attempted": 0, "delivered": 0, "failed": 0, "flushed": 0, "flush_failed": 0}
        if flush is None or (add is not None and (int(add["attempts"]), add["queued_at"]) <= (int(flush["attempts"]), flush["due_at"])):
            delivered = int(self._deliver_row(add))
            return {"attempted": 1, "delivered": delivered, "failed": 1 - delivered, "flushed": 0, "flush_failed": 0}
        flushed = int(self._flush_row(flush))
        return {"attempted": 1, "delivered": 0, "failed": 0, "flushed": flushed, "flush_failed": 1 - flushed}

    def status(self) -> dict[str, Any]:
        with self.database.connect() as db:
            row = db.execute(
                """SELECT COUNT(*) FILTER (WHERE delivered_at IS NULL) AS pending,
                   COUNT(*) FILTER (WHERE delivered_at IS NOT NULL) AS buffered,
                   COUNT(*) FILTER (WHERE flush_generation > 0 AND processed_at IS NULL) AS flush_pending,
                   COUNT(*) FILTER (WHERE processed_at IS NOT NULL) AS processed,
                   COALESCE(SUM(attempts), 0) AS attempts, MAX(last_error) AS last_error
                   FROM everos_outbox"""
            ).fetchone()
            flush = db.execute(
                "SELECT COALESCE(SUM(attempts), 0) AS attempts, MAX(last_error) AS last_error, MAX(last_result) AS last_result FROM everos_flush_state"
            ).fetchone()
        return {
            "enabled": True,
            "pending": int(row["pending"]),
            "buffered": int(row["buffered"]),
            "flush_pending": int(row["flush_pending"]),
            "processed": int(row["processed"]),
            "attempts": int(row["attempts"]) + int(flush["attempts"]),
            "last_error": flush["last_error"] or row["last_error"],
            "last_result": flush["last_result"],
            "flush_after_ingest": self.flush_after_ingest,
        }

    def close(self) -> None:
        self.client.close()
