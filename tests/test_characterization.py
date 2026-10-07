from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from companion_gateway.config import AffectConfig, AppConfig, DecisionConfig, ProactiveConfig
from companion_gateway.proactive import ProactiveEngine

NOW = datetime(2026, 9, 6, 3, 0, tzinfo=UTC)

SPEC = {
    "anxiety": {"floor": 0.02, "neutral": 0.2, "tau": 5},
    "contentment": {"floor": 0.06, "neutral": 0.35, "tau": 8},
    "dejection": {"floor": 0.0, "neutral": 0.15, "tau": 8},
    "elation": {"floor": 0.02, "neutral": 0.2, "tau": 3},
    "fatigue": {"floor": 0.02, "neutral": 0.2, "tau": 6},
    "fear": {"floor": 0.0, "neutral": 0.0, "tau": 7},
    "intimacy": {"floor": 0.06, "neutral": 0.35, "tau": 10},
    "irritability": {"floor": 0.0, "neutral": 0.15, "tau": 3},
    "jealousy": {"floor": 0.0, "neutral": 0.22, "tau": 2},
    "longing": {"floor": 0.15, "neutral": 0.3, "tau": 6},
    "lust": {"floor": 0.05, "neutral": 0.3, "tau": 4},
    "play": {"floor": 0.03, "neutral": 0.25, "tau": 3},
    "possessiveness": {"floor": 0.05, "neutral": 0.3, "tau": 4},
    "protectiveness": {"floor": 0.05, "neutral": 0.25, "tau": 4},
    "seeking": {"floor": 0.12, "neutral": 0.25, "tau": 4},
    "vitality": {"floor": 0.08, "neutral": 0.5, "tau": 6},
}

DEFAULT_TRIGGERS = [
    {"dimension": "fear", "threshold": 0.55, "reason": "fear"},
    {"dimension": "longing", "threshold": 0.48, "reason": "silence"},
]


def _ingest(service, *, role, content, external_id, occurred_at=NOW, conversation_id="one", **kw):
    return service.ingest_message(
        harness="astrbot", conversation_id=conversation_id, role=role, content=content,
        external_id=external_id, occurred_at=occurred_at, **kw,
    )


def _decide(service, message_id, emotion, *, now=NOW):
    decider = None if emotion is None else (lambda **_: emotion)
    return service.affect.record_user_message(
        message="x", source_message_id=message_id, decider=decider, instruction="", now=now
    )


def test_characterization_default_spec_initial_state_and_knobs(svc, config):
    service = svc()
    assert {name: dict(vals) for name, vals in service.affect.spec.items()} == SPEC
    assert service.affect.initial_state() == {
        "base": {name: values["neutral"] for name, values in SPEC.items()},
        "mood": {name: values["neutral"] for name, values in SPEC.items()},
    }
    affect = config.affect
    assert (affect.mood_follow_hours, affect.mood_return_hours) == (12.0, 72.0)
    rules = service.affect.emotions["silence"]["rules"]
    assert {name: rule["rate_per_hour"] for name, rule in rules.items()} == {
        "longing": 0.04, "anxiety": 0.02, "seeking": 0.02, "dejection": 0.01,
    }
    assert config.decision.increment == 0.1
    assert ProactiveConfig().thresholds == {}
    assert service.affect.emotions["proactive"]["triggers"] == DEFAULT_TRIGGERS


def test_characterization_decision_increment_is_exact_and_bounded(svc):
    service = svc(decision=DecisionConfig(increment=0.1))
    user = _ingest(service, role="user", content="I am scared.", external_id="d1")
    result = _decide(service, user["id"], "fear")
    assert result["emotion"] == "fear"
    assert result["increment"] == 0.1
    assert result["state"]["base"]["fear"] == 0.1
    assert result["state"]["base"]["anxiety"] == 0.2
    for _ in range(20):
        result = _decide(service, user["id"], "fear")
    assert result["state"]["base"]["fear"] == 1.0


def test_characterization_invalid_or_absent_decision_makes_no_adjustment(svc):
    service = svc()
    user = _ingest(service, role="user", content="hello", external_id="d2")
    assert _decide(service, user["id"], None) is None
    assert service.affect.status(now=NOW)["base"]["fear"] == 0.0
    assert service.affect.status(now=NOW)["base"]["contentment"] == 0.35


