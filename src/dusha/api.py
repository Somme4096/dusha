from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from importlib import metadata as _metadata
from typing import Any

from fastapi import FastAPI, Header, HTTPException

from . import schema as _schema
from .api_state import create_state_router
from .config import AppConfig, load_config
from .hooks import Registry
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

    app.include_router(create_state_router(service, authorized))
    return app
