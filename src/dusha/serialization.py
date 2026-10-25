from __future__ import annotations

import json
from typing import Any


def compact_json(value: Any, *, sort_keys: bool = False, ensure_ascii: bool = False) -> str:
    return json.dumps(
        value,
        ensure_ascii=ensure_ascii,
        separators=(",", ":"),
        sort_keys=sort_keys,
    )


def safe_json(value: Any, *, ensure_ascii: bool = False) -> str:
    return (
        compact_json(value, ensure_ascii=ensure_ascii)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def canonical(value: Any, *, ensure_ascii: bool = False) -> str:
    return compact_json(value, sort_keys=True, ensure_ascii=ensure_ascii)
