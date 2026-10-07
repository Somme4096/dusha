from __future__ import annotations

import copy
import math
import re
from pathlib import Path
from typing import Any

from .resources import load_packaged, loads_strict
from .serialization import canonical as _serialization_canonical
from .serialization import fingerprint as _serialization_fingerprint

SCHEMA_VERSION = 3

UNSET: Any = object()

_SILENCE_RULE_KEYS = {"rate_per_hour", "cap", "gate_hours"}

_V2_SILENCE_RATE = re.compile(r"silence_(.+)_per_hour")
_V2_GATED_SILENCE = {
    "dejection": {"rate_per_hour": "dejection_rate_per_hour", "gate_hours": "dejection_gate_hours"}
}
_V2_ALWAYS_SHOW = {"fear": "fear_minimum"}
_V2_TRIGGERS = (("fear", "fear"), ("longing", "silence"))


class EmotionsValidationError(ValueError):
    pass


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


def _section(snapshot: dict[str, Any], name: str, known: set[str]) -> dict[str, Any]:
    value = snapshot.get(name)
    if not isinstance(value, dict):
        raise EmotionsValidationError(f"{name} must be an object")
    unknown = sorted(set(value) - known)
    if unknown:
        raise EmotionsValidationError(f"unknown field(s) under {name}: {unknown}")
    return value


def _non_negative(value: Any, path: str) -> float:
    number = _number(value, path)
    if number < 0:
        raise EmotionsValidationError(f"{path} must not be negative")
    return number


def _dimension(name: Any, path: str, dimensions: dict[str, Any]) -> str:
    if not isinstance(name, str) or name not in dimensions:
        raise EmotionsValidationError(f"{path} references unknown dimension: {name!r}")
    return name


def _validate_silence(snapshot: dict[str, Any], dimensions: dict[str, Any]) -> None:
    silence = _section(snapshot, "silence", {"rules"})
    rules = silence.get("rules")
    if not isinstance(rules, dict):
        raise EmotionsValidationError("silence.rules must be an object")
    for name, rule in rules.items():
        path = f"silence.rules.{name}"
        _dimension(name, path, dimensions)
        if not isinstance(rule, dict):
            raise EmotionsValidationError(f"{path} must be an object")
        unknown = sorted(set(rule) - _SILENCE_RULE_KEYS)
        if unknown:
            raise EmotionsValidationError(f"unknown field(s) under {path}: {unknown}")
        rules[name] = {
            "rate_per_hour": _non_negative(rule.get("rate_per_hour"), f"{path}.rate_per_hour"),
            "cap": _non_negative(rule.get("cap"), f"{path}.cap"),
            "gate_hours": _non_negative(rule.get("gate_hours", 0.0), f"{path}.gate_hours"),
        }


def _validate_triggers(
    snapshot: dict[str, Any], dimensions: dict[str, Any], range_min: float, range_max: float
) -> None:
    proactive = _section(snapshot, "proactive", {"triggers"})
    triggers = proactive.get("triggers")
    if not isinstance(triggers, list):
        raise EmotionsValidationError("proactive.triggers must be an array")
    for index, trigger in enumerate(triggers):
        path = f"proactive.triggers[{index}]"
        if not isinstance(trigger, dict):
            raise EmotionsValidationError(f"{path} must be an object")
        unknown = sorted(set(trigger) - {"dimension", "threshold", "reason"})
        if unknown:
            raise EmotionsValidationError(f"unknown field(s) under {path}: {unknown}")
        name = _dimension(trigger.get("dimension"), f"{path}.dimension", dimensions)
        threshold = _number(trigger.get("threshold"), f"{path}.threshold")
        if not range_min <= threshold <= range_max:
            raise EmotionsValidationError(f"{path}.threshold must be within value_range")
        reason = _string(trigger.get("reason"), f"{path}.reason")
        if not reason:
            raise EmotionsValidationError(f"{path}.reason must be a non-empty string")
        triggers[index] = {"dimension": name, "threshold": threshold, "reason": reason}


