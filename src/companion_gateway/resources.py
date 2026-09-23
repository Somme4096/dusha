"""Packaged JSON resource loading and shared strict JSON parsing.

Uses importlib.resources so the files load from a source checkout and from an
installed wheel alike. Every caller receives fresh parsed objects; callers that
mutate a snapshot must copy it first. Strict parsing (duplicate keys, non-finite
constants, malformed JSON) is shared here so config.json and emotions.json use
the same rules.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any


class StrictJSONError(ValueError):
    """Raised for duplicate keys, non-finite constants, or malformed JSON."""


def loads_strict(text: str, *, source: str = "<string>") -> dict:
    """Parse JSON strictly. Duplicate keys and non-finite constants are errors."""

    def pairs(items: list[tuple[str, Any]]) -> dict:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise StrictJSONError(f"duplicate key in {source}: {key!r}")
            result[key] = value
        return result

    def constant(value: str) -> Any:
        raise StrictJSONError(f"non-finite constant is not allowed in {source}: {value!r}")

    try:
        data = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except json.JSONDecodeError as error:
        raise StrictJSONError(f"invalid JSON in {source}: {error}") from error
    if not isinstance(data, dict):
        raise StrictJSONError(f"{source} must contain a JSON object")
    return data


def read_packaged(name: str) -> str:
    base = resources.files("companion_gateway") / "resources"
    return (base / name).read_text(encoding="utf-8")


def load_packaged(name: str) -> dict:
    return loads_strict(read_packaged(name), source=name)