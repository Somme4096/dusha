from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query

from . import schema as _schema
from .commands import arg, option
from .config import MemoConfig
from .database import Database
from .hooks import Block, ContextRequest, render
from .interfaces import GatewayInput, call, errors
from .serialization import safe_json
from .timeutil import isoformat, utc_now

name = "memo"
inject = ("database", "prompts", "api", "cli")

SCHEMA = """
CREATE TABLE IF NOT EXISTS memo_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived')),
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT
);
CREATE INDEX IF NOT EXISTS memo_notes_status_created
ON memo_notes(status, created_at);
"""


class MemoAddInput(GatewayInput):
    text: str


class MemoDoneInput(GatewayInput):
    reason: str


class MemoStore:
    name = name

    def __init__(self, database: Database, config: MemoConfig, prompts: dict[str, Any]):
        self.database = database
        database.migrate(SCHEMA)
        self.config = config
        self.blocks = prompts["context_blocks"]

    def add(self, text: str, now: datetime | None = None) -> dict[str, Any]:
        value = self._validate_text(text)
        timestamp = isoformat(now or utc_now())
        with self.database.connect() as db:
            cursor = db.execute(
                """INSERT INTO memo_notes(text, status, reason, created_at, updated_at, archived_at)
                   VALUES(?, 'active', '', ?, ?, NULL)""",
                (value, timestamp, timestamp),
            )
            row = db.execute("SELECT * FROM memo_notes WHERE id=?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    def list(self, status: str = "active", limit: int = 20) -> list[dict[str, Any]]:
        state = self._validate_status(status)
        capped = max(1, min(int(limit), 500))
        with self.database.connect() as db:
            rows = db.execute(
                """SELECT * FROM memo_notes
                   WHERE status=? ORDER BY created_at ASC, id ASC LIMIT ?""",
                (state, capped),
            ).fetchall()
        return [dict(row) for row in rows]

    def done(self, note_id: int, reason: str, now: datetime | None = None) -> dict[str, Any]:
        value = str(reason).strip()
        if not value:
            raise ValueError("reason is required when completing a memo")
        if len(value) > self.config.reason_max_chars:
            raise ValueError(f"reason exceeds {self.config.reason_max_chars} characters")
        timestamp = isoformat(now or utc_now())
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM memo_notes WHERE id=?", (int(note_id),)).fetchone()
            if current is None:
                raise KeyError(note_id)
            if current["status"] == "archived":
                raise ValueError("memo is already archived")
            db.execute(
                """UPDATE memo_notes
                   SET status='archived', reason=?, updated_at=?, archived_at=?
                   WHERE id=?""",
                (value, timestamp, timestamp, int(note_id)),
            )
            row = db.execute("SELECT * FROM memo_notes WHERE id=?", (int(note_id),)).fetchone()
        return dict(row)

    def on_context(self, request: ContextRequest) -> Block | None:
        if self.config.max_items < 1:
            return None
        opening, closing = self.blocks["memo_open_delimiter"], self.blocks["memo_close_delimiter"]
        selected: list[dict[str, Any]] = []
        for note in self.list(status="active", limit=self.config.max_items):
            item = {"id": note["id"], "text": note["text"], "created_at": note["created_at"]}
            if len(opening + safe_json(selected + [item]) + closing) > self.config.max_chars:
                break
            selected.append(item)
        if not selected:
            return None
        return render(self.name, opening, selected, closing)

    def _validate_text(self, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("memo text is required")
        if len(text) > self.config.text_max_chars:
            raise ValueError(f"memo text exceeds {self.config.text_max_chars} characters")
        return text

    @staticmethod
    def _validate_status(value: str) -> str:
        status = str(value or "active").strip().casefold()
        if status not in {"active", "archived"}:
            raise ValueError("status must be active or archived")
        return status


def apply(ctx: Any, config: MemoConfig) -> None:
    store = MemoStore(ctx.database, config, ctx.prompts)
    ctx.provide("memo", store)
    ctx.on("context", store.on_context)
    _routes(ctx.api, store)
    _commands(ctx.cli, store)


def _routes(router: APIRouter, store: MemoStore) -> None:
    @router.post(
        "/state/v1/memo/add",
        tags=["state"],
        response_model=_schema.MemoEnvelope,
        summary="Add an agent memo",
        responses=errors(422, 503),
    )
    async def memo_add(body: MemoAddInput) -> dict[str, Any]:
        return {"memo": await call(store.add, body.text, errors=(ValueError,))}

    @router.get(
        "/state/v1/memo/list",
        tags=["state"],
        response_model=_schema.MemosEnvelope,
        responses=errors(422, 503),
    )
    async def memo_list(status: str = "active", limit: int = Query(20, ge=1, le=500)) -> dict[str, Any]:
        return {"memos": await call(store.list, status=status, limit=limit, errors=(ValueError,))}

    @router.post(
        "/state/v1/memo/{note_id}/done",
        tags=["state"],
        response_model=_schema.MemoEnvelope,
        summary="Archive an agent memo with a reason",
        responses=errors(404, 422, 503),
    )
    async def memo_done(note_id: int, body: MemoDoneInput) -> dict[str, Any]:
        memo = await call(
            store.done, note_id, body.reason, errors=(KeyError, ValueError), not_found="memo not found"
        )
        return {"memo": memo}


def _commands(cli: Any, store: MemoStore) -> None:
    memo = cli.group("memo", "Manage agent memos")
    memo.command("add", lambda text: {"memo": store.add(text)}, help="Add a memo", args=[arg("text")])
    memo.command(
        "list",
        lambda status, limit: {"memos": store.list(status, limit)},
        help="List memos, oldest first",
        args=[
            option("status", choices=("active", "archived"), default="active"),
            option("limit", int, default=20),
        ],
    )
    memo.command(
        "done",
        lambda note_id, reason: {"memo": store.done(note_id, reason)},
        help="Archive a memo with the reason it is done",
        args=[arg("note_id", int), arg("reason")],
    )
