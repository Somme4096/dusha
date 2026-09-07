from __future__ import annotations

from datetime import UTC, datetime, timedelta

from companion_gateway.service import CompanionService


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
