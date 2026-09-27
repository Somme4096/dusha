from __future__ import annotations

import dataclasses
from threading import Lock
from typing import Any

from worker import Worker

__all__ = [
    "ingest", "get", "conversation_id", "search", "context", "recent",
    "rebuild_index", "recall", "remember", "list_current", "history", "revise",
    "forget", "render_facts", "status", "backfill_once", "ensure_message_chunks",
    "rebuild_chunks", "close", "match_phrase",
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
    return {name: getattr(request, name) for name in dir(request) if not name.startswith("_") and not callable(getattr(request, name))}


def _call(method: str, request: Any = None, options: dict[str, Any] | None = None) -> Any:
    global _worker
    options = options or {}
    with _lock:
        if _worker is None:
            _worker = Worker(options["database_path"], options)
        return _worker.dispatch(method, {"request": _value(request), "options": options})


def _export(method: str):
    return lambda request=None, options=None: _call(method, request, options)


ingest = _export("ingest")
get = _export("get")
conversation_id = _export("conversation_id")
search = _export("search")
context = _export("context")
recent = _export("recent")
rebuild_index = _export("rebuild_index")
recall = _export("recall")
remember = _export("remember")
list_current = _export("list_current")
history = _export("history")
revise = _export("revise")
forget = _export("forget")
render_facts = _export("render_facts")
status = _export("status")
backfill_once = _export("backfill_once")
ensure_message_chunks = _export("ensure_message_chunks")
rebuild_chunks = _export("rebuild_chunks")
close = _export("close")
match_phrase = _export("match_phrase")
