from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class EmbeddingConfig:
    base_url: str = ""
    api_key_env: str = "EMBEDDING_API_KEY"
    model: str = ""
    dimensions: int | None = None
    timeout_seconds: float = 10.0
    batch_size: int = 32
    backfill_interval_seconds: int = 10
    failure_cooldown_seconds: int = 60


@dataclass(slots=True)
class MemoryConfig:
    recent_messages: int = 8
    search_hits: int = 4
    context_messages: int = 1
    injection_max_chars: int = 12000
    retrieval_mode: str = "lexical"
    child_chars: int = 800
    child_overlap_chars: int = 120
    lexical_candidates: int = 24
    semantic_candidates: int = 24
    rrf_k: int = 60
    semantic_min_similarity: float = 0.3
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
