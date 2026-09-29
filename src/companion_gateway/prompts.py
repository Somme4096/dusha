from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .resources import loads_strict, read_packaged
from .serialization import canonical as _serialization_canonical
from .serialization import fingerprint as _serialization_fingerprint

SCHEMA_VERSION = 2

_TEXT_SLOTS = {
    "affect_presentation",
    "companion_state",
    "evergreen",
    "proactive_generation_instruction",
    "decision_instruction",
}

_STRING_SLOTS = {"decision_instruction"}

_PROACTIVE_KEYS = {"base", "variants"}

_SLOT_KEYS: dict[str, set[str]] = {
    "affect_presentation": {
        "prefix",
        "separator",
        "suffix",
        "baseline",
        "level_connector",
        "level_high",
        "level_elevated",
        "level_noticeable",
    },
    "companion_state": {
        "open_delimiter",
        "close_delimiter",
        "affect_instruction",
        "memory_instruction",
    },
    "evergreen": {"open_delimiter", "close_delimiter"},
}


class PromptsValidationError(ValueError):
    pass


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise PromptsValidationError(f"{path} must be a string")
    return value


def _validate_proactive_slot(value: Any, *, require_complete: bool) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PromptsValidationError("proactive_generation_instruction must be an object")
    unknown = sorted(set(value) - _PROACTIVE_KEYS)
    if unknown:
        raise PromptsValidationError(
            f"unknown field(s) under proactive_generation_instruction: {unknown}"
        )
    missing = sorted(_PROACTIVE_KEYS - set(value))
    if missing:
        raise PromptsValidationError(
            f"proactive_generation_instruction is incomplete. Missing field(s): {missing}"
        )
    base = value["base"]
    if not isinstance(base, str):
        raise PromptsValidationError("proactive_generation_instruction.base must be a string")
    variants = value["variants"]
    if not isinstance(variants, list) or not variants:
        raise PromptsValidationError(
            "proactive_generation_instruction.variants must be a non-empty list"
        )
    for index, variant in enumerate(variants):
        if not isinstance(variant, str) or not variant.strip():
            raise PromptsValidationError(
                f"proactive_generation_instruction.variants[{index}] must be a non-empty string"
            )
    value["base"] = base
    value["variants"] = list(variants)
    return value


def _validate_text_slots(snapshot: dict[str, Any], require_complete: bool) -> dict[str, Any]:
    for slot, keys in _SLOT_KEYS.items():
        if slot not in snapshot:
            if require_complete:
                raise PromptsValidationError(f"missing prompts slot: {slot}")
            continue
        value = snapshot[slot]
        if not isinstance(value, dict):
            raise PromptsValidationError(f"{slot} must be an object")
        unknown = sorted(set(value) - keys)
        if unknown:
            raise PromptsValidationError(f"unknown field(s) under {slot}: {unknown}")
        missing = sorted(keys - set(value))
        if missing:
            raise PromptsValidationError(
                f"{slot} is incomplete. Missing field(s): {missing}"
            )
        for key in keys:
            value[key] = _string(value[key], f"{slot}.{key}")
    return snapshot


def _validate(snapshot: dict[str, Any], require_complete: bool) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise PromptsValidationError("prompts must contain a JSON object")
    if not require_complete:
        unknown = sorted(set(snapshot) - _TEXT_SLOTS)
        if unknown:
            raise PromptsValidationError(f"unknown prompts slot(s): {unknown}")
    _validate_text_slots(snapshot, require_complete=require_complete)
    if "proactive_generation_instruction" in snapshot:
        snapshot["proactive_generation_instruction"] = _validate_proactive_slot(
            snapshot["proactive_generation_instruction"], require_complete=require_complete
        )
    elif require_complete:
        raise PromptsValidationError("missing prompts slot: proactive_generation_instruction")
    for slot in _STRING_SLOTS:
        if slot in snapshot:
            snapshot[slot] = _string(snapshot[slot], slot)
        elif require_complete:
            raise PromptsValidationError(f"missing prompts slot: {slot}")
    if require_complete:
        schema_version = snapshot.get("schema_version")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise PromptsValidationError("schema_version must be an integer")
        if schema_version != SCHEMA_VERSION:
            raise PromptsValidationError(
                f"schema_version {schema_version} is not supported. Expected {SCHEMA_VERSION}"
            )
        if not snapshot.get("prompts_version"):
            raise PromptsValidationError("prompts_version must be a non-empty string")
        _string(snapshot["prompts_version"], "prompts_version")
    return snapshot


_DEFAULT = _validate(
    loads_strict(read_packaged("prompts.json"), source="prompts.json"), require_complete=True
)


def default_prompts() -> dict:
    return copy.deepcopy(_DEFAULT)


def load_prompts(path: str | Path) -> dict:
    source_path = Path(path)
    if not source_path.exists():
        raise PromptsValidationError(f"prompts file not found: {source_path}")
    try:
        text = source_path.read_text(encoding="utf-8")
    except OSError as error:
        raise PromptsValidationError(f"cannot read prompts file {source_path}: {error}") from error
    return _validate(loads_strict(text, source=str(source_path)), require_complete=False)


def resolve_prompts(prompts_config: Any) -> dict:
    snapshot = default_prompts()
    path = str(getattr(prompts_config, "path", "") or "")
    if path:
        user = load_prompts(path)
        for slot, value in user.items():
            snapshot[slot] = copy.deepcopy(value)
    return _validate(snapshot, require_complete=True)


def canonical(snapshot: dict[str, Any]) -> str:
    return _serialization_canonical(snapshot)


def fingerprint(snapshot: dict[str, Any]) -> str:
    return _serialization_fingerprint(snapshot)


def default_fingerprint() -> str:
    return fingerprint(_DEFAULT)
