from __future__ import annotations

import json

import httpx

from companion_gateway.config import EmbeddingConfig, load_config
from companion_gateway.memory import MemoryStore
from companion_gateway.semantic import OpenAIEmbeddingClient, SemanticIndex
from companion_gateway.service import CompanionService


class FakeEmbeddingClient:
    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            lowered = text.casefold()
            if "brass key" in lowered or "locate keepsake" in lowered:
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return vectors

    def close(self) -> None:
        pass


class FailingEmbeddingClient:
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise httpx.TimeoutException("embedding request timed out")

    def close(self) -> None:
        pass


def _enable_hybrid(config, model: str = "test-embedding") -> None:
    config.memory.retrieval_mode = "hybrid"
    config.memory.chunk_max_chars = 64
    config.memory.chunk_overlap_chars = 16
    config.memory.lexical_candidates = 8
    config.memory.semantic_candidates = 8
    config.embedding = EmbeddingConfig(
        base_url="https://embedding.invalid/v1",
        model=model,
        dimensions=2,
        batch_size=16,
        failure_cooldown_seconds=60,
    )


def test_child_offsets_are_deterministic_and_reconstruct_canonical_text(config):
    _enable_hybrid(config)
    service = CompanionService(config)
    text = "0123456789" * 15
    stored = service.ingest_message(
        harness="test",
        conversation_id="one",
        role="user",
        content=text,
        affect_label="neutral",
    )

    assert SemanticIndex.chunk_offsets(text, 64, 16) == [(0, 64), (48, 112), (96, 150)]
    with service.database.connect() as db:
        chunks = db.execute(
            """SELECT start_char, end_char FROM memory_chunks
               WHERE message_id=? AND chunker_key=? ORDER BY ordinal""",
            (stored["id"], service.semantic.chunker_key),
        ).fetchall()
    assert [text[row["start_char"] : row["end_char"]] for row in chunks] == [
        text[0:64],
        text[48:112],
        text[96:150],
    ]
    assert service.memory.get(stored["id"])["text"] == text


def test_semantic_child_search_returns_canonical_parent_after_restart(config):
    _enable_hybrid(config)
    service = CompanionService(config)
    target = "The brass key rests beneath the third flowerpot. Exact original wording."
    stored = service.ingest_message(
        harness="test",
        conversation_id="one",
        role="user",
        content=target,
        affect_label="neutral",
    )
    service.ingest_message(
        harness="test",
        conversation_id="two",
        role="user",
        content="The ocean is calm today.",
        affect_label="neutral",
    )
    service.semantic.client = FakeEmbeddingClient()
    backfill = service.semantic.backfill_once(force=True)
    assert backfill["embedded"] == 3

    restarted = CompanionService(config)
    restarted.semantic.client = FakeEmbeddingClient()
    results = restarted.memory.search("locate keepsake", limit=2, context_messages=0)

    assert results[0]["hit_id"] == stored["id"]
    assert results[0]["messages"][0]["text"] == target
    assert results[0]["retrieval"]["method"] == "hybrid"
    assert results[0]["retrieval"]["semantic_similarity"] == 1.0


def test_embedding_failure_falls_back_to_lexical_search(config):
    _enable_hybrid(config)
    service = CompanionService(config)
    stored = service.ingest_message(
        harness="test",
        conversation_id="one",
        role="user",
        content="Remember the violet telescope.",
        affect_label="neutral",
    )
    service.semantic.client = FakeEmbeddingClient()
    assert service.semantic.backfill_once(force=True)["embedded"] == 1
    service.semantic.client = FailingEmbeddingClient()

    results = service.memory.search("violet telescope", limit=2, context_messages=0)

    assert results[0]["hit_id"] == stored["id"]
    assert results[0]["messages"][0]["text"] == "Remember the violet telescope."
    assert service.semantic.status()["cooling_down"] is True
    assert service.semantic.status()["last_error"] == "embedding request timed out"


def test_model_change_uses_a_new_disposable_embedding_key(config):
    _enable_hybrid(config, model="model-a")
    first = CompanionService(config)
    first.ingest_message(
        harness="test",
        conversation_id="one",
        role="user",
        content="The brass key is safe.",
        affect_label="neutral",
    )
    first.semantic.client = FakeEmbeddingClient()
    assert first.semantic.backfill_once(force=True)["embedded"] == 1
    old_key = first.semantic.embedding_key

    _enable_hybrid(config, model="model-b")
    second = CompanionService(config)
    second.semantic.client = FakeEmbeddingClient()
    assert second.semantic.embedding_key != old_key
    assert second.semantic.status()["embedded_chunks"] == 0
    assert second.semantic.backfill_once(force=True)["embedded"] == 1
    assert second.memory.recent(limit=1)[0]["text"] == "The brass key is safe."


def test_openai_embedding_client_sends_configured_request(monkeypatch):
    monkeypatch.setenv("TEST_EMBEDDING_KEY", "secret")
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 2.0]},
                    {"index": 0, "embedding": [3.0, 0.0]},
                ]
            },
        )

    client = OpenAIEmbeddingClient(
        EmbeddingConfig(
            base_url="https://embedding.invalid/v1/",
            api_key_env="TEST_EMBEDDING_KEY",
            model="chosen-model",
            dimensions=2,
        )
    )
    client._client = httpx.Client(transport=httpx.MockTransport(handler))

    vectors = client.embed(["first", "second"])

    assert captured == {
        "url": "https://embedding.invalid/v1/embeddings",
        "authorization": "Bearer secret",
        "body": {
            "model": "chosen-model",
            "input": ["first", "second"],
            "encoding_format": "float",
            "dimensions": 2,
        },
    }
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    client.close()


def test_nested_embedding_configuration_loads_from_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """memory:
  retrieval_mode: hybrid
  chunk_max_chars: 900
  embedding:
    base_url: https://embedding.invalid/v1
    api_key_env: CUSTOM_KEY
    model: chosen-model
    dimensions: 768
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.memory.retrieval_mode == "hybrid"
    assert config.memory.chunk_max_chars == 900
    assert config.embedding.base_url == "https://embedding.invalid/v1"
    assert config.embedding.api_key_env == "CUSTOM_KEY"
    assert config.embedding.model == "chosen-model"
    assert config.embedding.dimensions == 768


def test_reciprocal_rank_fusion_rewards_both_retrievers():
    lexical = [{"id": 1, "rank": -2.0}, {"id": 2, "rank": -1.0}]
    semantic = [
        {"message_id": 2, "similarity": 0.9},
        {"message_id": 3, "similarity": 0.8},
    ]

    results = MemoryStore._fuse_candidates(lexical, semantic, rrf_k=60)
    assert [item["id"] for item in results] == [2, 1, 3]
    assert results[0]["rank"] == -results[0]["fusion_score"]


def test_shared_similarity_cutoff_filters_weak_memory_matches(config):
    from unittest.mock import Mock

    _enable_hybrid(config)
    service = CompanionService(config)
    service.ingest_message(
        harness="test", conversation_id="one", role="user", content="A keepsake", affect_label="neutral"
    )
    service.semantic.client = Mock()
    service.semantic.client.embed.return_value = [[1.0, 0.0]]
    assert service.semantic.backfill_once(force=True)["embedded"] == 1
    service.semantic.client.embed.return_value = [[0.4, (1 - 0.4**2) ** 0.5]]
    assert service.semantic.semantic_candidates("weak", 4) == []
    service.semantic.client.embed.return_value = [[0.6, 0.8]]
    assert len(service.semantic.semantic_candidates("strong", 4)) == 1
