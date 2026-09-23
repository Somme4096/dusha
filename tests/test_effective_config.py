"""Gate 1 remediation: effective configuration resolution and consumption.

Covers custom emotions.affect knobs, custom proactive thresholds, explicit
config overrides (including values equal to packaged defaults), fingerprint
consistency, override validation, and the CLI no-longer-restricting labels.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion_gateway import emotions
from companion_gateway.config import AffectConfig, AppConfig, ProactiveConfig
from companion_gateway.emotions import (
    default_fingerprint,
    fingerprint,
    load_emotions,
    resolve_emotions,
)
from companion_gateway.proactive import ProactiveEngine
from companion_gateway.service import CompanionService
from companion_gateway.timeutil import isoformat

NOW = datetime(2026, 9, 6, 3, 0, tzinfo=UTC)


def _custom_emotions(tmp_path, name: str, mutations) -> Path:
    base = emotions.default_emotions()
    base["emotion_version"] = "gate1-custom"
    mutations(base)
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    return path


def _config_with_emotions(tmp_path, path: Path, data_dir: str = "data") -> Path:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"data_dir": data_dir, "emotions": {"path": str(path)}}),
        encoding="utf-8",
    )
    return config


def test_custom_emotions_affect_knobs_consumed(tmp_path):
    path = _custom_emotions(
        tmp_path, "knobs",
        lambda base: base["affect"].update(
            {
                "mood_follow_hours": 2.0,
                "mood_return_hours": 8.0,
                "habituation_window_minutes": 3,
                "habituation_factor": 0.5,
                "silence_longing_per_hour": 0.01,
                "silence_anxiety_per_hour": 0.005,
                "silence_seeking_per_hour": 0.005,
                "classification_fallback_seconds": 40,
            }
        )
    )
    cfg = AppConfig(data_dir=tmp_path / "a", affect=AffectConfig(emotions_path=str(path)))
    service = CompanionService(cfg)
    assert service.affect.emotions["affect"] == {
        "mood_follow_hours": 2.0,
        "mood_return_hours": 8.0,
        "habituation_window_minutes": 3,
        "habituation_factor": 0.5,
        "silence_longing_per_hour": 0.01,
        "silence_anxiety_per_hour": 0.005,
        "silence_seeking_per_hour": 0.005,
        "classification_fallback_seconds": 40,
    }
    # classification_fallback_seconds is consumed by the engine.
    user = service.ingest_message(
        harness="astrbot",
        conversation_id="one",
        role="user",
        content="I am scared.",
        external_id="k1",
        occurred_at=NOW,
    )
    pending = service.affect.classification(user["id"])
    assert pending is not None
    assert pending["finalize_after"] == isoformat(NOW + timedelta(seconds=40))
    service.close()


def test_custom_silence_rate_and_habituation_change_behavior(tmp_path):
    path = _custom_emotions(
        tmp_path, "silence",
        lambda base: base["affect"].update(
            {"silence_longing_per_hour": 0.01, "habituation_factor": 0.5}
        )
    )

    def silence_longing(affect_cfg) -> float:
        service = CompanionService(
            AppConfig(data_dir=tmp_path / "s", affect=affect_cfg)
        )
        service.affect.apply_label("neutral", now=NOW, is_user_message=True)
        value = service.affect.status(now=NOW + timedelta(hours=4))["base"]["longing"]
        service.close()
        return value

    default_silence = silence_longing(AffectConfig())
    custom_silence = silence_longing(AffectConfig(emotions_path=str(path)))
    assert custom_silence < default_silence

    def second_gain(affect_cfg) -> float:
        service = CompanionService(
            AppConfig(data_dir=tmp_path / "h", affect=affect_cfg)
        )
        first = service.affect.apply_label("fear_general", now=NOW)
        second = service.affect.apply_label("fear_general", now=NOW + timedelta(minutes=1))
        gain = second.state["base"]["fear"] - first.state["base"]["fear"]
        service.close()
        return gain

    default_gain = second_gain(AffectConfig())
    custom_gain = second_gain(AffectConfig(emotions_path=str(path)))
    assert 0 < custom_gain < default_gain


def test_custom_impact_scale_and_follow_up_expiry_consumed(tmp_path):
    path = _custom_emotions(
        tmp_path, "impact",
        lambda base: base.update(impact_scale=1.0, follow_up_expiration_hours=6)
    )
    cfg = AppConfig(data_dir=tmp_path / "a", affect=AffectConfig(emotions_path=str(path)))
    service = CompanionService(cfg)
    first = service.affect.apply_label("fear_general", now=NOW)
    assert first.state["base"]["fear"] == 0.2  # 0.2 * impact 1.0 * (1 - 0)

    service.ingest_message(
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
        row = db.execute(
            "SELECT follow_up_at, follow_up_expires_at FROM affect_events ORDER BY id DESC"
        ).fetchone()
    assert row["follow_up_at"] == isoformat(NOW + timedelta(seconds=5, minutes=180))
    assert row["follow_up_expires_at"] == isoformat(NOW + timedelta(seconds=5, hours=6))
    service.close()


def test_custom_mood_follow_gain_changes_decay(tmp_path):
    path = _custom_emotions(
        tmp_path, "gain",
        lambda base: base.update(mood_follow_gain={"min": 0.05, "max": 0.5, "factor": 1.0})
    )

    def fear_mood(affect_cfg) -> float:
        service = CompanionService(
            AppConfig(data_dir=tmp_path / "g", affect=affect_cfg)
        )
        service.affect.apply_label("fear_death", now=NOW, is_user_message=False)
        value = service.affect.status(now=NOW + timedelta(hours=24))["mood"]["fear"]
        service.close()
        return value

    assert fear_mood(AffectConfig(emotions_path=str(path))) != fear_mood(AffectConfig())


def test_custom_proactive_thresholds_change_evaluate(tmp_path):
    path = _custom_emotions(
        tmp_path, "proactive",
        lambda base: base.update(
            proactive={"longing_threshold": 0.2, "fear_threshold": 0.9}
        )
    )

    def evaluate_reason(tag: int, proactive_cfg, affect_cfg) -> str | None:
        cfg = AppConfig(
            data_dir=tmp_path / f"p{tag}",
            affect=affect_cfg,
            proactive=proactive_cfg,
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
        service.affect.apply_label(
            "fear_death", now=know + timedelta(minutes=1), is_user_message=True
        )
        engine = ProactiveEngine(service, cfg)
        event = engine.evaluate(know + timedelta(hours=4))
        service.close()
        return event["reason"] if event else None

    # Default thresholds: fear 0.5583 >= 0.55 -> fear reason.
    assert evaluate_reason(1, ProactiveConfig(), AffectConfig()) == "fear"
    # Custom thresholds: fear below 0.9, longing 0.4286 >= 0.2 -> silence reason.
    assert evaluate_reason(2, ProactiveConfig(), AffectConfig(emotions_path=str(path))) == "silence"

    # Custom high longing threshold blocks the event (fear below its threshold).
    high_longing = _custom_emotions(
        tmp_path, "longing-gate",
        lambda base: base.update(proactive={"longing_threshold": 0.5, "fear_threshold": 0.9})
    )
    assert evaluate_reason(3, ProactiveConfig(), AffectConfig(emotions_path=str(high_longing))) is None


def test_explicit_override_wins_over_custom_file_even_when_equal_to_default(tmp_path):
    path = _custom_emotions(
        tmp_path, "override",
        lambda base: base["affect"].update(mood_follow_hours=24.0, classification_fallback_seconds=90)
    )
    # Programmatic explicit override equal to the packaged default.
    cfg = AppConfig(
        data_dir=tmp_path / "a",
        affect=AffectConfig(emotions_path=str(path), mood_follow_hours=12.0),
    )
    service = CompanionService(cfg)
    assert service.affect.emotions["affect"]["mood_follow_hours"] == 12.0
    assert service.affect.emotions["affect"]["classification_fallback_seconds"] == 90
    service.close()

    # Config-file explicit override equal to the packaged default.
    config = _config_with_emotions(tmp_path, path)
    config.write_text(
        json.dumps(
            {
                "data_dir": "data",
                "emotions": {"path": str(path)},
                "affect": {"mood_follow_hours": 12.0},
            }
        ),
        encoding="utf-8",
    )
    from companion_gateway.config import load_config

    cfg = load_config(config)
    service = CompanionService(cfg)
    assert service.affect.emotions["affect"]["mood_follow_hours"] == 12.0
    service.close()


def test_proactive_explicit_override_wins_over_custom_file(tmp_path):
    path = _custom_emotions(
        tmp_path, "proactive-2",
        lambda base: base.update(proactive={"longing_threshold": 0.2, "fear_threshold": 0.9})
    )
    cfg = AppConfig(
        data_dir=tmp_path / "a",
        affect=AffectConfig(emotions_path=str(path)),
        proactive=ProactiveConfig(fear_threshold=0.55),
    )
    service = CompanionService(cfg)
    engine = ProactiveEngine(service, cfg)
    assert engine._emotional["fear_threshold"] == 0.55
    assert engine._emotional["longing_threshold"] == 0.2
    service.close()


def test_fingerprints_differ_when_effective_behavior_differs(tmp_path):
    path = _custom_emotions(tmp_path, "custom", lambda base: None)

    def fingerprint_for(affect_cfg) -> str:
        service = CompanionService(
            AppConfig(data_dir=tmp_path / "f", affect=affect_cfg)
        )
        value = service.affect.emotions_fingerprint
        service.close()
        return value

    fp_custom = fingerprint_for(AffectConfig(emotions_path=str(path)))
    fp_override = fingerprint_for(AffectConfig(emotions_path=str(path), mood_follow_hours=6.0))
    fp_default = fingerprint_for(AffectConfig())
    assert fp_custom != default_fingerprint()
    assert fp_override != fp_custom
    assert fp_default == default_fingerprint()
    # Proactive overrides fold into the fingerprint at assembly time.
    cfg = AppConfig(
        data_dir=tmp_path / "f2",
        affect=AffectConfig(),
        proactive=ProactiveConfig(fear_threshold=0.7),
    )
    service = CompanionService(cfg)
    engine = ProactiveEngine(service, cfg)
    assert engine._emotional["fear_threshold"] == 0.7
    assert service.affect.emotions_fingerprint != default_fingerprint()
    service.close()


def test_dimension_override_unknown_key_rejected(tmp_path):
    from companion_gateway.affect import AffectEngine
    from companion_gateway.database import Database

    database = Database(tmp_path / "state.sqlite3")
    with pytest.raises(ValueError, match="unknown field"):
        AffectEngine(
            database,
            AffectConfig(dimensions={"fear": {"neutral": 0.1, "bogus": 2}}),
        )


def test_programmatic_affect_knob_bounds_validated(tmp_path):
    cfg = AppConfig(data_dir=tmp_path, affect=AffectConfig(mood_follow_hours=0))
    with pytest.raises(ValueError, match="mood_follow_hours must be positive"):
        CompanionService(cfg)


def test_programmatic_proactive_threshold_bounds_validated(tmp_path):
    cfg = AppConfig(
        data_dir=tmp_path,
        affect=AffectConfig(),
        proactive=ProactiveConfig(fear_threshold=5.0),
    )
    service = CompanionService(cfg)
    with pytest.raises(ValueError, match="within value_range"):
        ProactiveEngine(service, cfg)


def test_value_range_must_be_unit_range(tmp_path):
    snapshot = emotions.default_emotions()
    snapshot["value_range"] = {"min": 0.0, "max": 2.0}
    path = tmp_path / "emotions.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        load_emotions(path)


def test_extracted_tuning_defaults_present():
    snapshot = emotions.default_emotions()
    assert snapshot["impact_scale"] == 2.0
    assert snapshot["mood_follow_gain"] == {"min": 0.25, "max": 2.5, "factor": 4.0}
    assert snapshot["follow_up_expiration_hours"] == 24


def test_cli_affect_event_accepts_custom_label(monkeypatch, tmp_path, capsys):
    from companion_gateway import cli

    base = emotions.default_emotions()
    base["emotion_version"] = "cli-label"
    base["label_deltas"]["gladness"] = {"contentment": 0.1}
    base["label_patterns"]["gladness"] = ["wonderful"]
    path = tmp_path / "emotions.json"
    path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"data_dir": "data", "emotions": {"path": str(path)}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["companion-gateway", "--config", str(config), "affect", "event", "gladness"],
    )
    cli.main()
    out = capsys.readouterr().out
    assert '"label": "gladness"' in out


def test_cli_affect_event_invalid_label_errors(monkeypatch, tmp_path, capsys):
    from companion_gateway import cli

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_dir": "data"}), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["companion-gateway", "--config", str(config), "affect", "event", "bogus-label"],
    )
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "unsupported affect label" in err


def test_cli_affect_event_has_no_label_choices():
    from companion_gateway.cli import parser

    args = parser().parse_args(["affect", "event", "not-a-default-label"])
    assert args.label == "not-a-default-label"


def test_packaged_defaults_schema_validation_rejects_bad_shape():
    from companion_gateway.config import MemoryConfig, _strict_section

    with pytest.raises(ValueError, match="recent_messages must be int"):
        _strict_section(MemoryConfig, {"recent_messages": "not-an-int"}, "defaults memory")


def test_custom_emotions_unknown_section_keys_still_rejected(tmp_path):
    snapshot = emotions.default_emotions()
    snapshot["bogus_section"] = {}
    path = tmp_path / "emotions.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown emotions field"):
        load_emotions(path)


def test_resolve_emotions_returns_validated_proactive_section(tmp_path):
    base = emotions.default_emotions()
    base["emotion_version"] = "resolve"
    path = tmp_path / "emotions.json"
    path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    resolved = resolve_emotions(AffectConfig(emotions_path=str(path)), ProactiveConfig())
    assert resolved["proactive"] == {"longing_threshold": 0.48, "fear_threshold": 0.55}
    assert fingerprint(resolved) != default_fingerprint()