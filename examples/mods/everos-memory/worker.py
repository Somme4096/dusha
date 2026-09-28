from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


class Worker:
    def __init__(self, path: str | Path, options: dict[str, Any] | None = None):
        from memory.database import Database
        from memory.everos import EverOSMirror

        self.database = Database(path)
        self.everos = EverOSMirror(self.database, (options or {}).get("everos", {}))

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        options = params.get("options", {})
        params = params.get("request", params)
        if method == "ingest_messages":
            return self._ingest_messages(params)
        if method == "inject_context":
            return self._inject_context(params)
        if method == "status":
            return {"status": self._status()}
        if method == "backfill_once":
            return {"status": self._backfill_once()}
        if method == "rebuild_index":
            return {"status": self._rebuild_index()}
        if method == "rebuild_chunks":
            return {"status": self._rebuild_chunks()}
        if method == "ensure_message_chunks":
            return {"chunks": 0}
        if method == "match_phrase":
            return self._match_phrase(options, params)
        if method == "close":
            self.everos.close()
            return None
        raise ValueError(f"unknown memory method: {method}")

    def _ingest_messages(self, params: dict[str, Any]) -> dict[str, Any]:
        messages = params.get("messages") or ()
        highest = 0
        for item in messages:
            if not isinstance(item, dict):
                continue
            try:
                highest = max(highest, int(item["id"]))
            except (KeyError, TypeError, ValueError):
                continue
        self.everos.enqueue_messages(messages)
        try:
            self.everos.deliver_pending(1)
        except Exception:
            pass
        return {"highest_id": highest}

    def _inject_context(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.everos.inject_context(
            query=str(params.get("query", "")),
            scope=str(params.get("scope", "")),
            harness=str(params.get("harness", "")),
            conversation_id=str(params.get("conversation_id", "")),
            max_chars=int(params.get("max_chars", 0) or 0),
        )

    def _status(self) -> dict[str, Any]:
        return {
            "mode": "lexical",
            "retrieval": "lexical",
            "enabled": True,
            "plugin": "everos",
            "everos": self.everos.status(),
        }

    def _backfill_once(self) -> dict[str, Any]:
        return {
            "retrieval": "lexical",
            "enabled": True,
            "plugin": "everos",
            "everos": self.everos.backfill_once(),
        }

    @staticmethod
    def _rebuild_index() -> dict[str, Any]:
        return {"status": "rebuilt", "scope": "everos", "rebuilt": True}

    @staticmethod
    def _rebuild_chunks() -> dict[str, Any]:
        return {"status": "rebuilt", "scope": "everos", "chunks": 0}

    @staticmethod
    def _match_phrase(options: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        message = str(params["message"]).casefold()
        for phrase in options.get("phrase_patterns", []):
            if str(phrase["pattern"]).casefold() in message:
                return {"deltas": phrase["deltas"]}
        return {"deltas": None}


def main() -> None:
    parser = argparse.ArgumentParser(description="EverOS memory plugin worker")
    parser.add_argument("database", nargs="?", default="memory.sqlite3")
    args = parser.parse_args()
    worker: Worker | None = None
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if worker is None:
                worker = Worker(args.database, request.get("params", {}).get("options", {}))
            result = worker.dispatch(request["method"], request.get("params") or {})
            response = {"id": request.get("id"), "result": result}
        except Exception as error:
            error_type = type(error).__name__
            response = {
                "id": request.get("id") if "request" in locals() else None,
                "error": str(error),
                "error_type": error_type,
            }
        sys.stdout.write(json.dumps(response, default=str, separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
