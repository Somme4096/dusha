from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import Field

from . import prompts as _prompts
from . import schema as _schema
from .commands import CommandError, arg, option
from .config import EvergreenConfig
from .database import Database
from .hooks import Block, ContextRequest
from .interfaces import GatewayInput, call, errors
from .serialization import safe_json
from .timeutil import isoformat, parse_time, utc_now

_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
_KEEP = object()
_DEFAULT_EVERGREEN = _prompts.default_prompts()["evergreen"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS evergreen_fact_revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    fact_key TEXT NOT NULL,
    text TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('active', 'forgotten')),
    priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
    source_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    reason TEXT NOT NULL DEFAULT '',
    review_after TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL CHECK(created_by IN ('agent', 'operator', 'api')),
    UNIQUE(fact_id, revision)
);
CREATE INDEX IF NOT EXISTS evergreen_fact_key
ON evergreen_fact_revisions(fact_key, revision DESC);
CREATE INDEX IF NOT EXISTS evergreen_fact_history
ON evergreen_fact_revisions(fact_id, revision);
"""


class EvergreenConflict(ValueError):
    pass


class EvergreenStore:
    name = "evergreen"

    def __init__(self, database: Database, config: Any = None, prompts: dict[str, Any] | None = None):
        self.database = database
        database.migrate(SCHEMA)
        self.config = config
        self.delimiters = (prompts or {}).get("evergreen") or _DEFAULT_EVERGREEN

    def on_context(self, request: ContextRequest) -> Block | None:
        if self.config is None:
            return None
        text, facts = self.render(
            self.config.max_items,
            self.config.max_chars,
            open_delimiter=self.delimiters["open_delimiter"],
            close_delimiter=self.delimiters["close_delimiter"],
        )
        return Block(self.name, text, facts) if facts else None

    def remember(
        self,
        *,
        key: str,
        text: str,
        priority: int = 50,
        source_message_id: int | None = None,
        reason: str = "",
        review_after: str | datetime | None = None,
        expires_at: str | datetime | None = None,
        created_by: str = "agent",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        key = self._validate_key(key)
        text = self._validate_text(text)
        priority = self._validate_priority(priority)
        reason = self._validate_reason(reason)
        actor = self._validate_actor(created_by)
        timestamp = isoformat(now or utc_now())
        review = self._optional_time(review_after)
        expiry = self._optional_time(expires_at)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = self._active_key_row(db, key)
            if duplicate:
                state = "expired" if self._is_expired(duplicate["expires_at"], timestamp) else "active"
                raise EvergreenConflict(
                    f"fact key already exists: {duplicate['fact_id']} revision "
                    f"{duplicate['revision']} ({state})"
                )
            self._validate_source(db, source_message_id)
            fact_id = str(uuid.uuid4())
            row_id = self._insert_revision(
                db,
                fact_id=fact_id,
                revision=1,
                fact_key=key,
                text=text,
                state="active",
                priority=priority,
                source_message_id=source_message_id,
                reason=reason,
                review_after=review,
                expires_at=expiry,
                created_at=timestamp,
                created_by=actor,
            )
            row = db.execute(
                "SELECT * FROM evergreen_fact_revisions WHERE revision_id=?",
                (row_id,),
            ).fetchone()
        return self._row(row, timestamp)

    def revise(
        self,
        *,
        fact_id: str,
        expected_revision: int,
        text: str,
        priority: int | None = None,
        source_message_id: int | None = None,
        reason: str = "",
        review_after: str | datetime | None | object = _KEEP,
        expires_at: str | datetime | None | object = _KEEP,
        created_by: str = "agent",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        text = self._validate_text(text)
        if expected_revision < 1:
            raise ValueError("expected_revision must be positive")
        reason = self._validate_reason(reason)
        actor = self._validate_actor(created_by)
        timestamp = isoformat(now or utc_now())

        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._current_row(db, fact_id)
            if not current:
                raise KeyError(fact_id)
            if int(current["revision"]) != expected_revision:
                raise EvergreenConflict(
                    f"revision changed: expected {expected_revision}, current {current['revision']}"
                )
            duplicate = self._active_key_row(db, current["fact_key"], fact_id)
            if duplicate:
                raise EvergreenConflict(
                    f"fact key already exists: {duplicate['fact_id']} revision {duplicate['revision']}"
                )
            self._validate_source(db, source_message_id)
            next_priority = (
                int(current["priority"]) if priority is None else self._validate_priority(priority)
            )
            review = current["review_after"] if review_after is _KEEP else self._optional_time(review_after)
            expiry = current["expires_at"] if expires_at is _KEEP else self._optional_time(expires_at)
            row_id = self._insert_revision(
                db,
                fact_id=fact_id,
                revision=expected_revision + 1,
                fact_key=current["fact_key"],
                text=text,
                state="active",
                priority=next_priority,
                source_message_id=source_message_id,
                reason=reason,
                review_after=review,
                expires_at=expiry,
                created_at=timestamp,
                created_by=actor,
            )
            row = db.execute(
                "SELECT * FROM evergreen_fact_revisions WHERE revision_id=?",
                (row_id,),
            ).fetchone()
        return self._row(row, timestamp)

    def forget(
        self,
        *,
        fact_id: str,
        expected_revision: int,
        reason: str,
        source_message_id: int | None = None,
        created_by: str = "agent",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if expected_revision < 1:
            raise ValueError("expected_revision must be positive")
        reason = self._validate_reason(reason)
        if not reason:
            raise ValueError("reason is required when forgetting a fact")
        actor = self._validate_actor(created_by)
        timestamp = isoformat(now or utc_now())

        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._current_row(db, fact_id)
            if not current:
                raise KeyError(fact_id)
            if int(current["revision"]) != expected_revision:
                raise EvergreenConflict(
                    f"revision changed: expected {expected_revision}, current {current['revision']}"
                )
            if current["state"] == "forgotten":
                raise EvergreenConflict("fact is already forgotten")
            self._validate_source(db, source_message_id)
            row_id = self._insert_revision(
                db,
                fact_id=fact_id,
                revision=expected_revision + 1,
                fact_key=current["fact_key"],
                text=current["text"],
                state="forgotten",
                priority=current["priority"],
                source_message_id=source_message_id,
                reason=reason,
                review_after=current["review_after"],
                expires_at=current["expires_at"],
                created_at=timestamp,
                created_by=actor,
            )
            row = db.execute(
                "SELECT * FROM evergreen_fact_revisions WHERE revision_id=?",
                (row_id,),
            ).fetchone()
        return self._row(row, timestamp)

    def list_current(
        self,
        *,
        include_inactive: bool = False,
        due_only: bool = False,
        limit: int = 100,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        timestamp = isoformat(now or utc_now())
        with self.database.connect() as db:
            rows = db.execute(
                """SELECT e.* FROM evergreen_fact_revisions e
                   WHERE e.revision=(
                     SELECT MAX(n.revision) FROM evergreen_fact_revisions n
                     WHERE n.fact_id=e.fact_id
                   )
                   ORDER BY e.priority DESC, e.fact_key, e.fact_id""",
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = self._row(row, timestamp)
            active = item["effective_state"] == "active"
            due = active and bool(item["review_after"] and item["review_after"] <= timestamp)
            if due_only and not due:
                continue
            if not include_inactive and not active:
                continue
            item["review_due"] = due
            results.append(item)
            if len(results) >= max(1, min(limit, 500)):
                break
        return results

    def history(self, fact_id: str) -> list[dict[str, Any]]:
        timestamp = isoformat(utc_now())
        with self.database.connect() as db:
            rows = db.execute(
                """SELECT * FROM evergreen_fact_revisions
                   WHERE fact_id=? ORDER BY revision""",
                (fact_id,),
            ).fetchall()
        if not rows:
            raise KeyError(fact_id)
        return [self._row(row, timestamp) for row in rows]

    def render(
        self,
        max_items: int,
        max_chars: int,
        *,
        open_delimiter: str | None = None,
        close_delimiter: str | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        if max_items <= 0 or max_chars <= 0:
            return "", []
        opening = open_delimiter or _DEFAULT_EVERGREEN["open_delimiter"]
        closing = close_delimiter or _DEFAULT_EVERGREEN["close_delimiter"]
        candidates = self.list_current(limit=500)
        selected: list[dict[str, Any]] = []
        for fact in candidates:
            item = {
                "id": fact["fact_id"],
                "revision": fact["revision"],
                "key": fact["key"],
                "text": fact["text"],
            }
            if fact["review_due"]:
                item["review_due"] = True
            trial = selected + [item]
            rendered = opening + safe_json(trial) + closing
            if len(rendered) <= max_chars:
                selected = trial
            if len(selected) >= max_items:
                break
        if not selected:
            return "", []
        return opening + safe_json(selected) + closing, selected

    @staticmethod
    def _current_row(db: Any, fact_id: str) -> Any:
        return db.execute(
            """SELECT * FROM evergreen_fact_revisions
               WHERE fact_id=? ORDER BY revision DESC LIMIT 1""",
            (fact_id,),
        ).fetchone()

    @staticmethod
    def _active_key_row(db: Any, key: str, exclude_fact_id: str = "") -> Any:
        return db.execute(
            """SELECT e.fact_id, e.revision, e.expires_at
               FROM evergreen_fact_revisions e
               WHERE e.fact_key=? AND e.fact_id!=? AND e.state='active'
                 AND e.revision=(
                   SELECT MAX(n.revision) FROM evergreen_fact_revisions n
                   WHERE n.fact_id=e.fact_id
                 )
               LIMIT 1""",
            (key, exclude_fact_id),
        ).fetchone()

    @staticmethod
    def _validate_source(db: Any, source_message_id: int | None) -> None:
        if source_message_id is None:
            return
        row = db.execute("SELECT 1 FROM messages WHERE id=?", (source_message_id,)).fetchone()
        if not row:
            raise ValueError("source message does not exist")

    @staticmethod
    def _validate_key(value: str) -> str:
        key = value.strip().casefold()
        if not _KEY_PATTERN.fullmatch(key):
            raise ValueError("key must use 1 to 120 lowercase letters, digits, dots, underscores, or hyphens")
        return key

    @staticmethod
    def _validate_text(value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("fact text is required")
        if len(text) > 4_000:
            raise ValueError("fact text exceeds 4000 characters")
        return text

    @staticmethod
    def _validate_priority(value: int) -> int:
        priority = int(value)
        if not 0 <= priority <= 100:
            raise ValueError("priority must be between 0 and 100")
        return priority

    @staticmethod
    def _validate_reason(value: str) -> str:
        reason = value.strip()
        if len(reason) > 1_000:
            raise ValueError("reason exceeds 1000 characters")
        return reason

    @staticmethod
    def _validate_actor(value: str) -> str:
        if value not in {"agent", "operator", "api"}:
            raise ValueError("created_by must be agent, operator, or api")
        return value

    @staticmethod
    def _optional_time(value: str | datetime | None | object) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, (str, datetime)):
            raise ValueError("time value must be an ISO 8601 timestamp or null")
        return isoformat(parse_time(value))

    @staticmethod
    def _is_expired(expires_at: str | None, timestamp: str) -> bool:
        return bool(expires_at and expires_at <= timestamp)

    @classmethod
    def _row(cls, row: Any, timestamp: str) -> dict[str, Any]:
        item = dict(row)
        item["key"] = item.pop("fact_key")
        item["effective_state"] = (
            "expired"
            if item["state"] == "active" and cls._is_expired(item["expires_at"], timestamp)
            else item["state"]
        )
        return item

    @staticmethod
    def _insert_revision(
        db: Any,
        *,
        fact_id: str,
        revision: int,
        fact_key: str,
        text: str,
        state: str,
        priority: int,
        source_message_id: int | None,
        reason: str,
        review_after: str | None,
        expires_at: str | None,
        created_at: str,
        created_by: str,
    ) -> int:
        cursor = db.execute(
            """INSERT INTO evergreen_fact_revisions
               (fact_id, revision, fact_key, text, state, priority,
                source_message_id, reason, review_after, expires_at, created_at, created_by)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                fact_id,
                revision,
                fact_key,
                text,
                state,
                priority,
                source_message_id,
                reason,
                review_after,
                expires_at,
                created_at,
                created_by,
            ),
        )
        return cursor.lastrowid


