from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from importlib import metadata as _metadata
from typing import IO, Any

import httpx

from .config import AppConfig
from .outbox import Outbox

logger = logging.getLogger("dusha.mcp")

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    method: str
    path: str
    properties: dict[str, Any]
    required: tuple[str, ...] = ()
    queued: bool = False
    path_params: tuple[str, ...] = ()

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {"type": "object", "properties": self.properties, "required": list(self.required)},
        }


def _field(kind: str, description: str) -> dict[str, str]:
    return {"type": kind, "description": description}


_FACT = {
    "text": _field("string", "Complete fact text."),
    "priority": _field("integer", "Priority from 0 to 100."),
    "reason": _field("string", "Why the fact changes."),
    "review_after": _field("string", "ISO timestamp to review the fact after."),
    "expires_at": _field("string", "ISO timestamp the fact expires at."),
}

TOOLS = (
    Tool(
        "memory_search",
        "Search the companion's conversation archive for past exchanges.",
        "POST",
        "/state/v1/memory/search",
        {
            "query": _field("string", "Search text."),
            "limit": _field("integer", "Maximum number of hits."),
            "context_messages": _field("integer", "Neighbor messages to include with each hit."),
        },
        ("query",),
    ),
    Tool(
        "memory_get",
        "Read one archived message with its neighbors.",
        "GET",
        "/state/v1/memory/{message_id}",
        {
            "message_id": _field("integer", "Message id from a search hit."),
            "context_messages": _field("integer", "Neighbor messages on each side."),
        },
        ("message_id",),
        path_params=("message_id",),
    ),
    Tool(
        "evergreen_list",
        "List the evergreen facts the companion always remembers.",
        "GET",
        "/state/v1/evergreen/facts",
        {
            "include_inactive": _field("boolean", "Include forgotten and expired facts."),
            "due_only": _field("boolean", "Only facts due for review."),
            "limit": _field("integer", "Maximum number of facts."),
        },
    ),
    Tool(
        "evergreen_remember",
        "Store a new evergreen fact the companion should always remember.",
        "POST",
        "/state/v1/evergreen/facts",
        {"key": _field("string", "Stable fact key such as user.name."), **_FACT},
        ("key", "text"),
        queued=True,
    ),
    Tool(
        "evergreen_revise",
        "Replace an evergreen fact at the revision you last read.",
        "POST",
        "/state/v1/evergreen/facts/{fact_id}/revisions",
        {
            "fact_id": _field("string", "Fact id."),
            "expected_revision": _field("integer", "Revision you last read."),
            **_FACT,
        },
        ("fact_id", "expected_revision", "text"),
        path_params=("fact_id",),
    ),
    Tool(
        "evergreen_forget",
        "Forget an evergreen fact at the revision you last read.",
        "POST",
        "/state/v1/evergreen/facts/{fact_id}/forget",
        {
            "fact_id": _field("string", "Fact id."),
            "expected_revision": _field("integer", "Revision you last read."),
            "reason": _field("string", "Why the fact is forgotten."),
        },
        ("fact_id", "expected_revision", "reason"),
        path_params=("fact_id",),
    ),
    Tool(
        "memo_add",
        "Add a short memo the companion keeps in view until it is done.",
        "POST",
        "/state/v1/memo/add",
        {"text": _field("string", "Memo text.")},
        ("text",),
        queued=True,
    ),
    Tool(
        "memo_list",
        "List memos.",
        "GET",
        "/state/v1/memo/list",
        {
            "status": _field("string", "active or archived."),
            "limit": _field("integer", "Maximum number of memos."),
        },
    ),
    Tool(
        "memo_done",
        "Archive a memo with the reason it is done.",
        "POST",
        "/state/v1/memo/{note_id}/done",
        {"note_id": _field("string", "Memo id."), "reason": _field("string", "Why the memo is done.")},
        ("note_id", "reason"),
        path_params=("note_id",),
    ),
    Tool("affect_status", "Read the companion's current affect state.", "GET", "/state/v1/affect", {}),
)
_BY_NAME = {tool.name: tool for tool in TOOLS}


class McpServer:
    def __init__(self, config: AppConfig):
        server = config.proxy.server
        self.client = httpx.Client(
            base_url=server.url,
            timeout=server.timeout_seconds,
            headers={"x-companion-token": server.token} if server.token else {},
        )
        self.outbox = Outbox(config.data_dir)

    def close(self) -> None:
        self.client.close()

    def serve(self, reader: IO[str], writer: IO[str]) -> None:
        for line in reader:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                reply = _error(None, -32700, "parse error")
            else:
                reply = self.handle(message) if isinstance(message, dict) else _error(None, -32600, "invalid")
            if reply is not None:
                writer.write(json.dumps(reply, ensure_ascii=False) + "\n")
                writer.flush()

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method, ident, params = message.get("method"), message.get("id"), message.get("params") or {}
        if "id" not in message:
            return None
        if method == "initialize":
            asked = params.get("protocolVersion")
            return _result(
                ident,
                {
                    "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "dusha", "version": _metadata.version("dusha")},
                },
            )
        if method == "ping":
            return _result(ident, {})
        if method == "tools/list":
            return _result(ident, {"tools": [tool.schema() for tool in TOOLS]})
        if method == "tools/call":
            tool = _BY_NAME.get(params.get("name"))
            if tool is None:
                return _error(ident, -32602, f"unknown tool {params.get('name')!r}")
            return _result(ident, self.call(tool, params.get("arguments") or {}))
        return _error(ident, -32601, f"method not found: {method}")

    def call(self, tool: Tool, arguments: dict[str, Any]) -> dict[str, Any]:
        missing = [name for name in tool.required if name not in arguments]
        if missing:
            return _content({"error": f"missing argument(s): {missing}"}, error=True)
        values = {key: value for key, value in arguments.items() if key in tool.properties}
        path = tool.path.format(**{name: values.pop(name) for name in tool.path_params})
        self.outbox.flush(self.client)
        try:
            if tool.method == "GET":
                response = self.client.get(path, params=values)
            else:
                response = self.client.post(path, json=values)
        except httpx.RequestError as error:
            if tool.queued:
                self.outbox.put(path, values)
                return _content({"queued": True, "detail": "the server is unreachable, sent once it is back"})
            return _content({"error": f"the server is unreachable: {error}"}, error=True)
        try:
            data = response.json()
        except ValueError:
            data = {"detail": response.text}
        return _content(data, error=response.status_code >= 400)


def _content(data: Any, *, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "isError": error}


def _result(ident: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _error(ident: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}
