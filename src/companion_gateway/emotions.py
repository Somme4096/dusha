"""Emotional definitions: loading, validation, resolution, and fingerprinting.

The packaged emotions.json is the authoritative source for every emotional
definition, table, and tuning value. This module validates it strictly, computes
a stable canonical fingerprint, and resolves the effective snapshot a running
engine uses.

Resolution precedence is: packaged defaults, then an optional user emotions.json,
then explicit configuration overrides. Explicit overrides come from AffectConfig
affect knobs, ProactiveConfig emotional thresholds, and dimension overrides.
Config fields carry an `UNSET` sentinel until `__post_init__` fills them, so an
explicitly-provided value (including one equal to a packaged default) is never
confused with an unfilled default.
"""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any

from .resources import load_packaged, loads_strict
from .serialization import canonical as _serialization_canonical
from .serialization import fingerprint as _serialization_fingerprint

SCHEMA_VERSION = 2

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
        "negative_dimensions",
        "silence",
        "proactive_sent_deltas",
        "prompt",
        "affect",
        "proactive",
        "impact_scale",
        "mood_follow_gain",
    }
    unknown = sorted(set(snapshot) - known)
    if unknown:
        raise EmotionsValidationError(f"unknown emotions field(s): {unknown}")

    schema_version = _integer(snapshot["schema_version"], "schema_version")
    if schema_version != SCHEMA_VERSION:
        raise EmotionsValidationError(
            f"schema_version {schema_version} is not supported. Expected {SCHEMA_VERSION}"
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
            "value_range must be [0, 1] in this preserve-behavior phase. "
            "Arbitrary ranges are not supported"
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

    snapshot["negative_dimensions"] = _labels_of(
        snapshot.get("negative_dimensions") or [], "negative_dimensions", set(dimensions)
    )

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

    affect = snapshot["affect"]
    if not isinstance(affect, dict):
        raise EmotionsValidationError("affect must be an object")
    mood_follow = _number(affect.get("mood_follow_hours"), "affect.mood_follow_hours")
    mood_return = _number(affect.get("mood_return_hours"), "affect.mood_return_hours")
    silence_longing = _number(
        affect.get("silence_longing_per_hour"), "affect.silence_longing_per_hour"
    )
    silence_anxiety = _number(
        affect.get("silence_anxiety_per_hour"), "affect.silence_anxiety_per_hour"
    )
    silence_seeking = _number(
        affect.get("silence_seeking_per_hour"), "affect.silence_seeking_per_hour"
    )
    if mood_follow <= 0:
        raise EmotionsValidationError("affect.mood_follow_hours must be positive")
    if mood_return <= 0:
        raise EmotionsValidationError("affect.mood_return_hours must be positive")
    for name, value in (
        ("longing", silence_longing),
        ("anxiety", silence_anxiety),
        ("seeking", silence_seeking),
    ):
        if value < 0:
            raise EmotionsValidationError(f"affect.silence_{name}_per_hour must not be negative")
    affect["mood_follow_hours"] = mood_follow
    affect["mood_return_hours"] = mood_return
    affect["silence_longing_per_hour"] = silence_longing
    affect["silence_anxiety_per_hour"] = silence_anxiety
    affect["silence_seeking_per_hour"] = silence_seeking

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
    """Canonical sort-keyed JSON of an effective emotions snapshot."""
    return _serialization_canonical(snapshot)


def fingerprint(snapshot: dict[str, Any]) -> str:
    """Stable canonical fingerprint of an effective emotions snapshot."""
    return _serialization_fingerprint(snapshot)


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

    return _validate(snapshot)
