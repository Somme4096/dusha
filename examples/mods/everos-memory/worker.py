from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields
from pathlib import Path
from typing import Any

class Worker:
    def __init__(self, path: str | Path, options: dict[str, Any] | None = None):
        from memory.config import MemoryConfig
        from memory.database import Database
        from memory.evergreen import EvergreenStore
        from memory.semantic import SemanticIndex
        from memory.store import MemoryStore
        from memory.everos import EverOSMirror

        self.database = Database(path)
        self.config = _memory_config(MemoryConfig, options or {})
        self.semantic = SemanticIndex(self.database, self.config)
        self.everos = EverOSMirror(self.database, (options or {}).get("everos", {}))
        self.store = MemoryStore(self.database, self.semantic, self.everos)
        self.evergreen = EvergreenStore(self.database)

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        options = params.get("options", {})
        params = params.get("request", params)
        if method == "ingest":
            result = self.store.ingest(**params)
            self.everos.deliver_pending(1)
            return _message(result)
        if method == "get":
            return {"message": self.store.get(int(params["message_id"]))}
        if method == "conversation_id":
            return {"conversation_id": self.store.conversation_id(params["harness"], params["conversation_id"])}
        if method == "search":
            return {"results": self.store.search(**params)}
        if method == "context":
            return {"messages": self.store.context(params["message_id"], params.get("context_messages", 1))}
        if method == "recent":
            internal_id = self.store.conversation_id(params["harness"], params["conversation_id"])
            return {"messages": self.store.recent(
                internal_id, params.get("limit", 5), params.get("exclude_ids", ()),
            )}
        if method == "rebuild_index":
            return {"status": self.store.rebuild_index()}
        if method == "recall":
            return self._recall(params)
        if method == "remember":
            return {"fact": self.evergreen.remember(**params)}
        if method == "list_current":
            return {"facts": self.evergreen.list_current(**params)}
        if method == "history":
            return {"revisions": self.evergreen.history(params["fact_id"])}
        if method == "revise":
            return {"fact": self.evergreen.revise(**params)}
        if method == "forget":
            return {"fact": self.evergreen.forget(**params)}
        if method == "render_facts":
            rendered, facts = self.evergreen.render(**params)
            return {"rendered": rendered, "facts": facts}
        if method == "status":
            return {"status": {**self.semantic.status(), "everos": self.everos.status()}}
        if method == "backfill_once":
            status = self.semantic.backfill_once(**params)
            status["everos"] = self.everos.deliver_pending(1)
            return {"status": status}
        if method == "ensure_message_chunks":
            return {"chunks": self.semantic.ensure_message_chunks(params["message_id"])}
        if method == "rebuild_chunks":
            return {"status": self.semantic.rebuild_chunks()}
        if method == "close":
            self.semantic.close()
            self.everos.close()
            return None
        if method == "match_phrase":
            message = str(params["message"]).casefold()
            for phrase in options.get("phrase_patterns", []):
                if str(phrase["pattern"]).casefold() in message:
                    return {"deltas": phrase["deltas"]}
            return {"deltas": None}
        raise ValueError(f"unknown memory method: {method}")

    def _recall(self, params: dict[str, Any]) -> dict[str, Any]:
        search_params = {
            "query": params["query"],
            "limit": params.get("search_hits", 10),
            "context_messages": params.get("context_messages", 3),
            "exclude_ids": params.get("exclude_ids", ()),
        }
        search_hits = self.store.search(**search_params)
        records: list[dict[str, Any]] = []
        seen: set[int] = set()
        if params.get("include_recent", True):
            internal_id = self.store.conversation_id(params["harness"], params["conversation_id"])
            recent = self.store.recent(
                internal_id, params.get("recent_messages", 5), params.get("exclude_ids", ()),
            )
            for message in recent:
                if message["id"] not in seen:
                    records.append({**message, "source": "recent"})
                    seen.add(message["id"])
        for hit in search_hits:
            for message in hit["messages"]:
                if message["id"] not in seen and message["id"] not in params.get("exclude_ids", ()):
                    records.append({**message, "source": "recalled"})
                    seen.add(message["id"])
        return {"records": records, "search_hits": search_hits}


def _message(value: Any) -> Any:
    return value.__dict__ if hasattr(value, "__dict__") else {
        "id": value.id, "duplicate": value.duplicate,
        "conversation_id": value.conversation_id, "sha256": value.sha256,
    }


def _memory_config(memory_config_type: Any, options: dict[str, Any]) -> Any:
    config_options = options.get("memory", options)
    config = memory_config_type()
    for item in fields(config):
        if item.name == "embedding":
            continue
        if item.name in config_options:
            setattr(config, item.name, config_options[item.name])
    embedding_options = config_options.get("embedding", {})
    for item in fields(config.embedding):
        if item.name in embedding_options:
            setattr(config.embedding, item.name, embedding_options[item.name])
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLite memory worker")
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
