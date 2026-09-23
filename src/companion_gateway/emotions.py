"""Emotional definitions: loading, validation, resolution, and fingerprinting.

The packaged emotions.json is the authoritative source for every emotional
definition, table, and tuning value. This module validates it strictly, computes
a stable canonical fingerprint, and resolves the effective snapshot a running
engine uses.

Resolution precedence is: packaged defaults, then an optional user emotions.json,
then explicit configuration overrides. Explicit overrides come from AffectConfig
affect knobs, ProactiveConfig emotional thresholds, dimension overrides, and
label-pattern overrides. Config fields carry an `UNSET` sentinel until
`__post_init__` fills them, so an explicitly-provided value (including one equal
to a packaged default) is never confused with an unfilled default.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .resources import load_packaged, loads_strict

SCHEMA_VERSION = 1

# Sentinel for config fields that the caller did not provide.
UNSET: Any = object()


class EmotionsValidationError(ValueError):
    """Raised when an emotions definition is malformed or out of range."""


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EmotionsValidationError(f"{path} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise EmotionsValidationError(f"{path} must be a finite number")
    return number


def _integer(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EmotionsValidationError(f"{path} must be an integer")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise EmotionsValidationError(f"{path} must be a string")
    return value


def _deltas_map(value: Any, path: str, dimensions: dict[str, Any]) -> None:
    if not isinstance(value, dict):
        raise EmotionsValidationError(f"{path} must be an object")
    for name, amount in value.items():
        if name not in dimensions:
            raise EmotionsValidationError(f"{path}.{name} references unknown dimension")
        _number(amount, f"{path}.{name}")


def _labels_of(value: Any, path: str, known: set[str]) -> list[str]:
    if not isinstance(value, list):
        raise EmotionsValidationError(f"{path} must be an array")
    for item in value:
        label = _string(item, path)
        if label not in known:
            raise EmotionsValidationError(f"{path} contains unknown label: {label!r}")
    return list(value)


def _validate(snapshot: dict[str, Any]) -> dict[str, Any]:
    known = {
        "schema_version",
        "emotion_version",
        "description",
        "value_range",
        "dimensions",
        "label_deltas",
        "contact_deltas",
        "soothing_deltas",
        "negative_labels",
        "soothing_labels",
        "negative_dimensions",
        "recent_labels_limit",
        "negative_follow_up_minutes",
        "silence",
        "proactive_sent_deltas",
        "prompt",
        "label_patterns",
        "affect",
        "proactive",
        "impact_scale",
        "mood_follow_gain",
        "follow_up_expiration_hours",
    }
    unknown = sorted(set(snapshot) - known)
    if unknown:
        raise EmotionsValidationError(f"unknown emotions field(s): {unknown}")

    schema_version = _integer(snapshot["schema_version"], "schema_version")
    if schema_version != SCHEMA_VERSION:
        raise EmotionsValidationError(
            f"schema_version {schema_version} is not supported; expected {SCHEMA_VERSION}"
        )
    if not snapshot.get("emotion_version"):
        raise EmotionsValidationError("emotion_version must be a non-empty string")
    _string(snapshot["emotion_version"], "emotion_version")

    value_range = snapshot["value_range"]
    if not isinstance(value_range, dict):
        raise EmotionsValidationError("value_range must be an object")
    range_min = _number(value_range.get("min"), "value_range.min")
    range_max = _number(value_range.get("max"), "value_range.max")
    if (range_min, range_max) != (0.0, 1.0):
        raise EmotionsValidationError(
            "value_range must be [0, 1] in this preserve-behavior phase; "
            "arbitrary ranges are not supported"
        )
    snapshot["value_range"] = {"min": range_min, "max": range_max}

    dimensions = snapshot["dimensions"]
    if not isinstance(dimensions, dict) or not dimensions:
        raise EmotionsValidationError("dimensions must be a non-empty object")
    for name, params in dimensions.items():
        path = f"dimensions.{name}"
        if not isinstance(params, dict):
            raise EmotionsValidationError(f"{path} must be an object")
        neutral = _number(params.get("neutral"), f"{path}.neutral")
        floor = _number(params.get("floor"), f"{path}.floor")
        tau = _number(params.get("tau"), f"{path}.tau")
        if tau <= 0:
            raise EmotionsValidationError(f"{path}.tau must be positive")
        if floor > neutral:
            raise EmotionsValidationError(f"{path}.floor must be <= {path}.neutral")
        if not range_min <= floor <= range_max or not range_min <= neutral <= range_max:
            raise EmotionsValidationError(f"{path} values must be within value_range")
        params["neutral"] = neutral
        params["floor"] = floor
        params["tau"] = tau

    label_deltas = snapshot["label_deltas"]
    if not isinstance(label_deltas, dict) or not label_deltas:
        raise EmotionsValidationError("label_deltas must be a non-empty object")
    for label, deltas in label_deltas.items():
        _deltas_map(deltas, f"label_deltas.{label}", dimensions)

    known_labels = set(label_deltas)
    _deltas_map(snapshot.get("contact_deltas") or {}, "contact_deltas", dimensions)
    _deltas_map(snapshot.get("soothing_deltas") or {}, "soothing_deltas", dimensions)
    snapshot["negative_labels"] = _labels_of(
        snapshot.get("negative_labels") or [], "negative_labels", known_labels
    )
    snapshot["soothing_labels"] = _labels_of(
        snapshot.get("soothing_labels") or [], "soothing_labels", known_labels
    )
    snapshot["negative_dimensions"] = _labels_of(
        snapshot.get("negative_dimensions") or [], "negative_dimensions", set(dimensions)
    )

    limit = _integer(snapshot.get("recent_labels_limit"), "recent_labels_limit")
    if limit < 1:
        raise EmotionsValidationError("recent_labels_limit must be positive")
    snapshot["recent_labels_limit"] = limit

    follow_up = _integer(snapshot.get("negative_follow_up_minutes"), "negative_follow_up_minutes")
    if follow_up < 1:
        raise EmotionsValidationError("negative_follow_up_minutes must be positive")
    snapshot["negative_follow_up_minutes"] = follow_up

    silence = snapshot["silence"]
    if not isinstance(silence, dict):
        raise EmotionsValidationError("silence must be an object")
    caps = silence.get("caps")
    if not isinstance(caps, dict):
        raise EmotionsValidationError("silence.caps must be an object")
    for name, cap in caps.items():
        if name not in dimensions:
            raise EmotionsValidationError(f"silence.caps.{name} references unknown dimension")
        cap_value = _number(cap, f"silence.caps.{name}")
        if cap_value < 0:
            raise EmotionsValidationError(f"silence.caps.{name} must not be negative")
        caps[name] = cap_value
    gate = _number(silence.get("dejection_gate_hours"), "silence.dejection_gate_hours")
    rate = _number(silence.get("dejection_rate_per_hour"), "silence.dejection_rate_per_hour")
    if gate < 0:
        raise EmotionsValidationError("silence.dejection_gate_hours must not be negative")
    if rate < 0:
        raise EmotionsValidationError("silence.dejection_rate_per_hour must not be negative")
    silence["dejection_gate_hours"] = gate
    silence["dejection_rate_per_hour"] = rate

    _deltas_map(snapshot.get("proactive_sent_deltas") or {}, "proactive_sent_deltas", dimensions)

    impact_scale = _number(snapshot.get("impact_scale"), "impact_scale")
    if impact_scale <= 0:
        raise EmotionsValidationError("impact_scale must be positive")
    snapshot["impact_scale"] = impact_scale

    gain = snapshot["mood_follow_gain"]
    if not isinstance(gain, dict):
        raise EmotionsValidationError("mood_follow_gain must be an object")
    gain_min = _number(gain.get("min"), "mood_follow_gain.min")
    gain_max = _number(gain.get("max"), "mood_follow_gain.max")
    gain_factor = _number(gain.get("factor"), "mood_follow_gain.factor")
    if gain_min < 0:
        raise EmotionsValidationError("mood_follow_gain.min must not be negative")
    if gain_max < gain_min:
        raise EmotionsValidationError("mood_follow_gain.max must be >= mood_follow_gain.min")
    if gain_factor <= 0:
        raise EmotionsValidationError("mood_follow_gain.factor must be positive")
    snapshot["mood_follow_gain"] = {"min": gain_min, "max": gain_max, "factor": gain_factor}

    expiry_hours = _integer(
        snapshot.get("follow_up_expiration_hours"), "follow_up_expiration_hours"
    )
    if expiry_hours < 1:
        raise EmotionsValidationError("follow_up_expiration_hours must be positive")
    snapshot["follow_up_expiration_hours"] = expiry_hours

    prompt = snapshot["prompt"]
    if not isinstance(prompt, dict):
        raise EmotionsValidationError("prompt must be an object")
    top_n = _integer(prompt.get("top_n"), "prompt.top_n")
    if top_n < 1:
        raise EmotionsValidationError("prompt.top_n must be positive")
    deviation = _number(prompt.get("deviation_threshold"), "prompt.deviation_threshold")
    fear_min = _number(prompt.get("fear_minimum"), "prompt.fear_minimum")
    level_high = _number(prompt.get("level_high"), "prompt.level_high")
    level_elevated = _number(prompt.get("level_elevated"), "prompt.level_elevated")
    if deviation < 0:
        raise EmotionsValidationError("prompt.deviation_threshold must not be negative")
    if level_high < level_elevated:
        raise EmotionsValidationError("prompt.level_high must be >= prompt.level_elevated")
    prompt["top_n"] = top_n
    prompt["deviation_threshold"] = deviation
    prompt["fear_minimum"] = fear_min
    prompt["level_high"] = level_high
    prompt["level_elevated"] = level_elevated

    label_patterns = snapshot["label_patterns"]
    if not isinstance(label_patterns, dict):
        raise EmotionsValidationError("label_patterns must be an object")
    for label, phrases in label_patterns.items():
        if label not in known_labels:
            raise EmotionsValidationError(f"label_patterns.{label} references unknown label")
        if not isinstance(phrases, list) or not phrases:
            raise EmotionsValidationError(f"label_patterns.{label} must be a non-empty array")
        normalized: list[str] = []
        for phrase in phrases:
            text = _string(phrase, f"label_patterns.{label}")
            if not text:
                raise EmotionsValidationError(f"label_patterns.{label} must not contain empty phrases")
            normalized.append(text)
        label_patterns[label] = normalized

    affect = snapshot["affect"]
    if not isinstance(affect, dict):
        raise EmotionsValidationError("affect must be an object")
    mood_follow = _number(affect.get("mood_follow_hours"), "affect.mood_follow_hours")
    mood_return = _number(affect.get("mood_return_hours"), "affect.mood_return_hours")
    habituation_window = _integer(
        affect.get("habituation_window_minutes"), "affect.habituation_window_minutes"
    )
    habituation_factor = _number(affect.get("habituation_factor"), "affect.habituation_factor")
    silence_longing = _number(
        affect.get("silence_longing_per_hour"), "affect.silence_longing_per_hour"
    )
    silence_anxiety = _number(
        affect.get("silence_anxiety_per_hour"), "affect.silence_anxiety_per_hour"
    )
    silence_seeking = _number(
        affect.get("silence_seeking_per_hour"), "affect.silence_seeking_per_hour"
    )
    fallback = _integer(
        affect.get("classification_fallback_seconds"), "affect.classification_fallback_seconds"
    )
    if mood_follow <= 0:
        raise EmotionsValidationError("affect.mood_follow_hours must be positive")
    if mood_return <= 0:
        raise EmotionsValidationError("affect.mood_return_hours must be positive")
    if habituation_window <= 0:
        raise EmotionsValidationError("affect.habituation_window_minutes must be positive")
    if not 0 < habituation_factor <= 1:
        raise EmotionsValidationError("affect.habituation_factor must be in (0, 1]")
    for name, value in (
        ("longing", silence_longing),
        ("anxiety", silence_anxiety),
        ("seeking", silence_seeking),
    ):
        if value < 0:
            raise EmotionsValidationError(f"affect.silence_{name}_per_hour must not be negative")
    if fallback < 1:
        raise EmotionsValidationError("affect.classification_fallback_seconds must be positive")
    affect["mood_follow_hours"] = mood_follow
    affect["mood_return_hours"] = mood_return
    affect["habituation_window_minutes"] = habituation_window
    affect["habituation_factor"] = habituation_factor
    affect["silence_longing_per_hour"] = silence_longing
    affect["silence_anxiety_per_hour"] = silence_anxiety
    affect["silence_seeking_per_hour"] = silence_seeking
    affect["classification_fallback_seconds"] = fallback

    proactive = snapshot["proactive"]
    if not isinstance(proactive, dict):
        raise EmotionsValidationError("proactive must be an object")
    longing_threshold = _number(proactive.get("longing_threshold"), "proactive.longing_threshold")
    fear_threshold = _number(proactive.get("fear_threshold"), "proactive.fear_threshold")
    for name, value in (("longing_threshold", longing_threshold), ("fear_threshold", fear_threshold)):
        if not range_min <= value <= range_max:
            raise EmotionsValidationError(f"proactive.{name} must be within value_range")
    proactive["longing_threshold"] = longing_threshold
    proactive["fear_threshold"] = fear_threshold

    return snapshot


_DEFAULT = _validate(load_packaged("emotions.json"))


def default_emotions() -> dict:
    """Return a fresh deep copy of the validated packaged emotions.json."""
    return copy.deepcopy(_DEFAULT)


def canonical(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(snapshot: dict[str, Any]) -> str:
    """Stable canonical fingerprint of an effective emotions snapshot."""
    return "sha256:" + hashlib.sha256(canonical(snapshot).encode("utf-8")).hexdigest()


def default_fingerprint() -> str:
    return fingerprint(_DEFAULT)


def load_emotions(path: str | Path) -> dict:
    """Load and validate a user emotions.json file."""
    source_path = Path(path)
    if not source_path.exists():
        raise EmotionsValidationError(f"emotions file not found: {source_path}")
    try:
        text = source_path.read_text(encoding="utf-8")
    except OSError as error:
        raise EmotionsValidationError(f"cannot read emotions file {source_path}: {error}") from error
    return _validate(loads_strict(text, source=str(source_path)))


def _apply_explicit_overrides(
    section: dict[str, Any], config_obj: Any, explicit_attr: str, custom: bool
) -> dict[str, Any]:
    """Merge config field values over a snapshot section.

    Without a custom emotions file the current config field values are
    authoritative (defaults, explicit values, and values set before engine
    construction). With a custom file, only values the caller explicitly
    provided override the file's values.
    """
    result = dict(section)
    explicit = frozenset(getattr(config_obj, explicit_attr, ()))
    for name in list(section):
        value = getattr(config_obj, name, UNSET)
        if value is UNSET:
            continue
        if not custom or name in explicit:
            result[name] = value
    return result


def resolve_emotions(
    affect_config: Any, proactive_config: Any | None = None
) -> dict[str, Any]:
    """Resolve the effective emotions snapshot.

    Precedence: packaged defaults, optional user emotions.json (with pinned
    version check), then explicit configuration overrides. The returned snapshot
    is validated; the fingerprint callers derive from it reflects every applied
    override, not just the raw defaults.
    """
    snapshot = default_emotions()
    custom = bool(getattr(affect_config, "emotions_path", ""))
    if custom:
        user = load_emotions(str(affect_config.emotions_path))
        expected = str(getattr(affect_config, "expected_emotion_version", "") or "")
        if expected and user.get("emotion_version") != expected:
            raise EmotionsValidationError(
                f"emotions version mismatch: expected {expected!r}, found {user.get('emotion_version')!r}"
            )
        snapshot = user

    snapshot["affect"] = _apply_explicit_overrides(
        snapshot["affect"], affect_config, "explicit_knobs", custom
    )
    if proactive_config is not None:
        snapshot["proactive"] = _apply_explicit_overrides(
            snapshot["proactive"], proactive_config, "explicit_emotional", custom
        )

    overrides = getattr(affect_config, "dimensions", None) or {}
    if overrides:
        if not isinstance(overrides, dict):
            raise EmotionsValidationError("dimensions override must be an object")
        for name, values in overrides.items():
            if name not in snapshot["dimensions"]:
                raise EmotionsValidationError(
                    f"dimensions override references unknown dimension: {name!r}"
                )
            if not isinstance(values, dict):
                raise EmotionsValidationError(f"dimensions override {name!r} must be an object")
            unknown_keys = sorted(set(values) - {"neutral", "floor", "tau"})
            if unknown_keys:
                raise EmotionsValidationError(
                    f"dimensions override {name!r} has unknown field(s): {unknown_keys}"
                )
            for key in ("neutral", "floor", "tau"):
                if key in values:
                    snapshot["dimensions"][name][key] = _number(
                        values[key], f"dimensions override {name!r}.{key}"
                    )

    patterns = getattr(affect_config, "label_patterns", None) or {}
    if patterns:
        if not isinstance(patterns, dict):
            raise EmotionsValidationError("label_patterns override must be an object")
        for label, phrases in patterns.items():
            if label not in snapshot["label_deltas"]:
                raise EmotionsValidationError(
                    f"label_patterns override references unknown label: {label!r}"
                )
            if not isinstance(phrases, list):
                raise EmotionsValidationError(f"label_patterns override {label!r} must be an array")
            snapshot["label_patterns"][label] = [
                _string(p, f"label_patterns override {label!r}") for p in phrases
            ]

    return _validate(snapshot)