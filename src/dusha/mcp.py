from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import metadata as _metadata
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from .commands import Arg, CommandError, check_args, message, validate

logger = logging.getLogger("dusha.mcp")

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    handler: Callable[..., Any]
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


class Tools:
    def __init__(self) -> None:
        self.entries: dict[str, Tool] = {}

    def tool(
        self,
        name: str,
        handler: Callable[..., Any],
        *,
        help: str = "",
        args: Iterable[Arg] = (),
        not_found: str = "not found",
    ) -> None:
        if not NAME.fullmatch(name):
            raise ValueError(f"tool name must be letters, digits, dash, dot, or underscore, got {name!r}")
        if name in self.entries:
            raise ValueError(f"tool {name!r} is already registered")
        args = tuple(args)
        check_args(f"tool {name!r}", args)
        self.entries[name] = Tool(name, help, handler, args, not_found)


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

    async def call(tool: Tool, arguments: Any) -> dict[str, Any]:
        known = {item.name for item in tool.args}
        try:
            given = {key: value for key, value in arguments.items() if key in known}
            values = validate(tool.args, given)
            data = await asyncio.to_thread(tool.handler, **values)
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
            return _result(ident, {"tools": [tool.schema() for tool in service.tools.entries.values()]})
        if method == "tools/call":
            name = params.get("name")
            tool = service.tools.entries.get(name) if isinstance(name, str) else None
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