name = "evergreen"
inject = ("database", "prompts", "api", "cli")


class RememberFactInput(GatewayInput):
    key: str
    text: str
    priority: int = Field(default=50, ge=0, le=100)
    source_message_id: int | None = None
    reason: str = ""
    review_after: str | None = None
    expires_at: str | None = None


class ReviseFactInput(GatewayInput):
    expected_revision: int = Field(ge=1)
    text: str
    priority: int | None = Field(default=None, ge=0, le=100)
    source_message_id: int | None = None
    reason: str = ""
    review_after: str | None = None
    expires_at: str | None = None


class ForgetFactInput(GatewayInput):
    expected_revision: int = Field(ge=1)
    reason: str
    source_message_id: int | None = None


def apply(ctx: Any, config: EvergreenConfig) -> None:
    store = EvergreenStore(ctx.database, config, ctx.prompts)
    ctx.provide("evergreen", store)
    ctx.on("context", store.on_context)
    _routes(ctx.api, store)
    _commands(ctx.cli, store)


def _routes(router: APIRouter, store: EvergreenStore) -> None:
    @router.post(
        "/state/v1/evergreen/facts",
        tags=["state"],
        response_model=_schema.FactEnvelope,
        summary="Remember an evergreen fact",
        responses=errors(409, 422, 503),
    )
    async def remember_fact(body: RememberFactInput) -> dict[str, Any]:
        fact = await call(
            store.remember,
            key=body.key,
            text=body.text,
            priority=body.priority,
            source_message_id=body.source_message_id,
            reason=body.reason,
            review_after=body.review_after,
            expires_at=body.expires_at,
            created_by="agent",
            errors=(EvergreenConflict, ValueError),
            conflict=EvergreenConflict,
        )
        return {"fact": fact}

    @router.get(
        "/state/v1/evergreen/facts",
        tags=["state"],
        response_model=_schema.FactsEnvelope,
        responses=errors(503),
    )
    async def list_facts(
        include_inactive: bool = False,
        due_only: bool = False,
        limit: int = Query(100, ge=1, le=500),
    ) -> dict[str, Any]:
        facts = await call(
            store.list_current, include_inactive=include_inactive, due_only=due_only, limit=limit
        )
        return {"facts": facts}

    @router.get(
        "/state/v1/evergreen/facts/{fact_id}/history",
        tags=["state"],
        response_model=_schema.RevisionsEnvelope,
        responses=errors(404, 503),
    )
    async def fact_history(fact_id: str) -> dict[str, Any]:
        revisions = await call(
            store.history, fact_id, errors=(KeyError,), not_found="evergreen fact not found"
        )
        return {"revisions": revisions}

    @router.post(
        "/state/v1/evergreen/facts/{fact_id}/revisions",
        tags=["state"],
        response_model=_schema.FactEnvelope,
        summary="Revise an evergreen fact",
        responses=errors(404, 409, 422, 503),
    )
    async def revise_fact(fact_id: str, body: ReviseFactInput) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "fact_id": fact_id,
            "expected_revision": body.expected_revision,
            "text": body.text,
            "priority": body.priority,
            "source_message_id": body.source_message_id,
            "reason": body.reason,
            "created_by": "agent",
        }
        if "review_after" in body.model_fields_set:
            kwargs["review_after"] = body.review_after
        if "expires_at" in body.model_fields_set:
            kwargs["expires_at"] = body.expires_at
        fact = await call(
            store.revise,
            **kwargs,
            errors=(KeyError, EvergreenConflict, ValueError),
            not_found="evergreen fact not found",
            conflict=EvergreenConflict,
        )
        return {"fact": fact}

    @router.post(
        "/state/v1/evergreen/facts/{fact_id}/forget",
        tags=["state"],
        response_model=_schema.FactEnvelope,
        responses=errors(404, 409, 422, 503),
    )
    async def forget_fact(fact_id: str, body: ForgetFactInput) -> dict[str, Any]:
        fact = await call(
            store.forget,
            fact_id=fact_id,
            expected_revision=body.expected_revision,
            reason=body.reason,
            source_message_id=body.source_message_id,
            created_by="agent",
            errors=(KeyError, EvergreenConflict, ValueError),
            not_found="evergreen fact not found",
            conflict=EvergreenConflict,
        )
        return {"fact": fact}