def test_characterization_silence_caps_gate_and_rates(svc):
    service = svc()
    user = _ingest(service, role="user", content="hello", external_id="s1")
    _decide(service, user["id"], None)
    at5 = service.affect.status(now=NOW + timedelta(hours=5))
    at7 = service.affect.status(now=NOW + timedelta(hours=7))
    assert {k: at5["base"][k] for k in ("longing", "anxiety", "seeking", "dejection")} == {
        "longing": 0.5, "anxiety": 0.3, "seeking": 0.35, "dejection": 0.15,
    }
    assert {k: at7["base"][k] for k in ("longing", "anxiety", "seeking", "dejection")} == {
        "longing": 0.5302, "anxiety": 0.3091, "seeking": 0.3531, "dejection": 0.17,
    }


def test_characterization_proactive_sent_deltas(svc):
    service = svc()
    user = _ingest(service, role="user", content="hello", external_id="p1")
    _decide(service, user["id"], None)
    service.affect.on_proactive_sent(now=NOW + timedelta(hours=1))
    after = service.affect.status(now=NOW + timedelta(hours=1))
    assert {k: after["base"][k] for k in ("longing", "seeking", "anxiety")} == {
        "longing": 0.2856, "seeking": 0.243, "anxiety": 0.2668,
    }
    assert after["unanswered_proactive"] == 1


def test_characterization_two_timescale_decay(svc):
    service = svc(decision=DecisionConfig(increment=0.7))
    user = _ingest(service, role="user", content="hello", external_id="t1")
    _decide(service, user["id"], "fear")
    after24 = service.affect.status(now=NOW + timedelta(hours=24))
    assert after24["base"]["fear"] == 0.5047
    assert after24["mood"]["fear"] == 0.4982
    assert after24["base"]["anxiety"] == 0.38
    assert after24["base"]["contentment"] == 0.35


def test_characterization_prompt_selection_thresholds_and_baseline(svc, tmp_path):
    service = svc(decision=DecisionConfig(increment=1.0))
    user = _ingest(service, role="user", content="hello", external_id="pr1")
    _decide(service, user["id"], "fear")
    assert service.affect.prompt_context(now=NOW + timedelta(seconds=1)) == (
        "Affect: fear is high."
    )
    cfg = AppConfig(data_dir=tmp_path / "prompt-baseline", timezone="Asia/Taipei", affect=AffectConfig())
    assert svc(cfg=cfg).affect.prompt_context(now=NOW) == "Affect is near its usual baseline."


def test_characterization_decision_is_persistent_and_decays(svc, tmp_path):
    cfg = AppConfig(
        data_dir=tmp_path / "persist", timezone="Asia/Taipei", affect=AffectConfig(),
        decision=DecisionConfig(increment=0.7),
    )
    service = svc(cfg=cfg)
    user = _ingest(service, role="user", content="hello", external_id="per1")
    result = _decide(service, user["id"], "fear")
    assert result["state"]["base"]["fear"] == 0.7
    restarted = svc(cfg=cfg)
    persisted = restarted.affect.status(now=NOW)
    assert persisted["base"]["fear"] == 0.7
    later = restarted.affect.status(now=NOW + timedelta(hours=14))
    assert 0 < later["base"]["fear"] < persisted["base"]["fear"]


def test_affect_stored_state_keeps_legacy_ascii_escaping(svc, tmp_path, write_json):
    from companion_gateway import emotions
    from companion_gateway.config import AffectConfig, AppConfig

    base = emotions.default_emotions()
    base["emotion_version"] = "unicode-dimension"
    base["dimensions"]["熱い"] = {"neutral": 0.0, "floor": 0.0, "tau": 5}
    path = write_json(tmp_path / "emotions.json", base)
    cfg = AppConfig(data_dir=tmp_path / "data", affect=AffectConfig(emotions_path=str(path)))
    service = svc(cfg=cfg)
    user = _ingest(service, role="user", content="hot", external_id="u1")
    _decide(service, user["id"], "熱い")
    with service.database.connect() as db:
        state_json = db.execute("SELECT state_json FROM affect_state WHERE id=1").fetchone()[0]
    assert "\\u71b1\\u3044" in state_json
    assert "熱い" not in state_json


def _old_user_message(service, when, external_id="u1"):
    service.ingest_message(
        harness="astrbot", conversation_id="discord-main:FriendMessage:123",
        route="discord-main:FriendMessage:123", role="user",
        content="I will be away for a while.", external_id=external_id, occurred_at=when,
    )


