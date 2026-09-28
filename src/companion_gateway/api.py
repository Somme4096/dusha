from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException

from . import schema as _schema
from .api_openai import create_openai_router
from .api_state import create_state_router
from .config import AppConfig, load_config
from .proactive import ProactiveEngine
from .service import CompanionService

logger = logging.getLogger("companion_gateway")


class AuthConfigError(ValueError):
    pass

def _resolve_token(cfg: AppConfig) -> str:
    if not cfg.api_token_env:
        return ""
    value = os.getenv(cfg.api_token_env, "")
    if not value:
        raise AuthConfigError(
            f"api_token_env is set to {cfg.api_token_env!r} but that environment variable "
            "is missing or empty. Export it or clear api_token_env to disable auth"
        )
    return value

async def _scheduler(proactive: ProactiveEngine, interval: int) -> None:
    while True:
        try:
            await asyncio.to_thread(proactive.evaluate)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("proactive evaluation failed")
        await asyncio.sleep(max(5, interval))

async def _ingest_scheduler(service: CompanionService, interval: int) -> None:
    while True:
        try:
            await asyncio.to_thread(service.catch_up_plugin)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("memory plugin ingestion catch-up failed")
        await asyncio.sleep(max(1, interval))

async def _index_backfill_scheduler(service: CompanionService, interval: int) -> None:
    while True:
        try:
            result = await asyncio.to_thread(service.memory_index_backfill)
            if result.get("error") and not result.get("cooling_down"):
                logger.warning("memory index backfill unavailable: %s", result["error"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("memory index backfill failed")
        await asyncio.sleep(max(1, interval))

def create_app(config: AppConfig | None = None) -> FastAPI:
    cfg = config or load_config()
    expected_token = _resolve_token(cfg)
    auth_enabled = bool(expected_token)
    service = CompanionService(cfg)
    proactive = ProactiveEngine(service, cfg)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        tasks = [
            asyncio.create_task(
                _scheduler(proactive, cfg.proactive.poll_interval_seconds),
                name="proactive-evaluator",
            )
        ]
        if service.memory.enabled and cfg.storage.enabled:
            tasks.append(
                asyncio.create_task(
                    _ingest_scheduler(
                        service, cfg.memory_plugin.ingest_backfill_interval_seconds
                    ),
                    name="memory-plugin-ingest",
                )
            )
        if service.memory.enabled:
            tasks.append(
                asyncio.create_task(
                    _index_backfill_scheduler(service, cfg.memory.embedding.backfill_interval_seconds),
                    name="memory-plugin-backfill",
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
        title="Companion State Gateway",
        version="0.1.0",
        description="https://github.com/Somme4096/sophia",
        lifespan=lifespan,
        docs_url=None if auth_enabled else "/docs",
        redoc_url=None if auth_enabled else "/redoc",
        openapi_url=None if auth_enabled else "/openapi.json",
        openapi_tags=[
            {"name": "health", "description": "https://github.com/Somme4096/sophia"},
            {"name": "state", "description": "https://github.com/Somme4096/sophia"},
            {"name": "proxy", "description": "https://github.com/Somme4096/sophia"},
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

    @app.get("/health", tags=["health"], response_model=_schema.HealthResponse)
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "database": service.database.integrity_check(),
            "upstream_configured": bool(cfg.upstream.base_url),
            "memory_index": service.memory_index_status(),
        }

    app.include_router(create_state_router(service, proactive, authorized))
    if cfg.api_openai.enabled:
        app.include_router(create_openai_router(cfg, service, authorized))
    return app
