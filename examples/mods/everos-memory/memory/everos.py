from __future__ import annotations

import re
import math
from datetime import datetime, timezone
from urllib.parse import urlsplit
from typing import Any

import httpx


_SAFE = re.compile(r"^[a-zA-Z0-9_.@+-]+$")
_OPTIONS = {
    "url", "app_id", "project_id", "instance_namespace", "user_sender_id",
    "assistant_sender_id", "timeout_seconds",
}
_GATEWAY_TIMEOUT = 15.0


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
        timeout = options.get("timeout_seconds", 5)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout_seconds must be finite, positive, and less than gateway timeout")
        self.timeout = float(timeout)
        if not math.isfinite(self.timeout) or not 0 < self.timeout < _GATEWAY_TIMEOUT:
            raise ValueError("timeout_seconds must be finite, positive, and less than gateway timeout")
        self.client = httpx.Client(
            timeout=httpx.Timeout(self.timeout), follow_redirects=False, trust_env=False,
        )

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

    def deliver_pending(self, limit: int = 100) -> dict[str, Any]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM everos_outbox WHERE delivered_at IS NULL ORDER BY id LIMIT ?", (max(1, min(limit, 1000)),)
            ).fetchall()
        delivered = 0
        failed = 0
        for row in rows:
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
                error = "EverOS delivery failed"
            with self.database.connect() as db:
                if error is None:
                    db.execute(
                        "UPDATE everos_outbox SET delivered_at=?, last_error=NULL WHERE id=?",
                        (datetime.now(timezone.utc).isoformat(), row["id"]),
                    )
                    delivered += 1
                else:
                    db.execute(
                        "UPDATE everos_outbox SET attempts=attempts+1, last_error=? WHERE id=?",
                        (error, row["id"]),
                    )
                    failed += 1
        return {"attempted": len(rows), "delivered": delivered, "failed": failed}

    def status(self) -> dict[str, Any]:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS pending, COALESCE(SUM(attempts), 0) AS attempts, MAX(last_error) AS last_error FROM everos_outbox WHERE delivered_at IS NULL"
            ).fetchone()
        return {"enabled": True, "pending": int(row["pending"]), "attempts": int(row["attempts"]), "last_error": row["last_error"]}

    def close(self) -> None:
        self.client.close()
