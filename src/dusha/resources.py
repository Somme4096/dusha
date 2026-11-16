from __future__ import annotations

import json
import math
import re
from collections.abc import Container
from dataclasses import dataclass
from importlib import resources
from typing import Any

PLUGIN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class StrictJSONError(ValueError):
    pass


class PluginHeaderError(ValueError):
    pass


def plugins_header(value: str | None) -> list[str] | None:
    if value is None or not value.strip():
        return None
    items = [part.strip() for part in value.split(",")]
    if any(not item for item in items):
        raise PluginHeaderError("X-Companion-Plugins must be comma-separated plugin names or 'none'")
    if [item.casefold() for item in items] == ["none"]:
        return []
    for item in items:
        if item.casefold() == "none" or not PLUGIN_NAME.fullmatch(item):
            raise PluginHeaderError(f"invalid plugin name in X-Companion-Plugins: {item!r}")
    return list(dict.fromkeys(items))


@dataclass(frozen=True, slots=True)
class Bounds:
    default: int
    low: int
    high: int

    def clamp(self, value: int) -> int:
        return max(self.low, min(int(value), self.high))


def object_of(
    value: Any,
    label: str,
    allowed: Container[str],
    *,
    required: Container[str] = (),
    error: type[ValueError] = ValueError,
    none_is_empty: bool = True,
) -> dict[str, Any]:
    if value is None and none_is_empty:
        return {}
    if not isinstance(value, dict):
        raise error(f"{label} must be an object")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise error(f"unknown field(s) under {label}: {unknown}")
    missing = sorted(set(required) - set(value))
    if missing:
        raise error(f"{label} is incomplete. Missing field(s): {missing}")
    return value


def timeout_of(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be float")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be above 0")
    return float(value)


def port_of(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be int")
    if not 0 <= value <= 65535:
        raise ValueError(f"{label} must be at least 0 and at most 65535")
    return value


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
