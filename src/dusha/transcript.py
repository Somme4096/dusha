from __future__ import annotations

import json
from typing import Any

from .database import Database
from .memory import text_from_content
from .serialization import canonical

ROLES = frozenset({"user", "assistant", "tool"})


def _arguments(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def transcript_key(message: dict[str, Any]) -> str:
    calls = [
        {
            "id": call.get("id") or "",
            "name": (call.get("function") or {}).get("name") or "",
            "arguments": _arguments((call.get("function") or {}).get("arguments")),
        }
        for call in message.get("tool_calls") or []
        if isinstance(call, dict)
    ]
    return canonical(
        {
            "role": message["role"],
            "text": text_from_content(message.get("content")),
            "tool_calls": calls,
            "tool_call_id": message.get("tool_call_id") or "",
        }
    )


class TranscriptTree:
    def __init__(self, database: Database):
        self.database = database

    def root(self, harness: str, key: str, conversation: str) -> tuple[int, str] | None:
        scope = " AND c.external_id=?" if conversation else ""
        with self.database.connect() as db:
            row = db.execute(
                f"""SELECT n.message_id, c.external_id FROM transcript_nodes n
                    JOIN conversations c ON c.id=n.conversation_id
                    WHERE n.parent_id IS NULL AND n.key=? AND c.harness=?{scope}
                    ORDER BY n.message_id LIMIT 1""",
                (key, harness, conversation) if conversation else (key, harness),
            ).fetchone()
        return (int(row["message_id"]), str(row["external_id"])) if row else None

    def child(self, parent: int, key: str) -> int | None:
        with self.database.connect() as db:
            row = db.execute(
                """SELECT message_id FROM transcript_nodes
                   WHERE parent_id=? AND key=? ORDER BY message_id LIMIT 1""",
                (parent, key),
            ).fetchone()
        return int(row["message_id"]) if row else None

    def add(self, message_id: int, parent: int | None, conversation_id: int, key: str) -> None:
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO transcript_nodes(message_id, parent_id, conversation_id, key) VALUES(?,?,?,?)",
                (message_id, parent, conversation_id, key),
            )
