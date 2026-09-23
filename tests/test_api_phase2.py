"""Phase 2: context API shape, auth, budget mapping, provider independence."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from companion_gateway import api
from companion_gateway.config import AppConfig, IdentityPromptConfig, MemoryConfig

NOW = datetime(2026, 9, 6, 3, 0, tzinfo=UTC)

IDENTITY_SENTINEL = "# Identity SENTINEL\nRaw user-authored identity text."


async def _run_inline(function, /, *args, **kwargs):
    return function(*args, **kwargs)


def _cfg(
    tmp_path,
    *,
    memory: MemoryConfig | None = None,
    identity_prompt: IdentityPromptConfig | None = None,
) -> AppConfig:
    return AppConfig(
        data_dir=tmp_path / "data",
        timezone="Asia/Taipei",
        memory=memory
        or MemoryConfig(
            recent_messages=2, search_hits=4, context_messages=1, injection_max_chars=20_000
        ),
        identity_prompt=identity_prompt or IdentityPromptConfig(),
    )


async def test_api_context_returns_structured_fields_and_injection(config, monkeypatch):
    monkeypatch.setattr(api.asyncio, "to_thread", _run_inline)
    app = api.create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/state/v1/context",
            json={"harness": "api", "conversation_id": "one", "query": ""},
        )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"injection", "affect", "evergreen_facts", "records", "search_hits", "context"}
    context = body["context"]
    assert context["version"] == 1
    assert context["identity"] == {"configured": False, "text": "", "revision": ""}
    assert context["memory"]["session"] == body["records"]
    assert context["emotion"]["values"]["base"] == body["affect"]["base"]
    assert context["emotion"]["fingerprints"]["prompts"].startswith("sha256:")
    assert "<companion_state>" in body["injection"]


async def test_api_context_requires_same_state_auth(config, monkeypatch):
    config.api_token_env = "TEST_TOKEN_ENV"
    monkeypatch.setenv("TEST_TOKEN_ENV", "secret-token")
    monkeypatch.setattr(api.asyncio, "to_thread", _run_inline)
    app = api.create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post(
            "/state/v1/context", json={"query": ""}
        )
        allowed = await client.post(
            "/state/v1/context",
            json={"query": ""},
            headers={"X-Companion-Token": "secret-token"},
        )
    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["context"]["identity"]["configured"] is False


async def test_api_context_budget_overflow_maps_to_422(tmp_path, monkeypatch):
    identity_path = tmp_path / "identity.md"
    identity_path.write_text(IDENTITY_SENTINEL, encoding="utf-8")
    small = MemoryConfig(
        recent_messages=2, search_hits=4, context_messages=1, injection_max_chars=64
    )
    cfg = _cfg(
        tmp_path,
        memory=small,
        identity_prompt=IdentityPromptConfig(path=str(identity_path)),
    )
    monkeypatch.setattr(api.asyncio, "to_thread", _run_inline)
    app = api.create_app(cfg)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/state/v1/context", json={"query": ""})
    assert response.status_code == 422
    assert "too small for mandatory identity and companion" in response.json()["detail"]


async def test_api_context_works_without_upstream_credentials(config, monkeypatch):
    config.upstream.base_url = ""
    monkeypatch.setattr(api.asyncio, "to_thread", _run_inline)
    app = api.create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/state/v1/context", json={"query": "anything"}
        )
    assert response.status_code == 200
    assert "context" in response.json()