def _validate_appraisal(snapshot: dict[str, Any], dimensions: dict[str, Any]) -> None:
    snapshot.setdefault("appraisal", {})
    appraisal = _section(snapshot, "appraisal", {"min_similarity", "fallback_label", "prototypes"})
    min_similarity = _number(appraisal.get("min_similarity", 0.5), "appraisal.min_similarity")
    if not -1 <= min_similarity < 1:
        raise EmotionsValidationError("appraisal.min_similarity must be at least -1 and below 1")
    prototypes = appraisal.get("prototypes", {})
    if not isinstance(prototypes, dict):
        raise EmotionsValidationError("appraisal.prototypes must be an object")
    for label, prototype in prototypes.items():
        path = f"appraisal.prototypes.{label}"
        if not isinstance(prototype, dict):
            raise EmotionsValidationError(f"{path} must be an object")
        unknown = sorted(set(prototype) - {"text", "deltas"})
        if unknown:
            raise EmotionsValidationError(f"unknown field(s) under {path}: {unknown}")
        if not _string(prototype.get("text"), f"{path}.text").strip():
            raise EmotionsValidationError(f"{path}.text must be a non-empty string")
        _deltas_map(prototype.get("deltas"), f"{path}.deltas", dimensions)
    fallback = _string(appraisal.get("fallback_label", ""), "appraisal.fallback_label")
    if fallback and fallback not in prototypes:
        raise EmotionsValidationError(f"appraisal.fallback_label names unknown prototype: {fallback!r}")
    appraisal.update(min_similarity=min_similarity, fallback_label=fallback, prototypes=prototypes)


def _upgrade_v2(snapshot: dict[str, Any]) -> dict[str, Any]:
    dimensions = snapshot.get("dimensions")
    affect, silence = snapshot.get("affect"), snapshot.get("silence")
    prompt, proactive = snapshot.get("prompt"), snapshot.get("proactive")
    if not all(isinstance(item, dict) for item in (dimensions, affect, silence, prompt, proactive)):
        return snapshot
    caps = silence.pop("caps", None)
    caps = caps if isinstance(caps, dict) else {}
    rules: dict[str, dict[str, Any]] = {}
    for key in list(affect):
        match = _V2_SILENCE_RATE.fullmatch(key)
        if match:
            rules[match.group(1)] = {"rate_per_hour": affect.pop(key)}
    for name, fields in _V2_GATED_SILENCE.items():
        rules[name] = {new: silence.pop(old, None) for new, old in fields.items()}
    for name, rule in rules.items():
        if name in caps:
            rule["cap"] = caps[name]
    silence["rules"] = {name: rule for name, rule in rules.items() if name in dimensions}
    prompt["always_show"] = {
        name: prompt.pop(key)
        for name, key in _V2_ALWAYS_SHOW.items()
        if key in prompt and name in dimensions
    }
    proactive["triggers"] = [
        {"dimension": name, "threshold": proactive.pop(f"{name}_threshold"), "reason": reason}
        for name, reason in _V2_TRIGGERS
        if f"{name}_threshold" in proactive and name in dimensions
    ]
    if "appraisal" not in snapshot:
        appraisal = default_emotions()["appraisal"]
        for prototype in appraisal["prototypes"].values():
            prototype["deltas"] = {k: v for k, v in prototype["deltas"].items() if k in dimensions}
        snapshot["appraisal"] = appraisal
    snapshot["schema_version"] = SCHEMA_VERSION
    return snapshot


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
        "appraisal",
    }
    unknown = sorted(set(snapshot) - known)
    if unknown:
        raise EmotionsValidationError(f"unknown emotions field(s): {unknown}")

    schema_version = _integer(snapshot.get("schema_version"), "schema_version")
    if schema_version == 2:
        snapshot = _upgrade_v2(snapshot)
    elif schema_version != SCHEMA_VERSION:
        raise EmotionsValidationError(
            f"schema_version {schema_version} is not supported. Expected {SCHEMA_VERSION}"
        )
    if not snapshot.get("emotion_version"):
        raise EmotionsValidationError("emotion_version must be a non-empty string")
    _string(snapshot["emotion_version"], "emotion_version")

    value_range = snapshot.get("value_range")
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

    dimensions = snapshot.get("dimensions")
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
        if "description" in params:
            _string(params["description"], f"{path}.description")
        params["neutral"] = neutral
        params["floor"] = floor
        params["tau"] = tau

    snapshot["negative_dimensions"] = _labels_of(
        snapshot.get("negative_dimensions") or [], "negative_dimensions", set(dimensions)
    )

    _validate_silence(snapshot, dimensions)

    _deltas_map(snapshot.get("proactive_sent_deltas") or {}, "proactive_sent_deltas", dimensions)

    impact_scale = _number(snapshot.get("impact_scale"), "impact_scale")
    if impact_scale <= 0:
        raise EmotionsValidationError("impact_scale must be positive")
    snapshot["impact_scale"] = impact_scale

    gain = snapshot.get("mood_follow_gain")
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

    prompt = _section(
        snapshot,
        "prompt",
        {"top_n", "deviation_threshold", "always_show", "level_high", "level_elevated"},
    )
    top_n = _integer(prompt.get("top_n"), "prompt.top_n")
    if top_n < 1:
        raise EmotionsValidationError("prompt.top_n must be positive")
    deviation = _number(prompt.get("deviation_threshold"), "prompt.deviation_threshold")
    level_high = _number(prompt.get("level_high"), "prompt.level_high")
    level_elevated = _number(prompt.get("level_elevated"), "prompt.level_elevated")
    if deviation < 0:
        raise EmotionsValidationError("prompt.deviation_threshold must not be negative")
    if level_high < level_elevated:
        raise EmotionsValidationError("prompt.level_high must be >= prompt.level_elevated")
    always_show = prompt.get("always_show", {})
    _deltas_map(always_show, "prompt.always_show", dimensions)
    prompt["top_n"] = top_n
    prompt["deviation_threshold"] = deviation
    prompt["always_show"] = {name: float(minimum) for name, minimum in always_show.items()}
    prompt["level_high"] = level_high
    prompt["level_elevated"] = level_elevated

    affect = _section(snapshot, "affect", {"mood_follow_hours", "mood_return_hours"})
    mood_follow = _number(affect.get("mood_follow_hours"), "affect.mood_follow_hours")
    mood_return = _number(affect.get("mood_return_hours"), "affect.mood_return_hours")
    if mood_follow <= 0:
        raise EmotionsValidationError("affect.mood_follow_hours must be positive")
    if mood_return <= 0:
        raise EmotionsValidationError("affect.mood_return_hours must be positive")
    affect["mood_follow_hours"] = mood_follow
    affect["mood_return_hours"] = mood_return

    _validate_triggers(snapshot, dimensions, range_min, range_max)
    _validate_appraisal(snapshot, dimensions)

    return snapshot


