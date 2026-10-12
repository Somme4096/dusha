from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from importlib import metadata as _metadata
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import schema as _schema
from .api_openai import PathTokenMiddleware, create_openai_router
from .api_state import create_state_router
from .config import AppConfig, load_config
from .service import CompanionService, FeatureDisabledError

logger = logging.getLogger("dusha")


async def _scheduler(step: Callable[[], Any], interval: int, label: str) -> None:
    while True:
        try:
            await asyncio.to_thread(step)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s failed", label)
        await asyncio.sleep(max(1, interval))


def create_app(config: AppConfig | None = None) -> FastAPI:
    cfg = config or load_config()
    expected_token = cfg.api_token
    auth_enabled = bool(expected_token)
    service = CompanionService(cfg)
    proactive = service.proactive

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        tasks = []
        if cfg.features.proactive:
            interval = max(5, cfg.proactive.poll_interval_seconds)
            tasks.append(
                asyncio.create_task(
                    _scheduler(proactive.evaluate, interval, "proactive evaluation"),
                    name="proactive-evaluator",
                )
            )
        if service.mods:
            tasks.append(
                asyncio.create_task(
                    _scheduler(service.tick_mods, cfg.mods.tick_interval_seconds, "mod tick"),
                    name="mod-tick",
                )
            )
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
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
            {"name": "proxy", "description": "https://github.com/Somme4096/dusha"},
        ],
    )
    app.state.config = cfg
    app.state.service = service
    app.state.proactive = proactive

    async def authorized(x_companion_token: str = Header(default="")) -> None:
        if not auth_enabled:
            return
        if not x_companion_token or not secrets.compare_digest(
            x_companion_token.encode("utf-8"), expected_token.encode("utf-8")
        ):
            raise HTTPException(status_code=401, detail="invalid companion token")

    @app.exception_handler(FeatureDisabledError)
    async def feature_disabled(_: Request, error: FeatureDisabledError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(error)})

    @app.get("/health", tags=["health"], response_model=_schema.HealthResponse)
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "companion": cfg.home.name if cfg.home else "",
            "database": await asyncio.to_thread(service.database.integrity_check),
            "upstream_configured": bool(cfg.upstream.base_url),
            "mods": [mod.name for mod in service.mods],
        }

    app.include_router(create_state_router(service, proactive, authorized))
    if cfg.api_openai.enabled:
        app.include_router(create_openai_router(cfg, service, authorized))
        app.add_middleware(PathTokenMiddleware)
    return app
