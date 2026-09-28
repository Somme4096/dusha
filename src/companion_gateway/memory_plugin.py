from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class IndexStatusResult:
    status: dict[str, Any]


@dataclass(frozen=True)
class BackfillIndexRequest:
    limit: int = 100
    force: bool = False


@dataclass(frozen=True)
class BackfillIndexResult:
    status: dict[str, Any]


@dataclass(frozen=True)
class RebuildIndexResult:
    status: dict[str, Any]


@dataclass(frozen=True)
class RebuildMemoryIndexResult:
    status: dict[str, Any]


@dataclass(frozen=True)
class MatchPhraseRequest:
    message: str


@dataclass(frozen=True)
class MatchPhraseResult:
    deltas: dict[str, float] | None


@dataclass(frozen=True)
class IngestMessagesRequest:
    messages: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class IngestMessagesResult:
    highest_id: int


@dataclass(frozen=True)
class InjectContextRequest:
    query: str = ""
    scope: str = ""
    max_chars: int = 0
    harness: str = ""
    conversation_id: str = ""


@dataclass(frozen=True)
class InjectContextResult:
    text: str = ""
    records: list[dict[str, Any]] = field(default_factory=list)


class MemoryPluginError(RuntimeError):
    pass


class MemoryPlugin(Protocol):
    def status(self, request: None, options: dict[str, Any]) -> IndexStatusResult: ...
    def backfill_once(
        self, request: BackfillIndexRequest, options: dict[str, Any]
    ) -> BackfillIndexResult: ...
    def rebuild_chunks(self, request: None, options: dict[str, Any]) -> RebuildIndexResult: ...
    def rebuild_index(self, request: None, options: dict[str, Any]) -> RebuildMemoryIndexResult: ...
    def close(self, request: None, options: dict[str, Any]) -> None: ...
    def match_phrase(
        self, request: MatchPhraseRequest, options: dict[str, Any]
    ) -> MatchPhraseResult: ...
    def ingest_messages(
        self, request: IngestMessagesRequest, options: dict[str, Any]
    ) -> IngestMessagesResult: ...
    def inject_context(
        self, request: InjectContextRequest, options: dict[str, Any]
    ) -> InjectContextResult: ...
