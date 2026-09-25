from __future__ import annotations

import hashlib
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


def fingerprint(value: Any, *, ensure_ascii: bool = False) -> str:
    raw = canonical(value, ensure_ascii=ensure_ascii)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def short_fingerprint(value: Any, *, ensure_ascii: bool = False) -> str:
    raw = canonical(value, ensure_ascii=ensure_ascii)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]