from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .serialization import safe_json

logger = logging.getLogger("dusha")


@dataclass(frozen=True)
class ContextRequest:
    query: str = ""
    harness: str = ""
    conversation_id: str = ""


@dataclass(frozen=True)
class DecisionRequest:
    message: str
    emotions: dict[str, dict[str, Any]] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    instruction: str = ""


@dataclass(frozen=True)
class Block:
    name: str
    text: str
    data: Any


def render(name: str, open_delimiter: str, data: Any, close_delimiter: str) -> Block:
    return Block(name, open_delimiter + safe_json(data) + close_delimiter, data)


class Contributor:
    name = ""

    def start(self) -> None:
        return None

    def close(self) -> None:
        return None

    def on_message(self, message: dict[str, Any]) -> Any:
        return None

    def on_context(self, request: ContextRequest) -> Block | None:
        return None

    def decide(self, request: DecisionRequest) -> dict[str, Any] | None:
        return None

    def tick(self) -> Any:
        return None

    def status(self) -> Any:
        return None


class Registry:
    def __init__(self) -> None:
        self.items: list[Contributor] = []

    def add(self, contributor: Contributor) -> None:
        self.items.append(contributor)

    def start(self) -> None:
        for contributor in self.items:
            try:
                contributor.start()
            except Exception:
                logger.exception("%s failed to start", contributor.name)

    def close(self) -> None:
        for contributor in self.items:
            try:
                contributor.close()
            except Exception:
                logger.exception("%s failed to close", contributor.name)

    def dispatch_message(self, message: dict[str, Any]) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for contributor in self.items:
            try:
                value = contributor.on_message(message)
            except Exception:
                logger.exception("%s on_message failed", contributor.name)
                continue
            if value is not None:
                results[contributor.name] = value
        return results

    def dispatch_context(self, request: ContextRequest) -> list[Block]:
        blocks: list[Block] = []
        for contributor in self.items:
            try:
                block = contributor.on_context(request)
            except Exception:
                logger.exception("%s on_context failed", contributor.name)
                continue
            if block is not None:
                blocks.append(block)
        return blocks

    def tick(self) -> dict[str, Any]:
        return self._collect("tick")

    def status(self) -> dict[str, Any]:
        return self._collect("status")

    def _collect(self, hook: str) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for contributor in self.items:
            try:
                value = getattr(contributor, hook)()
            except Exception:
                logger.exception("%s %s failed", contributor.name, hook)
                continue
            if value is not None:
                results[contributor.name] = value
        return results

    def dispatch_decision(
        self, request: DecisionRequest, skip: Contributor | None = None
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for contributor in self.items:
            if contributor is skip:
                continue
            try:
                value = contributor.decide(request)
            except Exception:
                logger.exception("%s decide failed", contributor.name)
                continue
            if isinstance(value, dict):
                results.append(value)
        return results
