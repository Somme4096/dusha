"""Shared compact JSON serialization and canonical fingerprints (stdlib only).

The companion gateway needs one compact, deterministic JSON encoding for
injected context, stored payloads, and fingerprinting. This module owns that
encoding so no caller re-implements the compact separators, the ``& < >``
escaping that keeps injected JSON from breaking out of delimiter blocks, or the
canonical sort-keyed fingerprint used to detect effective-config changes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def compact_json(value: Any, *, sort_keys: bool = False, ensure_ascii: bool = False) -> str:
    """Serialize to compact JSON.

    ``ensure_ascii=False`` keeps non-ASCII text readable and is the shared
    default; callers that must match legacy ``json.dumps`` output (which
    defaulted to ``ensure_ascii=True``) pass ``ensure_ascii=True`` explicitly.
    """
    return json.dumps(
        value,
        ensure_ascii=ensure_ascii,
        separators=(",", ":"),
        sort_keys=sort_keys,
    )


def safe_json(value: Any, *, ensure_ascii: bool = False) -> str:
    """Serialize to compact JSON with ``& < >`` escaped as ``\\u00xx``.

    Delimiter-like text inside injected JSON is escaped so a literal
    ``</companion_state>`` in stored content cannot terminate the JSON block.
    """
    return (
        compact_json(value, ensure_ascii=ensure_ascii)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def canonical(value: Any, *, ensure_ascii: bool = False) -> str:
    """Canonical sort-keyed compact JSON used for stable fingerprints."""
    return compact_json(value, sort_keys=True, ensure_ascii=ensure_ascii)


def fingerprint(value: Any, *, ensure_ascii: bool = False) -> str:
    """Stable ``sha256:``-prefixed canonical fingerprint."""
    raw = canonical(value, ensure_ascii=ensure_ascii)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def short_fingerprint(value: Any, *, ensure_ascii: bool = False) -> str:
    """Unprefixed 24-character canonical fingerprint (semantic index keys)."""
    raw = canonical(value, ensure_ascii=ensure_ascii)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]