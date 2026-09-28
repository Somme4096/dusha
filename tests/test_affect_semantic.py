from __future__ import annotations

from unittest.mock import Mock

import httpx
import pytest

from companion_gateway.affect_semantic import PROTOTYPES, SemanticAppraisal
from companion_gateway.config import EmbeddingConfig
from companion_gateway.embedding import OpenAIEmbeddingClient
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


def test_semantic_appraisal_wiring_degrades_and_activates(config):
    assert CompanionService(config).affect.appraisal is None
    config.embedding.base_url = "http://localhost/v1"
    config.embedding.model = "test-model"
    assert isinstance(CompanionService(config).affect.appraisal, SemanticAppraisal)


def test_semantic_appraisal_drives_dimension(config):
    service = CompanionService(config)
    service.affect.appraisal = appraisal_for([[1.0, 0.0]])
    result = service.ingest_message(
        harness="test",
        conversation_id="chat",
        role="user",
        content="You make my world brighter",
        external_id="one",
    )
    assert result["affect"]["emotion"] == "intimacy"
    with service.database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM affect_decisions").fetchone()[0] == 1
    duplicate = service.ingest_message(
        harness="test",
        conversation_id="chat",
        role="user",
        content="You make my world brighter",
        external_id="one",
    )
    assert duplicate["duplicate"]
    assert service.affect.appraisal.client.embed.call_count == 2


def test_neutral_semantic_weights_leave_fear_untouched(config):
    service = CompanionService(config)
    service.affect.appraisal = appraisal_for([[0.0, 1.0]])
    result = service.ingest_message(
        harness="test",
        conversation_id="chat",
        role="user",
        content="The parcel arrives Tuesday",
        external_id="neutral-1",
    )
    assert result["affect"]["emotion"] == "contentment"
    assert service.affect.status()["base"]["fear"] == 0


def test_decision_plugin_result_wins_over_appraisal(config):
    service = CompanionService(config)
    service.affect.appraisal = Mock()
    service.affect.appraisal.weights.return_value = {"affectionate": 1.0}
    result = service.affect.record_user_message(
        message="x", source_message_id=None, decider=lambda **_: "fear", instruction=""
    )
    assert result["emotion"] == "fear"
    service.affect.appraisal.weights.assert_not_called()
