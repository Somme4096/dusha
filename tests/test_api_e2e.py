from __future__ import annotations

from contextlib import asynccontextmanager

import httpx

from companion_gateway import api


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@asynccontextmanager
async def _running_client(app):
    async with app.router.lifespan_context(app):
        async with _client(app) as client:
            yield client


async def test_http_state_survives_app_restart(config):
    first = api.create_app(config)
    async with _running_client(first) as client:
        health = await client.get("/health")
        created = await client.post(
            "/state/v1/messages",
            json={
                "harness": "e2e",
                "conversation_id": "restart",
                "role": "user",
                "content": "persist this message",
            },
        )
        assert health.status_code == 200
        assert created.status_code == 200
        message_id = created.json()["id"]

    second = api.create_app(config)
    async with _running_client(second) as client:
        restored = await client.get(f"/state/v1/messages/{message_id}")
        missing = await client.get("/state/v1/messages/999999")

    assert restored.status_code == 200
    assert restored.json()["text"] == "persist this message"
    assert missing.status_code == 404
    assert missing.json()["detail"] == "message not found"


async def test_http_auth_success_and_failure_paths(config, monkeypatch):
    config.api_token_env = "E2E_COMPANION_TOKEN"
    monkeypatch.setenv("E2E_COMPANION_TOKEN", "e2e-secret")
    app = api.create_app(config)

    async with _running_client(app) as client:
        public = await client.get("/health")
        denied = await client.post("/state/v1/context", json={"query": ""})
        allowed = await client.post(
            "/state/v1/context",
            json={"query": ""},
            headers={"X-Companion-Token": "e2e-secret"},
        )
        invalid = await client.post(
            "/state/v1/messages",
            json={"role": "user", "content": ""},
            headers={"X-Companion-Token": "e2e-secret"},
        )

    assert public.status_code == 200
    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert invalid.status_code == 422


async def test_http_proxy_context_keeps_system_messages_first(config, monkeypatch):
    config.upstream.base_url = "https://provider.invalid/v1"
    captured: dict = {}

    async def provider(request, cfg, path, body):
        captured.update(body)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    monkeypatch.setattr(api, "_upstream_request", provider)
    app = api.create_app(config)
    async with _running_client(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "test",
                "messages": [
                    {"role": "system", "content": "caller system"},
                    {"role": "user", "content": "hello"},
                ],
            },
        )

    assert response.status_code == 200
    assert [message["role"] for message in captured["messages"][:2]] == ["system", "system"]
    assert captured["messages"][1]["content"].startswith("<companion_state>")
