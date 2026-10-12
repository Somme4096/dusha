from __future__ import annotations

from typing import Any

from memory.database import Database
from memory.everos import EverOSMirror

__all__ = ["start", "on_message", "on_context", "decide", "tick", "status", "close"]

_mirror: EverOSMirror | None = None


def start(options: dict[str, Any]) -> None:
    global _mirror
    _mirror = EverOSMirror(Database(options["database_path"]), options.get("everos", {}))


def on_message(message: dict[str, Any], options: dict[str, Any]) -> None:
    _mirror.enqueue_messages([message])
    try:
        _mirror.deliver_pending(1)
    except Exception:
        pass


def on_context(request: Any, options: dict[str, Any]) -> dict[str, Any]:
    return _mirror.inject_context(
        query=str(request.query),
        scope=str(request.conversation_id or request.harness),
        harness=str(request.harness),
        conversation_id=str(request.conversation_id),
        max_chars=int(options["context_max_chars"]),
    )


def decide(request: Any, options: dict[str, Any]) -> dict[str, Any] | None:
    message = str(request.message).casefold()
    for phrase in options.get("phrase_patterns", []):
        if str(phrase["pattern"]).casefold() in message:
            return {"deltas": phrase["deltas"]}
    return None


def tick(options: dict[str, Any]) -> dict[str, Any]:
    return _mirror.backfill_once()


def status(options: dict[str, Any]) -> dict[str, Any]:
    return _mirror.status()


def close(options: dict[str, Any]) -> None:
    _mirror.close()
