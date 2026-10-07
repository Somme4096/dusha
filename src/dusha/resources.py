from __future__ import annotations

import json
from importlib import resources
from typing import Any


class StrictJSONError(ValueError):
    pass


def loads_strict(text: str, *, source: str = "<string>") -> dict:

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
    base = resources.files("dusha") / "resources"
    return (base / name).read_text(encoding="utf-8")


def load_packaged(name: str) -> dict:
    return loads_strict(read_packaged(name), source=name)
