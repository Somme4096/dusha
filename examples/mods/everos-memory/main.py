from __future__ import annotations

import dataclasses
from threading import Lock
from typing import Any

from worker import Worker

__all__ = [
    "ingest_messages", "inject_context", "status", "backfill_once",
    "rebuild_index", "rebuild_chunks", "ensure_message_chunks",
    "match_phrase", "close",
]

_lock = Lock()
_worker: Worker | None = None


def _value(request: Any) -> dict[str, Any]:
    if request is None:
        return {}
    if dataclasses.is_dataclass(request):
        return dataclasses.asdict(request)
    if isinstance(request, dict):
        return dict(request)
    return {
        name: getattr(request, name)
        for name in dir(request)
        if not name.startswith("_") and not callable(getattr(request, name))
    }


def _call(method: str, request: Any = None, options: dict[str, Any] | None = None) -> Any:
    global _worker
    options = options or {}
    with _lock:
        if _worker is None:
            _worker = Worker(options["database_path"], options)
        return _worker.dispatch(method, {"request": _value(request), "options": options})


def _export(method: str):
    return lambda request=None, options=None: _call(method, request, options)


ingest_messages = _export("ingest_messages")
inject_context = _export("inject_context")
status = _export("status")
backfill_once = _export("backfill_once")
rebuild_index = _export("rebuild_index")
rebuild_chunks = _export("rebuild_chunks")
ensure_message_chunks = _export("ensure_message_chunks")
match_phrase = _export("match_phrase")
close = _export("close")
