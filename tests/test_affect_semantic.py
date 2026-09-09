from __future__ import annotations

import json
from unittest.mock import Mock

import httpx
import pytest

from companion_gateway.affect import LABEL_DELTAS
from companion_gateway.affect_semantic import PROTOTYPES, SemanticAppraisal
from companion_gateway.config import EmbeddingConfig
from companion_gateway.semantic import OpenAIEmbeddingClient
from companion_gateway.service import CompanionService


def appraisal_for(query):
    client = OpenAIEmbeddingClient(EmbeddingConfig())
    anchors = [[1.0, 0.0] if label == "affectionate" else [0.0, -1.0] for label in PROTOTYPES]
    client.embed = Mock(side_effect=[anchors, query])
    return SemanticAppraisal(client)


def test_similarity_strength_and_neutral_remainder():
    strong = appraisal_for([[1.0, 0.0]]).weights("You mean everything to me")
    weak = appraisal_for([[0.75, 0.6614378278]]).weights("I enjoy our chats")
    assert strong["affectionate"] == 1
    assert weak == {"affectionate": 0.5, "neutral": 0.5}
    assert appraisal_for([[0.0, 1.0]]).weights("The parcel arrives Tuesday") == {"neutral": 1}


def test_multiple_matches_are_bounded_and_anchors_cached():
    client = OpenAIEmbeddingClient(EmbeddingConfig())
    client.embed = Mock(side_effect=[[[1.0, 0.0]] * len(PROTOTYPES), [[1.0, 0.0]], [[1.0, 0.0]]])
    appraisal = SemanticAppraisal(client)
    weights = appraisal.weights("mixed message")
    assert sum(weights.values()) == pytest.approx(1)
    assert len(weights) == len(PROTOTYPES)
    assert appraisal.weights("second message") == weights
    assert client.embed.call_count == 3


def test_outage_cooldown_and_dimension_change():
    appraisal = appraisal_for([[1.0]])
    assert appraisal.weights("message") is None
    assert appraisal.weights("another message") is None
    assert appraisal.client.embed.call_count == 2
    assert appraisal._anchors == []
    client = OpenAIEmbeddingClient(EmbeddingConfig())
    client.embed = Mock(side_effect=httpx.ConnectError("offline"))
    appraisal = SemanticAppraisal(client)
    assert appraisal.weights("message") is None
    assert appraisal.weights("message") is None
    assert client.embed.call_count == 1


def test_ingestion_blends_persists_and_deduplicates(config):
    service = CompanionService(config)
    service.affect.appraisal = appraisal_for([[0.9, 0.435889894]])
    result = service.ingest_message(
        harness="test",
        conversation_id="chat",
        role="user",
        content="You make my world brighter",
        external_id="one",
    )
    assert result["affect"]["label"] == "affectionate"
    with service.database.connect() as db:
        event = db.execute("SELECT * FROM affect_events").fetchone()
    weights = json.loads(event["note"])["weights"]
    deltas = json.loads(event["deltas_json"])
    assert weights["affectionate"] == pytest.approx(0.8)
    assert deltas["intimacy"] == pytest.approx(0.8 * LABEL_DELTAS["affectionate"]["intimacy"])
    assert deltas["anxiety"] == pytest.approx(0.8 * -0.18 + 0.2 * -0.05)
    duplicate = service.ingest_message(
        harness="test",
        conversation_id="chat",
        role="user",
        content="You make my world brighter",
        external_id="one",
    )
    assert duplicate["duplicate"]
    assert service.affect.appraisal.client.embed.call_count == 2
    restarted = CompanionService(config)
    with restarted.database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM affect_events").fetchone()[0] == 1


def test_explicit_label_bypasses_embeddings_and_outage_falls_back(config):
    service = CompanionService(config)
    service.affect.appraisal = Mock()
    service.affect.appraisal.weights.return_value = None
    explicit = service.ingest_message(
        harness="test",
        conversation_id="chat",
        role="user",
        content="hello",
        affect_label="fear_concern",
    )
    assert explicit["affect"]["label"] == "fear_concern"
    service.affect.appraisal.weights.assert_not_called()
    fallback = service.ingest_message(
        harness="test",
        conversation_id="chat",
        role="user",
        content="I love you",
    )
    assert fallback["affect"]["label"] == "affectionate"


def test_semantic_wiring_independent_of_retrieval_mode(config):
    config.embedding.base_url = "http://localhost/v1"
    config.embedding.model = "test-model"
    service = CompanionService(config)
    assert service.affect.appraisal.client is service.semantic.client
    assert not service.semantic.enabled
    config.embedding.model = ""
    assert CompanionService(config).affect.appraisal is None


def test_semantic_neutral_does_not_reenter_phrase_matcher(config):
    service = CompanionService(config)
    service.affect.appraisal = appraisal_for([[0.0, 1.0]])
    result = service.ingest_message(
        harness="test",
        conversation_id="chat",
        role="user",
        content="The word 'afraid' has six letters",
    )
    assert result["affect"]["label"] == "neutral"
    assert result["affect"]["state"]["base"]["fear"] == 0


def test_negative_semantic_label_retains_follow_up(config):
    service = CompanionService(config)
    service.affect.appraisal = Mock()
    service.affect.appraisal.weights.return_value = {"struggling": 0.8, "neutral": 0.2}
    result = service.ingest_message(
        harness="test",
        conversation_id="chat",
        role="user",
        content="Everything is too much lately",
    )
    assert result["affect"]["label"] == "struggling"
    with service.database.connect() as db:
        event = db.execute("SELECT * FROM affect_events").fetchone()
    assert event["follow_up_at"] is not None
