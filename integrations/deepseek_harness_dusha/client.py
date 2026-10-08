"""Async HTTP client for the Dusha state API."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from .config import DeepSeekHarnessConfig

# Sentinel marks an optional value the caller did not supply, so it is omitted.
UNSET: Any = object()


class DushaError(Exception):
    def __init__(self, message: str, *, status: int | None = None, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


def _assign(payload: dict[str, Any], key: str, value: Any) -> None:
    if value is not UNSET:
        payload[key] = value


class DushaClient:
    def __init__(
        self,
        config: DeepSeekHarnessConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Accept": "application/json"}
        if config.api_token:
            headers["X-Companion-Token"] = config.api_token
        self._client = httpx.AsyncClient(
            base_url=config.gateway_url,
            timeout=config.request_timeout_seconds,
            headers=headers,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self._client.request(method, path, json=json, params=params)
        except httpx.HTTPError as error:
            raise DushaError(f"Dusha is not reachable at {self._client.base_url}: {error}") from error
        if response.status_code >= 400:
            detail = self._detail(response)
            message = detail or f"Dusha request failed ({response.status_code})."
            raise DushaError(message, status=response.status_code, detail=detail)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return ""
        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, str):
                return detail
            if detail is not None:
                return str(detail)
        return ""

    async def health(self) -> Any:
        return await self._request("GET", "/health")

    async def ingest_message(
        self,
        *,
        harness: str,
        conversation_id: str,
        role: str,
        content: Any,
        route: str = "",
        external_id: str = "",
        occurred_at: Any = UNSET,
    ) -> Any:
        body: dict[str, Any] = {
            "harness": harness,
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "route": route,
            "external_id": external_id,
        }
        _assign(body, "occurred_at", occurred_at)
        return await self._request("POST", "/state/v1/messages", json=body)

    async def build_context(
        self,
        *,
        harness: str,
        conversation_id: str,
        query: str,
        exclude_message_ids: Any = None,
        include_recent: bool = True,
    ) -> Any:
        body: dict[str, Any] = {
            "harness": harness,
            "conversation_id": conversation_id,
            "query": query,
            "include_recent": include_recent,
        }
        if exclude_message_ids is not None:
            body["exclude_message_ids"] = list(exclude_message_ids)
        return await self._request("POST", "/state/v1/context", json=body)

    async def search_memory(self, *, query: str, limit: Any = UNSET, context_messages: Any = UNSET) -> Any:
        body: dict[str, Any] = {"query": query}
        _assign(body, "limit", limit)
        _assign(body, "context_messages", context_messages)
        return await self._request("POST", "/state/v1/memory/search", json=body)

    async def get_memory(self, *, memory_id: Any, context_messages: Any = UNSET) -> Any:
        params: dict[str, Any] = {}
        _assign(params, "context_messages", context_messages)
        path = f"/state/v1/memory/{quote(str(memory_id), safe='')}"
        return await self._request("GET", path, params=params or None)

    async def get_affect(self) -> Any:
        return await self._request("GET", "/state/v1/affect")

    async def remember_fact(
        self,
        *,
        key: str,
        text: str,
        priority: Any = UNSET,
        reason: Any = UNSET,
        source_message_id: Any = UNSET,
        review_after: Any = UNSET,
        expires_at: Any = UNSET,
    ) -> Any:
        body: dict[str, Any] = {"key": key, "text": text}
        _assign(body, "priority", priority)
        _assign(body, "reason", reason)
        _assign(body, "source_message_id", source_message_id)
        _assign(body, "review_after", review_after)
        _assign(body, "expires_at", expires_at)
        return await self._request("POST", "/state/v1/evergreen/facts", json=body)

    async def list_facts(
        self,
        *,
        include_inactive: Any = UNSET,
        due_only: Any = UNSET,
        limit: Any = UNSET,
    ) -> Any:
        params: dict[str, Any] = {}
        _assign(params, "include_inactive", include_inactive)
        _assign(params, "due_only", due_only)
        _assign(params, "limit", limit)
        return await self._request("GET", "/state/v1/evergreen/facts", params=params or None)

    async def revise_fact(
        self,
        *,
        fact_id: str,
        expected_revision: int,
        text: str,
        priority: Any = UNSET,
        reason: Any = UNSET,
        source_message_id: Any = UNSET,
        review_after: Any = UNSET,
        expires_at: Any = UNSET,
    ) -> Any:
        body: dict[str, Any] = {"expected_revision": expected_revision, "text": text}
        _assign(body, "priority", priority)
        _assign(body, "reason", reason)
        _assign(body, "source_message_id", source_message_id)
        _assign(body, "review_after", review_after)
        _assign(body, "expires_at", expires_at)
        path = f"/state/v1/evergreen/facts/{quote(fact_id, safe='')}/revisions"
        return await self._request("POST", path, json=body)

    async def forget_fact(
        self,
        *,
        fact_id: str,
        expected_revision: int,
        reason: str,
        source_message_id: Any = UNSET,
    ) -> Any:
        body: dict[str, Any] = {"expected_revision": expected_revision, "reason": reason}
        _assign(body, "source_message_id", source_message_id)
        path = f"/state/v1/evergreen/facts/{quote(fact_id, safe='')}/forget"
        return await self._request("POST", path, json=body)
