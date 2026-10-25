from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata as _metadata
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from .commands import Arg, CommandError, arg, message, option, validate
from .evergreen import REVISE_ARGS, revision

logger = logging.getLogger("dusha.mcp")

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    plugin: str
    description: str
    run: Callable[..., Any]
    args: tuple[Arg, ...] = ()
    not_found: str = "not found"

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": {item.name: item.schema() for item in self.args},
                "required": [item.name for item in self.args if item.required],
            },
        }


def _search(service: Any, query: str, limit: int, context_messages: int) -> dict[str, Any]:
    hits = service.messages.search(query, max(1, min(limit, 50)), max(0, min(context_messages, 10)))
    return {"results": hits}


def _record(service: Any, message_id: int, context_messages: int) -> dict[str, Any]:
    messages = service.messages.context(message_id, max(0, min(context_messages, 10)))
    if not messages:
        raise KeyError(message_id)
    return {"messages": messages}


def _facts(service: Any, **values: Any) -> dict[str, Any]:
    return {"facts": service.get("evergreen").list_current(**values)}


def _remember(service: Any, **values: Any) -> dict[str, Any]:
    return {"fact": service.get("evergreen").remember(**values, created_by="agent")}


def _revise(service: Any, **values: Any) -> dict[str, Any]:
    return {"fact": service.get("evergreen").revise(**revision(values), created_by="agent")}


def _forget(service: Any, **values: Any) -> dict[str, Any]:
    return {"fact": service.get("evergreen").forget(**values, created_by="agent")}


_FACT = (
    option("priority", int, default=50, help="Priority from 0 to 100."),
    option("reason", default="", help="Why the fact changes."),
    option("review_after", help="ISO timestamp to review the fact after."),
    option("expires_at", help="ISO timestamp the fact expires at."),
)

TOOLS = (
    Tool(
        "memory_search",
        "memory",
        "Search the companion's conversation archive for past exchanges.",
        _search,
        (
            arg("query", help="Search text."),
            option("limit", int, default=8, help="Maximum number of hits."),
            option("context_messages", int, default=1, help="Neighbor messages to include with each hit."),
        ),
    ),
    Tool(
        "memory_get",
        "memory",
        "Read one archived message with its neighbors.",
        _record,
        (
            arg("message_id", int, help="Message id from a search hit."),
            option("context_messages", int, default=1, help="Neighbor messages on each side."),
        ),
        "memory record not found",
    ),
    Tool(
        "evergreen_list",
        "evergreen",
        "List the evergreen facts the companion always remembers.",
        _facts,
        (
            option("include_inactive", bool, help="Include forgotten and expired facts."),
            option("due_only", bool, help="Only facts due for review."),
            option("limit", int, default=100, help="Maximum number of facts."),
        ),
    ),
    Tool(
        "evergreen_remember",
        "evergreen",
        "Store a new evergreen fact the companion should always remember.",
        _remember,
        (
            arg("key", help="Stable fact key such as user.name."),
            arg("text", help="Complete fact text."),
            *_FACT,
        ),
    ),
    Tool(
        "evergreen_revise",
        "evergreen",
        "Replace an evergreen fact at the revision you last read.",
        _revise,
        REVISE_ARGS,
        "evergreen fact not found",
    ),
    Tool(
        "evergreen_forget",
        "evergreen",
        "Forget an evergreen fact at the revision you last read.",
        _forget,
        (
            arg("fact_id", help="Fact id."),
            arg("expected_revision", int, help="Revision you last read."),
            arg("reason", help="Why the fact is forgotten."),
        ),
        "evergreen fact not found",
    ),
    Tool(
        "memo_add",
        "memo",
        "Add a short memo the companion keeps in view until it is done.",
        lambda service, text: {"memo": service.get("memo").add(text)},
        (arg("text", help="Memo text."),),
    ),
    Tool(
        "memo_list",
        "memo",
        "List memos, oldest first.",
        lambda service, status, limit: {"memos": service.get("memo").list(status, limit)},
        (
            option("status", choices=("active", "archived"), default="active", help="Which memos to list."),
            option("limit", int, default=20, help="Maximum number of memos."),
        ),
    ),
    Tool(
        "memo_done",
        "memo",
        "Archive a memo with the reason it is done.",
        lambda service, note_id, reason: {"memo": service.get("memo").done(note_id, reason)},
        (arg("note_id", int, help="Memo id."), arg("reason", help="Why the memo is done.")),
        "memo not found",
    ),
    Tool(
        "affect_status",
        "affect",
        "Read the companion's current affect state.",
        lambda service: service.get("affect").status(),
    ),
)


def _content(data: Any, *, error: bool = False) -> dict[str, Any]:
    text = json.dumps(data, ensure_ascii=False, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _result(ident: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _error(ident: Any, code: int, text: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": text}}


def _refused(request: Request) -> Response | None:
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).hostname not in LOOPBACK:
        return JSONResponse({"detail": "origin not allowed"}, status_code=403)
    version = request.headers.get("mcp-protocol-version")
    if version and version not in PROTOCOL_VERSIONS:
        return JSONResponse({"detail": f"unsupported MCP protocol version {version!r}"}, status_code=400)
    return None


def create_mcp_router(service: Any, authorized: Callable[..., Any]) -> APIRouter:
    router = APIRouter(dependencies=[Depends(authorized)])

    def tools() -> dict[str, Tool]:
        active = {runtime.name for runtime in service.registry.runtimes if runtime.state == "active"}
        return {tool.name: tool for tool in TOOLS if tool.plugin in active}

    async def call(tool: Tool, arguments: Any) -> dict[str, Any]:
        known = {item.name for item in tool.args}
        try:
            given = {key: value for key, value in arguments.items() if key in known}
            values = validate(tool.args, given)
            data = await asyncio.to_thread(tool.run, service, **values)
        except KeyError:
            return _content({"detail": tool.not_found}, error=True)
        except (CommandError, ValueError) as error:
            return _content({"detail": message(error)}, error=True)
        except Exception:
            logger.exception("%s failed", tool.name)
            return _content({"detail": f"{tool.name} failed, see the server log"}, error=True)
        return _content(data)

    async def handle(item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return _error(None, -32600, "invalid request")
        if "method" not in item or "id" not in item:
            return None
        method, ident = item["method"], item["id"]
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
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
            return _result(ident, {"tools": [tool.schema() for tool in tools().values()]})
        if method == "tools/call":
            name = params.get("name")
            tool = tools().get(name) if isinstance(name, str) else None
            if tool is None:
                return _error(ident, -32602, f"unknown tool {name!r}")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            return _result(ident, await call(tool, arguments))
        return _error(ident, -32601, f"method not found: {method}")

    @router.post("/mcp", include_in_schema=False)
    async def post(request: Request) -> Response:
        refused = _refused(request)
        if refused is not None:
            return refused
        try:
            body = json.loads(await request.body())
        except ValueError:
            return JSONResponse(_error(None, -32700, "parse error"), status_code=400)
        if isinstance(body, list):
            replies = [reply for item in body if (reply := await handle(item)) is not None]
        else:
            replies = await handle(body)
        if not replies:
            return Response(status_code=202)
        return JSONResponse(replies)

    @router.api_route("/mcp", methods=["GET", "DELETE"], include_in_schema=False)
    async def other() -> Response:
        return Response(status_code=405, headers={"Allow": "POST"})

    return router