def revision(values: dict[str, Any]) -> dict[str, Any]:
    kwargs = dict(values)
    for name in ("review_after", "expires_at"):
        clear = kwargs.pop(f"clear_{name}", False)
        if clear and kwargs.get(name) is not None:
            raise CommandError(f"{name} and clear_{name} cannot be used together")
        if clear:
            kwargs[name] = None
        elif kwargs.get(name) is None:
            kwargs.pop(name, None)
    return kwargs


REVISE_ARGS = (
    arg("fact_id", help="Fact id."),
    arg("expected_revision", int, help="Revision you last read."),
    arg("text", help="Complete fact text."),
    option("priority", int, help="Priority from 0 to 100, unchanged when absent."),
    option("reason", default="", help="Why the fact changes."),
    option("review_after", help="ISO timestamp to review the fact after."),
    option("clear_review_after", bool, help="Remove the review time."),
    option("expires_at", help="ISO timestamp the fact expires at."),
    option("clear_expires_at", bool, help="Remove the expiry time."),
)


def _commands(cli: Any, store: EvergreenStore) -> None:
    evergreen = cli.group("evergreen", "Inspect or update evergreen facts")
    evergreen.command(
        "list",
        lambda **values: {"facts": store.list_current(**values)},
        help="List current facts by priority",
        args=[option("include_inactive", bool), option("due_only", bool), option("limit", int, default=100)],
    )
    evergreen.command(
        "history",
        lambda fact_id: {"revisions": store.history(fact_id)},
        help="List every revision of a fact",
        args=[arg("fact_id")],
    )
    evergreen.command(
        "remember",
        lambda **values: {"fact": store.remember(**values, created_by="operator")},
        help="Remember a fact under a stable key",
        args=[
            arg("key"),
            arg("text"),
            option("priority", int, default=50),
            option("source_message_id", int),
            option("reason", default=""),
            option("review_after"),
            option("expires_at"),
        ],
    )
    evergreen.command(
        "revise",
        lambda **values: {"fact": store.revise(**revision(values), created_by="operator")},
        help="Replace a fact at the revision you last read",
        args=[*REVISE_ARGS, option("source_message_id", int)],
    )
    evergreen.command(
        "forget",
        lambda **values: {"fact": store.forget(**values, created_by="operator")},
        help="Forget a fact at the revision you last read",
        args=[
            arg("fact_id"),
            arg("expected_revision", int),
            option("source_message_id", int),
            option("reason", required=True),
        ],
    )
