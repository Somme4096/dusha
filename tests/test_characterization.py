"""Deterministic golden characterization of the original affect engine.

Captured against the pre-migration engine at commit fcbbffb. Every expected value
here is a hardcoded number or string; none are recomputed from migrated defaults.
These tests lock numeric behavior before the constants move to packaged JSON.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from companion_gateway.config import AffectConfig, AppConfig, ProactiveConfig
from companion_gateway.proactive import ProactiveEngine
from companion_gateway.service import CompanionService
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


def test_characterization_default_spec_initial_state_and_knobs(config):
    service = CompanionService(config)
    assert {name: dict(vals) for name, vals in service.affect.spec.items()} == SPEC
    assert service.affect.initial_state() == {
        "base": {name: values["neutral"] for name, values in SPEC.items()},
        "mood": {name: values["neutral"] for name, values in SPEC.items()},
        "recent_labels": [],
    }
    affect = config.affect
    assert affect.mood_follow_hours == 12.0
    assert affect.mood_return_hours == 72.0
    assert affect.habituation_window_minutes == 15
    assert affect.habituation_factor == 0.7
    assert affect.silence_longing_per_hour == 0.04
    assert affect.silence_anxiety_per_hour == 0.02
    assert affect.silence_seeking_per_hour == 0.02
    assert affect.classification_fallback_seconds == 120
    default_proactive = ProactiveConfig()
    assert default_proactive.longing_threshold == 0.48
    assert default_proactive.fear_threshold == 0.55
    service.close()


def test_characterization_affectionate_applies_contact_soothing_and_label(config):
    service = CompanionService(config)
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
    service.close()


def test_characterization_negative_label_gets_180_minute_follow_up(config):
    service = CompanionService(config)
    user = service.ingest_message(
        harness="astrbot",
        conversation_id="one",
        role="user",
        content="I might die in this accident.",
        external_id="c1",
        occurred_at=NOW,
    )
    service.ingest_message(
        harness="astrbot",
        conversation_id="one",
        role="assistant",
        content="Please stay safe.",
        external_id="c2",
        occurred_at=NOW + timedelta(seconds=5),
    )
    with service.database.connect() as db:
        rows = db.execute(
            "SELECT label, occurred_at, follow_up_at FROM affect_events ORDER BY id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("fear_death", "2026-09-06T03:00:00+00:00", "2026-09-06T06:00:05+00:00")
    ]
    classification = service.affect.classification(user["id"])
    assert classification is not None
    assert classification["automatic_label"] == "fear_death"
    assert classification["label"] == "fear_death"
    assert classification["decision_source"] == "automatic"
    assert classification["status"] == "applied"
    service.close()


def test_characterization_repeated_label_habituates_with_exact_gain(config):
    service = CompanionService(config)
    first = service.affect.apply_label("fear_general", now=NOW)
    second = service.affect.apply_label("fear_general", now=NOW + timedelta(minutes=1))
    assert first.state["base"]["fear"] == 0.4
    assert second.state["base"]["fear"] == 0.5673
    assert first.state["base"]["fear"] == 0.4
    assert second.state["base"]["fear"] - first.state["base"]["fear"] == 0.1673
    service.close()


def test_characterization_silence_caps_gate_and_rates(config):
    service = CompanionService(config)
    service.affect.apply_label("neutral", now=NOW, is_user_message=True)
    at5 = service.affect.status(now=NOW + timedelta(hours=5))
    at7 = service.affect.status(now=NOW + timedelta(hours=7))
    assert {k: at5["base"][k] for k in ("longing", "anxiety", "seeking", "dejection")} == {
        "longing": 0.4722,
        "anxiety": 0.3,
        "seeking": 0.343,
        "dejection": 0.15,
    }
    assert {k: at7["base"][k] for k in ("longing", "anxiety", "seeking", "dejection")} == {
        "longing": 0.5074,
        "anxiety": 0.3091,
        "seeking": 0.3479,
        "dejection": 0.17,
    }
    service.close()


def test_characterization_proactive_sent_deltas(config):
    service = CompanionService(config)
    service.affect.apply_label("neutral", now=NOW, is_user_message=True)
    service.affect.on_proactive_sent(now=NOW + timedelta(hours=1))
    after = service.affect.status(now=NOW + timedelta(hours=1))
    assert {k: after["base"][k] for k in ("longing", "seeking", "anxiety")} == {
        "longing": 0.2448,
        "seeking": 0.2289,
        "anxiety": 0.2668,
    }
    assert after["unanswered_proactive"] == 1
    service.close()


def test_characterization_two_timescale_decay(config):
    service = CompanionService(config)
    service.affect.apply_label("fear_death", now=NOW, is_user_message=False)
    after24 = service.affect.status(now=NOW + timedelta(hours=24))
    assert after24["base"]["fear"] == 0.5047
    assert after24["mood"]["fear"] == 0.4982
    assert after24["base"]["anxiety"] == 0.5377
    assert after24["mood"]["anxiety"] == 0.5365
    assert after24["base"]["contentment"] == 0.3178
    service.close()


def test_characterization_pending_fallback_survives_restart_and_applies_at_timeout(config):
    service = CompanionService(config)
    now = utc_now() + timedelta(hours=1)
    user = service.ingest_message(
        harness="astrbot",
        conversation_id="one",
        role="user",
        content="I am scared.",
        external_id="h1",
        occurred_at=now,
    )
    service.close()
    restarted = CompanionService(config)
    pending = restarted.affect.classification(user["id"])
    assert pending is not None
    assert pending["automatic_label"] == "fear_general"
    assert pending["status"] == "pending"
    assert pending["label"] is None
    assert pending["finalize_after"] == isoformat(now + timedelta(seconds=120))
    assert restarted.affect.finalize_due(now + timedelta(seconds=119)) == []
    finalized = restarted.affect.finalize_due(now + timedelta(seconds=120))
    assert [item["label"] for item in finalized] == ["fear_general"]
    assert [item["decision_source"] for item in finalized] == ["automatic"]
    assert [item["status"] for item in finalized] == ["applied"]
    restarted.close()


def test_characterization_prompt_selection_thresholds(config):
    service = CompanionService(config)
    service.affect.apply_label("fear_death", now=NOW, is_user_message=True)
    assert service.affect.prompt_context(now=NOW + timedelta(seconds=1)) == (
        "Affect: fear is high; irritability is noticeable; "
        "anxiety is noticeable; contentment is noticeable."
    )
    service.close()


def test_characterization_prompt_baseline(tmp_path):
    cfg = AppConfig(
        data_dir=tmp_path / "prompt-baseline",
        timezone="Asia/Taipei",
        affect=AffectConfig(),
    )
    service = CompanionService(cfg)
    assert service.affect.prompt_context(now=NOW) == "Affect is near its usual baseline."
    service.close()


def test_characterization_classify_order_and_tie_behavior(config):
    service = CompanionService(config)
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
    service.close()


def test_characterization_proactive_default_fear_threshold(config, tmp_path):
    cfg = AppConfig(
        data_dir=tmp_path / "fear-threshold",
        timezone="Asia/Taipei",
        affect=AffectConfig(),
        proactive=ProactiveConfig(),
    )
    service = CompanionService(cfg)
    know = datetime(2026, 9, 6, 0, 0, tzinfo=UTC)
    service.ingest_message(
        harness="astrbot",
        conversation_id="discord:FriendMessage:1",
        route="discord:FriendMessage:1",
        role="user",
        content="hello",
        external_id="k1",
        occurred_at=know,
        affect_label="neutral",
    )
    service.affect.apply_label("fear_death", now=know + timedelta(minutes=1), is_user_message=True)
    engine = ProactiveEngine(service, cfg)
    assert service.affect.status(now=know + timedelta(hours=4))["base"]["fear"] == 0.5583
    event = engine.evaluate(know + timedelta(hours=4))
    assert event is not None
    assert event["reason"] == "fear"
    service.close()


def test_characterization_proactive_default_longing_below_threshold(config, tmp_path):
    cfg = AppConfig(
        data_dir=tmp_path / "longing-threshold",
        timezone="Asia/Taipei",
        affect=AffectConfig(),
        proactive=ProactiveConfig(),
    )
    service = CompanionService(cfg)
    know = datetime(2026, 9, 6, 0, 0, tzinfo=UTC)
    service.ingest_message(
        harness="astrbot",
        conversation_id="discord:FriendMessage:2",
        route="discord:FriendMessage:2",
        role="user",
        content="hello",
        external_id="k2",
        occurred_at=know,
        affect_label="neutral",
    )
    engine = ProactiveEngine(service, cfg)
    assert service.affect.status(now=know + timedelta(hours=4))["base"]["longing"] == 0.4286
    assert engine.evaluate(know + timedelta(hours=4)) is None
    service.close()