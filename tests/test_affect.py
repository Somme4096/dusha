from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from companion_gateway.affect import AffectClassificationConflict
from companion_gateway.service import CompanionService
from companion_gateway.timeutil import utc_now


def test_fear_labels_are_distinct_and_persistent(config):
    service = CompanionService(config)
    now = datetime(2026, 9, 6, 3, 0, tzinfo=UTC)
    result = service.affect.apply_label("fear_death", now=now)
    assert result.state["base"]["fear"] >= 0.69
    assert result.state["base"]["anxiety"] > 0.2

    restarted = CompanionService(config)
    persisted = restarted.affect.status(now=now)
    assert persisted["base"]["fear"] == result.state["base"]["fear"]

    later = restarted.affect.status(now=now + timedelta(hours=14))
    assert 0 < later["base"]["fear"] < persisted["base"]["fear"]


def test_deterministic_classifier_prioritizes_specific_fear(config):
    service = CompanionService(config)
    assert service.affect.classify("I am scared that you will leave me forever") == "fear_separation"
    assert service.affect.classify("I am worried about you. Are you safe?") == "fear_concern"
    assert service.affect.classify("I might die in this accident") == "fear_death"
    assert service.affect.classify("That was an ordinary day") == "neutral"


def test_repeated_label_habituates(config):
    service = CompanionService(config)
    now = datetime(2026, 9, 6, 3, 0, tzinfo=UTC)
    first = service.affect.apply_label("fear_general", now=now)
    second = service.affect.apply_label("fear_general", now=now + timedelta(minutes=1))
    first_gain = first.state["base"]["fear"]
    second_gain = second.state["base"]["fear"] - first.state["base"]["fear"]
    assert 0 < second_gain < first_gain


def test_agent_label_overrides_pending_automatic_label_once(config):
    service = CompanionService(config)
    now = utc_now() + timedelta(minutes=10)
    stored = service.ingest_message(
        harness="astrbot",
        conversation_id="one",
        role="user",
        content="I hate you.",
        external_id="agent-priority",
        occurred_at=now,
    )

    pending = stored["affect"]["classification"]
    assert pending["automatic_label"] == "hostile"
    assert pending["status"] == "pending"
    assert pending["label"] is None
    with service.database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM affect_events").fetchone()[0] == 0

    chosen = service.affect.record_agent_label(
        stored["id"],
        "affectionate",
        now=now + timedelta(seconds=2),
    )
    retry = service.affect.record_agent_label(
        stored["id"],
        "affectionate",
        now=now + timedelta(seconds=3),
    )

    assert chosen["automatic_label"] == "hostile"
    assert chosen["agent_label"] == "affectionate"
    assert chosen["label"] == "affectionate"
    assert chosen["decision_source"] == "agent"
    assert retry == chosen
    with service.database.connect() as db:
        events = db.execute("SELECT label, source_message_id FROM affect_events").fetchall()
    assert [tuple(event) for event in events] == [("affectionate", stored["id"])]

    with pytest.raises(AffectClassificationConflict, match="already finalized"):
        service.affect.record_agent_label(stored["id"], "hostile", now=now + timedelta(seconds=4))


def test_agent_neutral_can_reject_keyword_false_positive(config):
    service = CompanionService(config)
    now = utc_now() + timedelta(minutes=10)
    stored = service.ingest_message(
        harness="astrbot",
        conversation_id="one",
        role="user",
        content='The novel contains the quoted line "I hate you."',
        external_id="neutral-override",
        occurred_at=now,
    )

    chosen = service.affect.record_agent_label(stored["id"], "neutral", now=now)

    assert chosen["automatic_label"] == "hostile"
    assert chosen["label"] == "neutral"
    assert chosen["decision_source"] == "agent"


def test_assistant_message_finalizes_automatic_fallback(config):
    service = CompanionService(config)
    now = utc_now() + timedelta(minutes=10)
    user = service.ingest_message(
        harness="astrbot",
        conversation_id="one",
        role="user",
        content="I might die in this accident.",
        external_id="automatic-user",
        occurred_at=now,
    )
    service.ingest_message(
        harness="astrbot",
        conversation_id="one",
        role="assistant",
        content="Please contact emergency services now.",
        external_id="automatic-assistant",
        occurred_at=now + timedelta(seconds=5),
    )

    resolved = service.affect.classification(user["id"])
    assert resolved["label"] == "fear_death"
    assert resolved["decision_source"] == "automatic"
    assert resolved["status"] == "applied"


def test_pending_classification_survives_restart_and_expires(config):
    config.affect.classification_fallback_seconds = 30
    service = CompanionService(config)
    now = utc_now() + timedelta(hours=1)
    user = service.ingest_message(
        harness="astrbot",
        conversation_id="one",
        role="user",
        content="I am scared.",
        external_id="pending-restart",
        occurred_at=now,
    )
    service.close()

    restarted = CompanionService(config)
    assert restarted.affect.classification(user["id"])["status"] == "pending"
    assert restarted.affect.finalize_due(now + timedelta(seconds=29)) == []

    finalized = restarted.affect.finalize_due(now + timedelta(seconds=30))
    assert len(finalized) == 1
    assert finalized[0]["label"] == "fear_general"
    assert finalized[0]["decision_source"] == "automatic"
