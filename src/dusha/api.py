from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from importlib import metadata as _metadata
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException

from . import schema as _schema
from .api_state import create_state_router
from .commands import CommandError, message, validate
from .config import AppConfig, load_config
from .hooks import Registry
from .mcp import create_mcp_router
from .service import CompanionService

logger = logging.getLogger("dusha")


async def _scheduler(step: Callable[[], Any], interval: float, label: str) -> None:
    while True:
        try:
            await asyncio.to_thread(step)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s failed", label)
        await asyncio.sleep(max(1, interval))


async def _jobs(registry: Registry) -> None:
    tasks: dict[int, asyncio.Task] = {}
    try:
        while True:
            for job in list(registry.jobs):
                if id(job) not in tasks:
                    label = f"{job.plugin} interval"
                    tasks[id(job)] = asyncio.create_task(_scheduler(job.callback, job.seconds, label))
            await asyncio.sleep(1)
    finally:
        for task in tasks.values():
            task.cancel()
        for task in tasks.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task


def create_app(config: AppConfig | None = None) -> FastAPI:
    cfg = config or load_config()
    expected_token = cfg.api_token
    auth_enabled = bool(expected_token)
    service = CompanionService(cfg)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        jobs = asyncio.create_task(_jobs(service.registry), name="plugin-intervals")
        try:
            yield
        finally:
            jobs.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await jobs
            service.close()

    app = FastAPI(
        title="Dusha",
        version=_metadata.version("dusha"),
        description="https://github.com/Somme4096/dusha",
        lifespan=lifespan,
        docs_url=None if auth_enabled else "/docs",
        redoc_url=None if auth_enabled else "/redoc",
        openapi_url=None if auth_enabled else "/openapi.json",
        openapi_tags=[
            {"name": "health", "description": "https://github.com/Somme4096/dusha"},
            {"name": "state", "description": "https://github.com/Somme4096/dusha"},
            {"name": "cli", "description": "https://github.com/Somme4096/dusha"},
        ],
    )
    app.state.config = cfg
    app.state.service = service

    async def authorized(x_companion_token: str = Header(default="")) -> None:
        if not auth_enabled:
            return
        if not x_companion_token or not secrets.compare_digest(
            x_companion_token.encode("utf-8"), expected_token.encode("utf-8")
        ):
            raise HTTPException(status_code=401, detail="invalid companion token")

    @app.get("/health", tags=["health"], response_model=_schema.HealthResponse)
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "companion": cfg.home.name if cfg.home else "",
            "database": await asyncio.to_thread(service.database.integrity_check),
            "plugins": [
                runtime.name for runtime in service.registry.runtimes if runtime.state == "active"
            ],
        }

    @app.get("/cli/v1/commands", tags=["cli"], dependencies=[Depends(authorized)])
    async def cli_commands() -> dict[str, Any]:
        states = {name: status["state"] for name, status in service.plugins_status().items()}
        return {**service.commands.manifest(), "plugins": states}

    @app.post("/cli/v1/run/{path:path}", tags=["cli"], dependencies=[Depends(authorized)])
    async def cli_run(path: str, body: Annotated[dict[str, Any] | None, Body()] = None) -> Any:
        command = service.commands.find(tuple(path.split("/")))
        if command is None:
            raise HTTPException(status_code=404, detail=f"unknown command: {path.replace('/', ' ')}")
        try:
            values = validate(command.args, body or {})
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        try:
            return await asyncio.to_thread(command.handler, **values)
        except (CommandError, LookupError, ValueError) as error:
            raise HTTPException(status_code=400, detail=message(error)) from error

    app.include_router(create_state_router(service, authorized))
    app.include_router(create_mcp_router(service, authorized))
    return app