_DEFAULT = _validate(load_packaged("emotions.json"))


def default_emotions() -> dict:
    return copy.deepcopy(_DEFAULT)


def canonical(snapshot: dict[str, Any]) -> str:
    return _serialization_canonical(snapshot)


def fingerprint(snapshot: dict[str, Any]) -> str:
    return _serialization_fingerprint(snapshot)


def default_fingerprint() -> str:
    return fingerprint(_DEFAULT)


def load_emotions(path: str | Path) -> dict:
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
    result = dict(section)
    explicit = frozenset(getattr(config_obj, explicit_attr, ()))
    for name in list(section):
        value = getattr(config_obj, name, UNSET)
        if value is UNSET:
            continue
        if not custom or name in explicit:
            result[name] = value
    return result


def _merge_overrides(
    target: dict[str, Any], overrides: Any, label: str, keys: set[str], *, create: bool
) -> None:
    if not overrides:
        return
    if not isinstance(overrides, dict):
        raise EmotionsValidationError(f"{label} must be an object")
    for name, values in overrides.items():
        if name not in target and not create:
            raise EmotionsValidationError(f"{label} references unknown dimension: {name!r}")
        if not isinstance(values, dict):
            raise EmotionsValidationError(f"{label} {name!r} must be an object")
        unknown_keys = sorted(set(values) - keys)
        if unknown_keys:
            raise EmotionsValidationError(f"{label} {name!r} has unknown field(s): {unknown_keys}")
        entry = target.setdefault(name, {})
        for key, value in values.items():
            entry[key] = _number(value, f"{label} {name!r}.{key}")


def resolve_emotions(
    affect_config: Any, proactive_config: Any | None = None
) -> dict[str, Any]:
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
    _merge_overrides(
        snapshot["dimensions"],
        getattr(affect_config, "dimensions", None),
        "dimensions override",
        {"neutral", "floor", "tau"},
        create=False,
    )
    _merge_overrides(
        snapshot["silence"]["rules"],
        getattr(affect_config, "silence", None),
        "silence override",
        _SILENCE_RULE_KEYS,
        create=True,
    )
    thresholds = getattr(proactive_config, "thresholds", None) or {}
    if not isinstance(thresholds, dict):
        raise EmotionsValidationError("thresholds override must be an object")
    for name, value in thresholds.items():
        matched = [item for item in snapshot["proactive"]["triggers"] if item["dimension"] == name]
        if not matched:
            raise EmotionsValidationError(
                f"thresholds override references a dimension without a proactive trigger: {name!r}"
            )
        for trigger in matched:
            trigger["threshold"] = _number(value, f"thresholds override {name!r}")

    return _validate(snapshot)
