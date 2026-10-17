from __future__ import annotations

from typing import Any

from .database import Database
from .everos import EverOSMirror


def apply(ctx: Any, config: dict[str, Any]) -> None:
    mirror = EverOSMirror(Database(ctx.data_dir / "everos.sqlite3"), config.get("everos", {}))
    ctx.on("message", lambda message: _mirror(mirror, message))
    ctx.on("context", lambda request: _recall(mirror, request, config))
    ctx.on("decide", lambda request: _phrase(request, config))
    ctx.interval(float(config.get("interval_seconds", 10)), mirror.backfill_once)
    ctx.on_dispose(mirror.close)


def _mirror(mirror: EverOSMirror, message: dict[str, Any]) -> None:
    mirror.enqueue_messages([message])
    try:
        mirror.deliver_pending(1)
    except Exception:
        pass


def _recall(mirror: EverOSMirror, request: Any, config: dict[str, Any]) -> dict[str, Any]:
    return mirror.inject_context(
        query=str(request.query),
        scope=str(request.conversation_id or request.harness),
        harness=str(request.harness),
        conversation_id=str(request.conversation_id),
        max_chars=int(config["context_max_chars"]),
    )


def _phrase(request: Any, config: dict[str, Any]) -> dict[str, Any] | None:
    message = str(request.message).casefold()
    for phrase in config.get("phrase_patterns", []):
        if str(phrase["pattern"]).casefold() in message:
            return {"deltas": phrase["deltas"]}
    return None
