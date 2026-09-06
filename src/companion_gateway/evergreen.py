from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any

from .database import Database
from .timeutil import isoformat, parse_time, utc_now

_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
_KEEP = object()


class EvergreenConflict(ValueError):
    pass


class EvergreenStore:
    """Append-only revisions for facts selected and maintained by the agent."""

    def __init__(self, database: Database):
        self.database = database

    def remember(
        self,
        *,
        companion_id: str,
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
        self._ensure_companion(companion_id, timestamp)

        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = self._active_key_row(db, companion_id, key)
            if duplicate:
                state = "expired" if self._is_expired(duplicate["expires_at"], timestamp) else "active"
                raise EvergreenConflict(
                    f"fact key already exists: {duplicate['fact_id']} revision "
                    f"{duplicate['revision']} ({state})"
                )
            self._validate_source(db, companion_id, source_message_id)
            fact_id = str(uuid.uuid4())
            cursor = db.execute(
                """INSERT INTO evergreen_fact_revisions
                   (fact_id, companion_id, revision, fact_key, text, state, priority,
                    source_message_id, reason, review_after, expires_at, created_at, created_by)
                   VALUES(?,?,?,?,?,'active',?,?,?,?,?,?,?)""",
                (
                    fact_id,
                    companion_id,
                    1,
                    key,
                    text,
                    priority,
                    source_message_id,
                    reason,
                    review,
                    expiry,
                    timestamp,
                    actor,
                ),
            )
            row = db.execute(
                "SELECT * FROM evergreen_fact_revisions WHERE revision_id=?",
                (cursor.lastrowid,),
            ).fetchone()
        return self._row(row, timestamp)

    def revise(
        self,
        *,
        companion_id: str,
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
            current = self._current_row(db, companion_id, fact_id)
            if not current:
                raise KeyError(fact_id)
            if int(current["revision"]) != expected_revision:
                raise EvergreenConflict(
                    f"revision changed: expected {expected_revision}, current {current['revision']}"
                )
            duplicate = self._active_key_row(db, companion_id, current["fact_key"], fact_id)
            if duplicate:
                raise EvergreenConflict(
                    f"fact key already exists: {duplicate['fact_id']} revision {duplicate['revision']}"
                )
            self._validate_source(db, companion_id, source_message_id)
            next_priority = (
                int(current["priority"]) if priority is None else self._validate_priority(priority)
            )
            review = current["review_after"] if review_after is _KEEP else self._optional_time(review_after)
            expiry = current["expires_at"] if expires_at is _KEEP else self._optional_time(expires_at)
            cursor = db.execute(
                """INSERT INTO evergreen_fact_revisions
                   (fact_id, companion_id, revision, fact_key, text, state, priority,
                    source_message_id, reason, review_after, expires_at, created_at, created_by)
                   VALUES(?,?,?,?,?,'active',?,?,?,?,?,?,?)""",
                (
                    fact_id,
                    companion_id,
                    expected_revision + 1,
                    current["fact_key"],
                    text,
                    next_priority,
                    source_message_id,
                    reason,
                    review,
                    expiry,
                    timestamp,
                    actor,
                ),
            )
            row = db.execute(
                "SELECT * FROM evergreen_fact_revisions WHERE revision_id=?",
                (cursor.lastrowid,),
            ).fetchone()
        return self._row(row, timestamp)

    def forget(
        self,
        *,
        companion_id: str,
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
            current = self._current_row(db, companion_id, fact_id)
            if not current:
                raise KeyError(fact_id)
            if int(current["revision"]) != expected_revision:
                raise EvergreenConflict(
                    f"revision changed: expected {expected_revision}, current {current['revision']}"
                )
            if current["state"] == "forgotten":
                raise EvergreenConflict("fact is already forgotten")
            self._validate_source(db, companion_id, source_message_id)
            cursor = db.execute(
                """INSERT INTO evergreen_fact_revisions
                   (fact_id, companion_id, revision, fact_key, text, state, priority,
                    source_message_id, reason, review_after, expires_at, created_at, created_by)
                   VALUES(?,?,?,?,?,'forgotten',?,?,?,?,?,?,?)""",
                (
                    fact_id,
                    companion_id,
                    expected_revision + 1,
                    current["fact_key"],
                    current["text"],
                    current["priority"],
                    source_message_id,
                    reason,
                    current["review_after"],
                    current["expires_at"],
                    timestamp,
                    actor,
                ),
            )
            row = db.execute(
                "SELECT * FROM evergreen_fact_revisions WHERE revision_id=?",
                (cursor.lastrowid,),
            ).fetchone()
        return self._row(row, timestamp)

    def list_current(
        self,
        companion_id: str,
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
                   WHERE e.companion_id=? AND e.revision=(
                     SELECT MAX(n.revision) FROM evergreen_fact_revisions n
                     WHERE n.fact_id=e.fact_id
                   )
                   ORDER BY e.priority DESC, e.fact_key, e.fact_id""",
                (companion_id,),
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

    def history(self, companion_id: str, fact_id: str) -> list[dict[str, Any]]:
        timestamp = isoformat(utc_now())
        with self.database.connect() as db:
            rows = db.execute(
                """SELECT * FROM evergreen_fact_revisions
                   WHERE companion_id=? AND fact_id=? ORDER BY revision""",
                (companion_id, fact_id),
            ).fetchall()
        if not rows:
            raise KeyError(fact_id)
        return [self._row(row, timestamp) for row in rows]

    def render(self, companion_id: str, max_items: int, max_chars: int) -> tuple[str, list[dict[str, Any]]]:
        if max_items <= 0 or max_chars <= 0:
            return "", []
        candidates = self.list_current(companion_id, limit=500)
        selected: list[dict[str, Any]] = []
        opening = "<evergreen_facts>\n"
        closing = "\n</evergreen_facts>\n"
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
            rendered = opening + self._safe_json(trial) + closing
            if len(rendered) <= max_chars:
                selected = trial
            if len(selected) >= max_items:
                break
        if not selected:
            return "", []
        return opening + self._safe_json(selected) + closing, selected

    def _ensure_companion(self, companion_id: str, timestamp: str) -> None:
        if not companion_id.strip():
            raise ValueError("companion_id is required")
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO companions(id, created_at, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at",
                (companion_id, timestamp, timestamp),
            )

    @staticmethod
    def _current_row(db: Any, companion_id: str, fact_id: str) -> Any:
        return db.execute(
            """SELECT * FROM evergreen_fact_revisions
               WHERE companion_id=? AND fact_id=? ORDER BY revision DESC LIMIT 1""",
            (companion_id, fact_id),
        ).fetchone()

    @staticmethod
    def _active_key_row(db: Any, companion_id: str, key: str, exclude_fact_id: str = "") -> Any:
        return db.execute(
            """SELECT e.fact_id, e.revision, e.expires_at
               FROM evergreen_fact_revisions e
               WHERE e.companion_id=? AND e.fact_key=? AND e.fact_id!=? AND e.state='active'
                 AND e.revision=(
                   SELECT MAX(n.revision) FROM evergreen_fact_revisions n
                   WHERE n.fact_id=e.fact_id
                 )
               LIMIT 1""",
            (companion_id, key, exclude_fact_id),
        ).fetchone()

    @staticmethod
    def _validate_source(db: Any, companion_id: str, source_message_id: int | None) -> None:
        if source_message_id is None:
            return
        row = db.execute(
            "SELECT companion_id FROM messages WHERE id=?",
            (source_message_id,),
        ).fetchone()
        if not row:
            raise ValueError("source message does not exist")
        if row["companion_id"] != companion_id:
            raise ValueError("source message belongs to another companion")

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
    def _safe_json(value: Any) -> str:
        return (
            json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            .replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
        )
