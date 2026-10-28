from __future__ import annotations

import copy
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import legacy
from .homes import DOCS
from .resources import loads_strict, read_packaged
from .serialization import canonical as _serialization_canonical

SCHEMA_VERSION = 4

logger = logging.getLogger("dusha")

_SLOTS = {"companion_state", "plugins"}

_COMPANION_STATE = {"open_delimiter": str, "close_delimiter": str}


class PromptsValidationError(ValueError):
    pass


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise PromptsValidationError(f"{path} must be a string")
    return value


def _string_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise PromptsValidationError(f"{path} must be a non-empty list")
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise PromptsValidationError(f"{path}[{index}] must be a non-empty string")
    return list(value)


def _template(value: str, path: str, fields: dict[str, Any]) -> None:
    try:
        value.format(**fields)
    except (KeyError, IndexError, ValueError) as error:
        raise PromptsValidationError(
            f"{path} has an invalid placeholder. Allowed: {sorted(fields)}. Double literal braces"
        ) from error


def _value(value: Any, kind: Any, path: str, templates: dict[str, dict[str, Any]]) -> Any:
    if isinstance(kind, dict):
        return _object(value, kind, path, templates)
    if kind is list:
        return _string_list(value, path)
    text = _string(value, path)
    if path in templates:
        _template(text, path, templates[path])
    return text


def _object(value: Any, spec: dict[str, Any], path: str, templates: dict[str, dict[str, Any]]) -> dict:
    if not isinstance(value, dict):
        raise PromptsValidationError(f"{path} must be an object")
    unknown = sorted(set(value) - set(spec))
    if unknown:
        raise PromptsValidationError(f"unknown field(s) under {path}: {unknown}")
    missing = sorted(set(spec) - set(value))
    if missing:
        raise PromptsValidationError(f"{path} is incomplete. Missing field(s): {missing}")
    return {key: _value(value[key], kind, f"{path}.{key}", templates) for key, kind in spec.items()}


def section(
    prompts: dict[str, Any],
    name: str,
    spec: dict[str, Any],
    templates: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    # Spec values are str, list, or a nested spec dict. Template keys are dotted slot paths.
    path = f"plugins.{name}"
    placeholders = {f"{path}.{key}": fields for key, fields in (templates or {}).items()}
    return _object(prompts["plugins"].get(name, {}), spec, path, placeholders)


def _validate(snapshot: Any, require_complete: bool) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise PromptsValidationError("prompts must contain a JSON object")
    allowed = _SLOTS | {"schema_version", "prompts_version"} if require_complete else _SLOTS
    unknown = sorted(set(snapshot) - allowed)
    if unknown:
        raise PromptsValidationError(f"unknown prompts slot(s): {unknown}")
    if "companion_state" in snapshot:
        state = snapshot["companion_state"]
        snapshot["companion_state"] = _object(state, _COMPANION_STATE, "companion_state", {})
    elif require_complete:
        raise PromptsValidationError("missing prompts slot: companion_state")
    plugins = snapshot.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise PromptsValidationError("plugins must be an object keyed by plugin name")
    for name, slots in plugins.items():
        if not isinstance(slots, dict):
            raise PromptsValidationError(f"plugins.{name} must be an object")
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
    overlay, found = legacy.upgrade_prompts(loads_strict(text, source=str(source_path)))
    if found:
        logger.warning(
            "%s uses the schema 3 prompts layout %s. It still loads until 2.0.21. "
            "Run dusha init to upgrade it, see %s",
            source_path, found, f"{DOCS}/blob/main/docs/guide.md#migrating",
        )
    return _validate(overlay, require_complete=False)


def resolve_prompts(prompts_config: Any, plugins: Iterable[str]) -> dict:
    snapshot = default_prompts()
    path = str(getattr(prompts_config, "path", "") or "")
    if path:
        user = load_prompts(path)
        unknown = sorted(set(user["plugins"]) - set(plugins))
        if unknown:
            raise PromptsValidationError(f"unknown plugin(s) under plugins: {unknown}")
        for slot, value in user.items():
            if slot != "plugins":
                snapshot[slot] = copy.deepcopy(value)
                continue
            for name, slots in value.items():
                snapshot["plugins"].setdefault(name, {}).update(copy.deepcopy(slots))
    return _validate(snapshot, require_complete=True)


def canonical(snapshot: dict[str, Any]) -> str:
    return _serialization_canonical(snapshot)