def test_characterization_proactive_default_thresholds(svc, tmp_path):
    cfg = AppConfig(
        data_dir=tmp_path / "proactive", timezone="Asia/Taipei", affect=AffectConfig(),
        proactive=ProactiveConfig(), decision=DecisionConfig(increment=1.0),
    )
    service = svc(cfg=cfg)
    know = datetime(2026, 9, 6, 0, 0, tzinfo=UTC)
    stored = _ingest(service, role="user", content="hello", external_id="k1",
                     conversation_id="discord:FriendMessage:1", route="discord:FriendMessage:1",
                     occurred_at=know)
    _decide(service, stored["id"], "fear", now=know + timedelta(minutes=1))
    engine = ProactiveEngine(service, cfg)
    event = engine.evaluate(know + timedelta(hours=4))
    assert event is not None
    assert event["reason"] == "fear"


def test_queue_lease_ack_cooldown_and_restart(svc):
    start = datetime(2026, 9, 6, 0, 0, tzinfo=UTC)
    now = start + timedelta(hours=5)
    service = svc()
    _old_user_message(service, start)
    engine = ProactiveEngine(service, service.config)
    created = engine.evaluate(now)
    assert created is not None
    assert created["target"]["route"] == "discord-main:FriendMessage:123"
    assert chr(0x2014) not in created["generation_instruction"]
    assert engine.evaluate(now) is None
    leased = engine.poll("astrbot", now=now, harness="astrbot")
    assert [item["id"] for item in leased] == [created["id"]]
    restarted_service = svc()
    restarted = ProactiveEngine(restarted_service, restarted_service.config)
    assert restarted.poll("other", now=now, harness="astrbot") == []
    restarted.acknowledge(created["id"], "astrbot", "sent", text="Checking in.", now=now)
    assert restarted.evaluate(now + timedelta(minutes=359)) is None
    state = restarted_service.affect.status(now)
    assert state["unanswered_proactive"] == 1
    assert any(message["text"] == "Checking in." for message in restarted_service._memory_fallback.recent())


def test_user_reply_resets_unanswered_limit(svc):
    start = datetime(2026, 9, 5, 0, 0, tzinfo=UTC)
    first_send = start + timedelta(hours=5)
    service = svc()
    _old_user_message(service, start)
    engine = ProactiveEngine(service, service.config)
    event = engine.evaluate(first_send)
    engine.poll("astrbot", now=first_send, harness="astrbot")
    engine.acknowledge(event["id"], "astrbot", "sent", now=first_send)
    assert engine.evaluate(first_send + timedelta(hours=12)) is None
    _old_user_message(service, first_send + timedelta(hours=12), external_id="u2")
    assert service.affect.status(first_send + timedelta(hours=12))["unanswered_proactive"] == 0


def test_quiet_hours_block_creation(svc):
    service = svc(proactive=ProactiveConfig(quiet_start_hour=1, quiet_end_hour=8))
    _old_user_message(service, datetime(2026, 9, 5, 12, 0, tzinfo=UTC))
    engine = ProactiveEngine(service, service.config)
    assert engine.evaluate(datetime(2026, 9, 5, 18, 0, tzinfo=UTC)) is None


def test_failed_delivery_waits_before_retry(svc):
    start = datetime(2026, 9, 6, 0, 0, tzinfo=UTC)
    now = start + timedelta(hours=5)
    service = svc()
    _old_user_message(service, start)
    engine = ProactiveEngine(service, service.config)
    event = engine.evaluate(now)
    assert event is not None
    assert engine.poll("astrbot", now=now, harness="astrbot")
    engine.acknowledge(event["id"], "astrbot", "failed", error="network", now=now)
    assert engine.poll("astrbot", now=now + timedelta(minutes=14), harness="astrbot") == []
    retried = engine.poll("astrbot", now=now + timedelta(minutes=15), harness="astrbot")
    assert [item["id"] for item in retried] == [event["id"]]


@pytest.mark.parametrize("emotion", ["bogus", "", "fear_death", 5, None])
def test_invalid_decider_results_make_no_adjustment(svc, emotion):
    service = svc()
    user = _ingest(service, role="user", content="hello", external_id=f"inv-{emotion}")
    assert service.affect.record_user_message(
        message="hello", source_message_id=user["id"],
        decider=lambda **_: emotion, instruction="", now=NOW,
    ) is None
    assert service.affect.status(now=NOW)["base"]["fear"] == 0.0
