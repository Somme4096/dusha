from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from . import schema as _schema

DOCS = "https://github.com/Somme4096/dusha"
ERROR_DOCS = {
    status: {"model": _schema.ErrorDetail, "description": DOCS} for status in (401, 404, 409, 422, 503)
}


class GatewayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CommandError(RuntimeError):
    pass


def errors(*statuses: int) -> dict[int, Any]:
    return {status: ERROR_DOCS[status] for status in (401, *statuses)}


async def call(
    function: Callable[..., Any],
    /,
    *args: Any,
    errors: tuple[type[Exception], ...] = (),
    not_found: str | None = None,
    conflict: type[Exception] | tuple[type[Exception], ...] = (),
    invalid: int = 422,
    **kwargs: Any,
) -> Any:
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except Exception as error:
        if not isinstance(error, errors):
            raise
        if isinstance(error, KeyError):
            raise HTTPException(status_code=404, detail=not_found or "resource not found") from error
        if isinstance(error, conflict):
            raise HTTPException(status_code=409, detail=str(error)) from error
        if isinstance(error, ValueError):
            raise HTTPException(status_code=invalid, detail=str(error)) from error
        raise


class Commands:
    def __init__(self, parser: argparse.ArgumentParser | None = None):
        self.parser = parser or argparse.ArgumentParser(prog="dusha")
        self.subparsers = self.parser.add_subparsers(dest="command", required=True)

    def command(self, name: str, help: str) -> argparse.ArgumentParser:
        return self.subparsers.add_parser(name, help=help)
