"""OpenCode-parity tools exposed by the DeepSeek harness adapter."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from .client import UNSET, DushaError

_CLEAR_WORDS = {"clear", "none", "null"}


class ToolValidationError(ValueError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolValidationError(f"{name} is required")
    return value


def _require_number(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ToolValidationError(f"{name} must be a number")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError as error:
            raise ToolValidationError(f"{name} must be a number") from error
    raise ToolValidationError(f"{name} must be a number")


def _optional_number(value: Any, name: str) -> Any:
    if value is None or value is UNSET:
        return UNSET
    return _require_number(value, name)


def _optional_bool(value: Any, name: str) -> Any:
    if value is None or value is UNSET:
        return UNSET
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    raise ToolValidationError(f"{name} must be a boolean")


def _optional_string(value: Any, name: str) -> Any:
    if value is None or value is UNSET:
        return UNSET
    if isinstance(value, str):
        return value if value else UNSET
    raise ToolValidationError(f"{name} must be a string")


def _optional_time(value: Any, name: str) -> Any:
    if value is UNSET:
        return UNSET
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.casefold() in _CLEAR_WORDS:
            return None
        return normalized or UNSET
    raise ToolValidationError(f"{name} must be an ISO 8601 time string")


class ToolsetMixin:
    async def _run_tool(self, operation: Callable[[], Awaitable[Any]]) -> str:
        try:
            payload = await operation()
        except ToolValidationError as error:
            return _json({"ok": False, "error": str(error)})
        except DushaError as error:
            result: dict[str, Any] = {"ok": False, "error": str(error)}
            if error.status is not None:
                result["status"] = error.status
            return _json(result)
        except Exception as error:
            return _json({"ok": False, "error": str(error)})
        if isinstance(payload, dict):
            return _json({"ok": True, **payload})
        return _json({"ok": True, "result": payload})

    async def memory_search(self, query: Any, limit: Any = None, context_messages: Any = None) -> str:
        async def operation() -> Any:
            return await self.client.search_memory(
                query=_require_string(query, "query"),
                limit=_optional_number(limit, "limit"),
                context_messages=_optional_number(context_messages, "context_messages"),
            )

        return await self._run_tool(operation)

    async def context_build(
        self, query: Any, conversation_id: Any = None, include_recent: Any = None
    ) -> str:
        async def operation() -> Any:
            conversation = _optional_string(conversation_id, "conversation_id")
            recent = _optional_bool(include_recent, "include_recent")
            return await self.client.build_context(
                harness=self.config.harness,
                conversation_id="" if conversation is UNSET else conversation,
                query=_require_string(query, "query"),
                include_recent=True if recent is UNSET else recent,
            )

        return await self._run_tool(operation)

    async def affect_status(self) -> str:
        async def operation() -> Any:
            return await self.client.get_affect()

        return await self._run_tool(operation)

    async def evergreen_remember(
        self,
        key: Any,
        text: Any,
        priority: Any = None,
        reason: Any = None,
        review_after: Any = UNSET,
        expires_at: Any = UNSET,
    ) -> str:
        async def operation() -> Any:
            return await self.client.remember_fact(
                key=_require_string(key, "key"),
                text=_require_string(text, "text"),
                priority=_optional_number(priority, "priority"),
                reason=_optional_string(reason, "reason"),
                review_after=_optional_time(review_after, "review_after"),
                expires_at=_optional_time(expires_at, "expires_at"),
            )

        return await self._run_tool(operation)

    async def evergreen_list(
        self, include_inactive: Any = None, due_only: Any = None, limit: Any = None
    ) -> str:
        async def operation() -> Any:
            return await self.client.list_facts(
                include_inactive=_optional_bool(include_inactive, "include_inactive"),
                due_only=_optional_bool(due_only, "due_only"),
                limit=_optional_number(limit, "limit"),
            )

        return await self._run_tool(operation)

    async def evergreen_revise(
        self,
        fact_id: Any,
        expected_revision: Any,
        text: Any,
        priority: Any = None,
        reason: Any = None,
        review_after: Any = UNSET,
        expires_at: Any = UNSET,
    ) -> str:
        async def operation() -> Any:
            return await self.client.revise_fact(
                fact_id=_require_string(fact_id, "fact_id"),
                expected_revision=_require_number(expected_revision, "expected_revision"),
                text=_require_string(text, "text"),
                priority=_optional_number(priority, "priority"),
                reason=_optional_string(reason, "reason"),
                review_after=_optional_time(review_after, "review_after"),
                expires_at=_optional_time(expires_at, "expires_at"),
            )

        return await self._run_tool(operation)

    async def evergreen_forget(self, fact_id: Any, expected_revision: Any, reason: Any) -> str:
        async def operation() -> Any:
            return await self.client.forget_fact(
                fact_id=_require_string(fact_id, "fact_id"),
                expected_revision=_require_number(expected_revision, "expected_revision"),
                reason=_require_string(reason, "reason"),
            )

        return await self._run_tool(operation)
