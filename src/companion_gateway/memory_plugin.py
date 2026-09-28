from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

_KEEP = "__keep__"


@dataclass(frozen=True)
class IngestMessageRequest:
    harness: str
    conversation_id: str
    role: str
    content: Any
    route: str = ""
    external_id: str = ""
    occurred_at: str | None = None
    source_payload: Any = None


@dataclass(frozen=True)
class IngestMessageResult:
    id: int
    duplicate: bool
    conversation_id: int
    sha256: str


@dataclass(frozen=True)
class GetMessageRequest:
    message_id: int


@dataclass(frozen=True)
class GetMessageResult:
    message: dict[str, Any] | None


@dataclass(frozen=True)
class ConversationIdRequest:
    harness: str
    conversation_id: str


@dataclass(frozen=True)
class ConversationIdResult:
    conversation_id: int | None


@dataclass(frozen=True)
class SearchRequest:
    query: str
    limit: int = 8
    context_messages: int = 1
    exclude_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class SearchResult:
    results: list[dict[str, Any]]


@dataclass(frozen=True)
class MemoryContextRequest:
    message_id: int
    context_messages: int = 1


@dataclass(frozen=True)
class MemoryContextResult:
    messages: list[dict[str, Any]]


@dataclass(frozen=True)
class RecentMessagesRequest:
    harness: str
    conversation_id: str
    limit: int = 5
    exclude_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class RecentMessagesResult:
    messages: list[dict[str, Any]]


@dataclass(frozen=True)
class RecallRequest:
    harness: str
    conversation_id: str
    query: str
    search_hits: int = 10
    context_messages: int = 3
    exclude_ids: tuple[int, ...] = ()
    recent_messages: int = 5
    include_recent: bool = True


@dataclass(frozen=True)
class RecallResult:
    records: list[dict[str, Any]]
    search_hits: list[dict[str, Any]]


@dataclass(frozen=True)
class RebuildMemoryIndexResult:
    status: dict[str, Any]


@dataclass(frozen=True)
class RememberFactRequest:
    key: str
    text: str
    priority: int = 50
    source_message_id: int | None = None
    reason: str = ""
    review_after: str | None = None
    expires_at: str | None = None
    created_by: str = "agent"


@dataclass(frozen=True)
class RememberFactResult:
    fact: dict[str, Any]


@dataclass(frozen=True)
class ListFactsRequest:
    include_inactive: bool = False
    due_only: bool = False
    limit: int = 100


@dataclass(frozen=True)
class ListFactsResult:
    facts: list[dict[str, Any]]


@dataclass(frozen=True)
class FactHistoryRequest:
    fact_id: str


@dataclass(frozen=True)
class FactHistoryResult:
    revisions: list[dict[str, Any]]


@dataclass(frozen=True)
class ReviseFactRequest:
    fact_id: str
    expected_revision: int
    text: str
    priority: int | None = None
    source_message_id: int | None = None
    reason: str = ""
    review_after: str | None = _KEEP
    expires_at: str | None = _KEEP
    created_by: str = "agent"


@dataclass(frozen=True)
class ReviseFactResult:
    fact: dict[str, Any]


@dataclass(frozen=True)
class ForgetFactRequest:
    fact_id: str
    expected_revision: int
    reason: str
    source_message_id: int | None = None
    created_by: str = "agent"


@dataclass(frozen=True)
class ForgetFactResult:
    fact: dict[str, Any]


@dataclass(frozen=True)
class RenderFactsRequest:
    max_items: int = 10
    max_chars: int = 2000
    open_delimiter: str | None = None
    close_delimiter: str | None = None


@dataclass(frozen=True)
class RenderFactsResult:
    rendered: str
    facts: list[dict[str, Any]]


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
class EnsureMessageChunksRequest:
    message_id: int


@dataclass(frozen=True)
class EnsureMessageChunksResult:
    chunks: int


@dataclass(frozen=True)
class RebuildIndexResult:
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


@dataclass(frozen=True)
class InjectContextResult:
    text: str = ""
    records: list[dict[str, Any]] = field(default_factory=list)


class MemoryPluginError(RuntimeError):
    pass


class MemoryPlugin(Protocol):
    def ingest(self, request: IngestMessageRequest, options: dict[str, Any]) -> IngestMessageResult: ...
    def get(self, request: GetMessageRequest, options: dict[str, Any]) -> GetMessageResult: ...
    def conversation_id(self, request: ConversationIdRequest, options: dict[str, Any]) -> ConversationIdResult: ...
    def search(self, request: SearchRequest, options: dict[str, Any]) -> SearchResult: ...
    def context(self, request: MemoryContextRequest, options: dict[str, Any]) -> MemoryContextResult: ...
    def recent(self, request: RecentMessagesRequest, options: dict[str, Any]) -> RecentMessagesResult: ...
    def rebuild_index(self, request: None, options: dict[str, Any]) -> RebuildMemoryIndexResult: ...
    def recall(self, request: RecallRequest, options: dict[str, Any]) -> RecallResult: ...
    def remember(self, request: RememberFactRequest, options: dict[str, Any]) -> RememberFactResult: ...
    def list_current(self, request: ListFactsRequest, options: dict[str, Any]) -> ListFactsResult: ...
    def history(self, request: FactHistoryRequest, options: dict[str, Any]) -> FactHistoryResult: ...
    def revise(self, request: ReviseFactRequest, options: dict[str, Any]) -> ReviseFactResult: ...
    def forget(self, request: ForgetFactRequest, options: dict[str, Any]) -> ForgetFactResult: ...
    def render_facts(self, request: RenderFactsRequest, options: dict[str, Any]) -> RenderFactsResult: ...
    def status(self, request: None, options: dict[str, Any]) -> IndexStatusResult: ...
    def backfill_once(self, request: BackfillIndexRequest, options: dict[str, Any]) -> BackfillIndexResult: ...
    def ensure_message_chunks(self, request: EnsureMessageChunksRequest, options: dict[str, Any]) -> EnsureMessageChunksResult: ...
    def rebuild_chunks(self, request: None, options: dict[str, Any]) -> RebuildIndexResult: ...
    def close(self, request: None, options: dict[str, Any]) -> None: ...
    def match_phrase(self, request: MatchPhraseRequest, options: dict[str, Any]) -> MatchPhraseResult: ...
    def ingest_messages(self, request: IngestMessagesRequest, options: dict[str, Any]) -> IngestMessagesResult: ...
    def inject_context(self, request: InjectContextRequest, options: dict[str, Any]) -> InjectContextResult: ...
