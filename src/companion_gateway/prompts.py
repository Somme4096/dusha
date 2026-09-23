"""Prompt text slots: packaged defaults, strict user overlay, fingerprint.

All core-owned prompt and instruction text lives in packaged prompts.json.
A user prompts file can replace any recognized text slot completely; omitted
slots inherit the packaged defaults. Replacement is literal: a provided slot
replaces the packaged slot entirely, with no appended hidden defaults.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from .resources import loads_strict, read_packaged

SCHEMA_VERSION = 1

# Recognized top-level text slots a user file may replace.
_TEXT_SLOTS = {
    "affect_presentation",
    "companion_state",
    "evergreen",
    "proactive_generation_instruction",
}

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
    """Raised when a prompts definition is malformed."""


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise PromptsValidationError(f"{path} must be a string")
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
                f"{slot} is incomplete; missing field(s): {missing}"
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
    if require_complete:
        schema_version = snapshot.get("schema_version")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise PromptsValidationError("schema_version must be an integer")
        if schema_version != SCHEMA_VERSION:
            raise PromptsValidationError(
                f"schema_version {schema_version} is not supported; expected {SCHEMA_VERSION}"
            )
        if not snapshot.get("prompts_version"):
            raise PromptsValidationError("prompts_version must be a non-empty string")
        _string(snapshot["prompts_version"], "prompts_version")
        _string(
            snapshot["proactive_generation_instruction"],
            "proactive_generation_instruction",
        )
    return snapshot


_DEFAULT = _validate(
    loads_strict(read_packaged("prompts.json"), source="prompts.json"), require_complete=True
)


def default_prompts() -> dict:
    """Return a fresh deep copy of the validated packaged prompts.json."""
    return copy.deepcopy(_DEFAULT)


def load_prompts(path: str | Path) -> dict:
    """Load and validate a user prompts overlay file (a subset of text slots)."""
    source_path = Path(path)
    if not source_path.exists():
        raise PromptsValidationError(f"prompts file not found: {source_path}")
    try:
        text = source_path.read_text(encoding="utf-8")
    except OSError as error:
        raise PromptsValidationError(f"cannot read prompts file {source_path}: {error}") from error
    return _validate(loads_strict(text, source=str(source_path)), require_complete=False)


def resolve_prompts(prompts_config: Any) -> dict:
    """Resolve the effective prompts snapshot: packaged defaults plus user overlay.

    Each provided text slot replaces the packaged slot entirely. The resolved
    snapshot is validated as complete.
    """
    snapshot = default_prompts()
    path = str(getattr(prompts_config, "path", "") or "")
    if path:
        user = load_prompts(path)
        for slot, value in user.items():
            snapshot[slot] = copy.deepcopy(value)
    return _validate(snapshot, require_complete=True)


def canonical(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(snapshot: dict[str, Any]) -> str:
    """Stable canonical fingerprint of the effective prompts snapshot."""
    return "sha256:" + hashlib.sha256(canonical(snapshot).encode("utf-8")).hexdigest()


def default_fingerprint() -> str:
    return fingerprint(_DEFAULT)