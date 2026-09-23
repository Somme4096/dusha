"""Deterministic golden characterization of the original affect engine.

Captured against the pre-migration engine at commit fcbbffb. Every expected value
is a hardcoded number or string; none are recomputed from migrated defaults.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from companion_gateway.affect import AffectClassificationConflict
from companion_gateway.config import AffectConfig, AppConfig, ProactiveConfig
from companion_gateway.proactive import ProactiveEngine
from companion_gateway.timeutil import isoformat, utc_now

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


def _ingest(service, *, role, content, external_id, occurred_at=NOW, conversation_id="one", **kw):
    return service.ingest_message(
        harness="astrbot", conversation_id=conversation_id, role=role, content=content,
        external_id=external_id, occurred_at=occurred_at, **kw,
    )


def test_characterization_default_spec_initial_state_and_knobs(svc, config):
    service = svc()
    assert {name: dict(vals) for name, vals in service.affect.spec.items()} == SPEC
    assert service.affect.initial_state() == {
        "base": {name: values["neutral"] for name, values in SPEC.items()},
        "mood": {name: values["neutral"] for name, values in SPEC.items()},
        "recent_labels": [],
    }
    affect = config.affect
    assert (affect.mood_follow_hours, affect.mood_return_hours) == (12.0, 72.0)
    assert (affect.habituation_window_minutes, affect.habituation_factor) == (15, 0.7)
    assert (
        affect.silence_longing_per_hour, affect.silence_anxiety_per_hour, affect.silence_seeking_per_hour
    ) == (0.04, 0.02, 0.02)
    assert affect.classification_fallback_seconds == 120
    default_proactive = ProactiveConfig()
    assert (default_proactive.longing_threshold, default_proactive.fear_threshold) == (0.48, 0.55)


def test_characterization_affectionate_applies_contact_soothing_and_label(svc):
    service = svc()
    result = service.affect.apply_label("affectionate", now=NOW, is_user_message=True)
    assert result.state["base"] == {
        "anxiety": 0.2,
        "contentment": 0.5723,
        "dejection": 0.15,
        "elation": 0.2,
        "fatigue": 0.2,
        "fear": 0.0,
        "intimacy": 0.61,
        "irritability": 0.15,
        "jealousy": 0.22,
        "longing": 0.2112,
        "lust": 0.468,
        "play": 0.25,
        "possessiveness": 0.3,
        "protectiveness": 0.25,
        "seeking": 0.23,
        "vitality": 0.5,
    }


def test_characterization_negative_label_gets_180_minute_follow_up(svc):
    service = svc()
    user = _ingest(service, role="user", content="I might die in this accident.", external_id="c1")
    _ingest(
        service, role="assistant", content="Please stay safe.", external_id="c2",
        occurred_at=NOW + timedelta(seconds=5),
    )
    with service.database.connect() as db:
        rows = db.execute("SELECT label, occurred_at, follow_up_at FROM affect_events ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [
        ("fear_death", "2026-09-06T03:00:00+00:00", "2026-09-06T06:00:05+00:00")]
    classification = service.affect.classification(user["id"])
    assert classification is not None
    assert classification["automatic_label"] == "fear_death"
    assert classification["label"] == "fear_death"
    assert classification["decision_source"] == "automatic"
    assert classification["status"] == "applied"


def test_characterization_repeated_label_habituates_with_exact_gain(svc):
    service = svc()
    first = service.affect.apply_label("fear_general", now=NOW)
    second = service.affect.apply_label("fear_general", now=NOW + timedelta(minutes=1))
    assert first.state["base"]["fear"] == 0.4
    assert second.state["base"]["fear"] == 0.5673
    assert second.state["base"]["fear"] - first.state["base"]["fear"] == 0.1673


def test_characterization_silence_caps_gate_and_rates(svc):
    service = svc()
    service.affect.apply_label("neutral", now=NOW, is_user_message=True)
    at5 = service.affect.status(now=NOW + timedelta(hours=5))
    at7 = service.affect.status(now=NOW + timedelta(hours=7))
    assert {k: at5["base"][k] for k in ("longing", "anxiety", "seeking", "dejection")} == {
        "longing": 0.4722, "anxiety": 0.3, "seeking": 0.343, "dejection": 0.15,
    }
    assert {k: at7["base"][k] for k in ("longing", "anxiety", "seeking", "dejection")} == {
        "longing": 0.5074, "anxiety": 0.3091, "seeking": 0.3479, "dejection": 0.17,
    }


def test_characterization_proactive_sent_deltas(svc):
    service = svc()
    service.affect.apply_label("neutral", now=NOW, is_user_message=True)
    service.affect.on_proactive_sent(now=NOW + timedelta(hours=1))
    after = service.affect.status(now=NOW + timedelta(hours=1))
    assert {k: after["base"][k] for k in ("longing", "seeking", "anxiety")} == {
        "longing": 0.2448, "seeking": 0.2289, "anxiety": 0.2668,
    }
    assert after["unanswered_proactive"] == 1


def test_characterization_two_timescale_decay(svc):
    service = svc()
    service.affect.apply_label("fear_death", now=NOW, is_user_message=False)
    after24 = service.affect.status(now=NOW + timedelta(hours=24))
    assert after24["base"]["fear"] == 0.5047
    assert after24["mood"]["fear"] == 0.4982
    assert after24["base"]["anxiety"] == 0.5377
    assert after24["mood"]["anxiety"] == 0.5365
    assert after24["base"]["contentment"] == 0.3178


def test_characterization_pending_fallback_survives_restart_and_applies_at_timeout(svc):
    now = utc_now() + timedelta(hours=1)
    service = svc()
    user = _ingest(service, role="user", content="I am scared.", external_id="h1", occurred_at=now)
    service.close()
    restarted = svc()
    pending = restarted.affect.classification(user["id"])
    assert pending is not None
    assert pending["automatic_label"] == "fear_general"
    assert pending["status"] == "pending"
    assert pending["label"] is None
    assert pending["finalize_after"] == isoformat(now + timedelta(seconds=120))
    assert restarted.affect.finalize_due(now + timedelta(seconds=119)) == []
    finalized = restarted.affect.finalize_due(now + timedelta(seconds=120))
    assert [{k: item[k] for k in ("label", "decision_source", "status")} for item in finalized] == [
        {"label": "fear_general", "decision_source": "automatic", "status": "applied"},
    ]


def test_characterization_prompt_selection_thresholds_and_baseline(svc, tmp_path):
    service = svc()
    service.affect.apply_label("fear_death", now=NOW, is_user_message=True)
    assert service.affect.prompt_context(now=NOW + timedelta(seconds=1)) == (
        "Affect: fear is high; irritability is noticeable; "
        "anxiety is noticeable; contentment is noticeable."
    )
    cfg = AppConfig(data_dir=tmp_path / "prompt-baseline", timezone="Asia/Taipei", affect=AffectConfig())
    assert svc(cfg=cfg).affect.prompt_context(now=NOW) == "Affect is near its usual baseline."


def test_characterization_classify_order_and_tie_behavior(svc):
    service = svc()
    expected = {
        "I am scared that you will leave me forever": "fear_separation",
        "I am worried about you. Are you safe?": "fear_concern",
        "I might die in this accident": "fear_death",
        "That was an ordinary day": "neutral",
        "I hate you and I am scared": "fear_general",
        "love you and miss you": "affectionate",
        "just kidding i love you": "affectionate",
        "I am exhausted and burned out": "struggling",
        "suicide is the only option": "fear_death",
    }
    for text, label in expected.items():
        assert service.affect.classify(text) == label


@pytest.mark.parametrize(
    ("conversation", "label", "expected", "event_reason"),
    [
        ("discord:FriendMessage:1", "fear_death", {"fear": 0.5583}, "fear"),
        ("discord:FriendMessage:2", None, {"longing": 0.4286}, None),
    ],
)
def test_characterization_proactive_default_thresholds(
    svc, tmp_path, conversation, label, expected, event_reason):
    cfg = AppConfig(
        data_dir=tmp_path / "proactive", timezone="Asia/Taipei", affect=AffectConfig(),
        proactive=ProactiveConfig(),
    )
    service = svc(cfg=cfg)
    know = datetime(2026, 9, 6, 0, 0, tzinfo=UTC)
    _ingest(service, role="user", content="hello", external_id="k1", conversation_id=conversation,
            route=conversation, occurred_at=know, affect_label="neutral")
    if label:
        service.affect.apply_label(label, now=know + timedelta(minutes=1), is_user_message=True)
    engine = ProactiveEngine(service, cfg)
    status = service.affect.status(now=know + timedelta(hours=4))
    assert {k: status["base"][k] for k in expected} == expected
    event = engine.evaluate(know + timedelta(hours=4))
    assert (event["reason"] if event else None) == event_reason


def test_fear_labels_are_distinct_and_persistent(svc):
    now = datetime(2026, 9, 6, 3, 0, tzinfo=UTC)
    service = svc()
    result = service.affect.apply_label("fear_death", now=now)
    assert result.state["base"]["fear"] >= 0.69
    assert result.state["base"]["anxiety"] > 0.2
    restarted = svc()
    persisted = restarted.affect.status(now=now)
    assert persisted["base"]["fear"] == result.state["base"]["fear"]
    later = restarted.affect.status(now=now + timedelta(hours=14))
    assert 0 < later["base"]["fear"] < persisted["base"]["fear"]


def test_agent_label_overrides_pending_automatic_label_once(svc):
    now = utc_now() + timedelta(minutes=10)
    service = svc()
    stored = _ingest(service, role="user", content="I hate you.", external_id="agent-priority",
                     occurred_at=now)
    pending = stored["affect"]["classification"]
    assert pending["automatic_label"] == "hostile"
    assert pending["status"] == "pending"
    assert pending["label"] is None
    with service.database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM affect_events").fetchone()[0] == 0
    chosen = service.affect.record_agent_label(stored["id"], "affectionate", now=now + timedelta(seconds=2))
    retry = service.affect.record_agent_label(stored["id"], "affectionate", now=now + timedelta(seconds=3))
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

    # A neutral agent label can reject a keyword false positive.
    stored = _ingest(service, role="user", content='The novel contains the quoted line "I hate you."',
                     external_id="neutral-override", occurred_at=now)
    chosen = service.affect.record_agent_label(stored["id"], "neutral", now=now)
    assert chosen["automatic_label"] == "hostile"
    assert chosen["label"] == "neutral"
    assert chosen["decision_source"] == "agent"


def test_pending_classification_survives_restart_and_expires(svc, tmp_path):
    cfg = AppConfig(
        data_dir=tmp_path / "pending", affect=AffectConfig(classification_fallback_seconds=30)
    )
    now = utc_now() + timedelta(hours=1)
    service = svc(cfg=cfg)
    user = _ingest(service, role="user", content="I am scared.", external_id="pending-restart",
                   occurred_at=now)
    service.close()
    restarted = svc(cfg=cfg)
    assert restarted.affect.classification(user["id"])["status"] == "pending"
    assert restarted.affect.finalize_due(now + timedelta(seconds=29)) == []
    finalized = restarted.affect.finalize_due(now + timedelta(seconds=30))
    assert len(finalized) == 1
    assert finalized[0]["label"] == "fear_general"
    assert finalized[0]["decision_source"] == "automatic"


def test_affect_stored_state_keeps_legacy_ascii_escaping(svc, tmp_path, write_json):
    from companion_gateway import emotions
    from companion_gateway.config import AffectConfig, AppConfig

    base = emotions.default_emotions()
    base["emotion_version"] = "unicode-label"
    base["label_deltas"]["熱い"] = {"fear": 0.1}
    base["label_patterns"]["熱い"] = ["hot"]
    path = write_json(tmp_path / "emotions.json", base)
    cfg = AppConfig(data_dir=tmp_path / "data", affect=AffectConfig(emotions_path=str(path)))
    service = svc(cfg=cfg)
    service.affect.apply_label("熱い", now=datetime(2026, 9, 6, 3, 0, tzinfo=UTC))
    with service.database.connect() as db:
        state_json = db.execute("SELECT state_json FROM affect_state WHERE id=1").fetchone()[0]
    assert "\\u71b1\\u3044" in state_json
    assert "熱い" not in state_json


def _old_user_message(service, when, external_id="u1"):
    service.ingest_message(
        harness="astrbot", conversation_id="discord-main:FriendMessage:123",
        route="discord-main:FriendMessage:123", role="user",
        content="I will be away for a while.", external_id=external_id, occurred_at=when,
        affect_label="neutral",
    )


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
    assert any(message["text"] == "Checking in." for message in restarted_service.memory.recent())


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