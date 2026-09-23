from __future__ import annotations

import json

import pytest

from companion_gateway.emotions import (
    SCHEMA_VERSION,
    canonical,
    default_emotions,
    default_fingerprint,
    fingerprint,
    load_emotions,
)


def test_default_emotions_validate_and_fingerprint_is_stable():
    snapshot = default_emotions()
    assert snapshot["schema_version"] == SCHEMA_VERSION
    assert snapshot["emotion_version"] == "0.1.0"
    assert len(snapshot["dimensions"]) == 16
    assert len(snapshot["label_deltas"]) == 16
    assert len(snapshot["label_patterns"]) == 14
    assert fingerprint(snapshot) == default_fingerprint()
    assert fingerprint(default_emotions()) == fingerprint(snapshot)
    assert canonical(snapshot) == canonical(default_emotions())


def test_default_emotions_return_fresh_copies():
    first = default_emotions()
    second = default_emotions()
    first["dimensions"]["fear"]["neutral"] = 0.9
    assert second["dimensions"]["fear"]["neutral"] == 0.0


def _write(tmp_path, mutation) -> str:
    snapshot = default_emotions()
    mutation(snapshot)
    path = tmp_path / "emotions.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda s: s.update(schema_version=2), "schema_version"),
        (lambda s: s.update(emotion_version=""), "emotion_version"),
        (lambda s: s["label_deltas"]["neutral"].update(bogus=0.1), "unknown dimension"),
        (lambda s: s["contact_deltas"].update(bogus=0.1), "unknown dimension"),
        (lambda s: s["dimensions"]["fear"].update(floor=0.5), "floor must be <="),
        (lambda s: s["dimensions"]["fear"].update(tau=0), "tau must be positive"),
        (lambda s: s["dimensions"]["fear"].update(tau=-3), "tau must be positive"),
        (lambda s: s["dimensions"]["fear"].update(neutral=True), "finite number"),
        (lambda s: s["dimensions"].pop("fear"), "unknown dimension"),
        (lambda s: s["label_patterns"].update(bogus=["x"]), "unknown label"),
        (lambda s: s["label_patterns"].update(neutral=[]), "non-empty array"),
        (lambda s: s["negative_labels"].append("bogus"), "unknown label"),
        (lambda s: s["negative_dimensions"].append("bogus"), "contains unknown label"),
        (lambda s: s["silence"].update(dejection_rate_per_hour=-1), "must not be negative"),
        (lambda s: s["affect"].update(mood_follow_hours=0), "mood_follow_hours must be positive"),
        (lambda s: s["affect"].update(habituation_factor=1.5), "habituation_factor"),
        (lambda s: s["prompt"].update(top_n=0), "top_n must be positive"),
        (lambda s: s["prompt"].update(level_high=0.4, level_elevated=0.6), "level_high"),
        (lambda s: s["proactive"].update(longing_threshold=5.0), "within value_range"),
    ],
)
def test_invalid_emotions_files_rejected(tmp_path, mutation, message):
    path = _write(tmp_path, mutation)
    with pytest.raises(ValueError, match=message):
        load_emotions(path)


def test_emotions_file_rejects_duplicate_keys(tmp_path):
    path = tmp_path / "emotions.json"
    path.write_text('{"emotion_version": "a", "emotion_version": "b"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate key"):
        load_emotions(path)


def test_emotions_file_rejects_nan_constant(tmp_path):
    path = tmp_path / "emotions.json"
    path.write_text(
        '{"schema_version": 1, "emotion_version": "x", "dimensions": {}, '
        '"label_deltas": {"neutral": {"fear": NaN}}}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="non-finite"):
        load_emotions(path)


def test_emotions_file_missing_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="emotions file not found"):
        load_emotions(tmp_path / "absent.json")