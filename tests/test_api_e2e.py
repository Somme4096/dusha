from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from companion_gateway import api, api_openai
from companion_gateway.config import load_config


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _write_config(path: Path, *, data_dir: Path, enabled: object = None, token_env: str = "") -> Path:
    raw = {
        "data_dir": str(data_dir),
        "upstream": {"base_url": "https://provider.invalid/v1"},
    }
    if enabled is not None:
        raw["api_openai"] = {"enabled": enabled}
    if token_env:
        raw["api_token_env"] = token_env
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


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

    monkeypatch.setattr(api_openai, "_upstream_request", provider)
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


@pytest.mark.parametrize("enabled", [None, True])
async def test_http_config_json_enables_openai_routes(tmp_path, enabled, monkeypatch):
    async def provider(request, cfg, path, body):
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(api_openai, "_upstream_request", provider)
    config_path = _write_config(tmp_path / "config.json", data_dir=tmp_path / "data", enabled=enabled)
    loaded = load_config(config_path)
    app = api.create_app(loaded)

    async with _running_client(app) as client:
        models = await client.get("/v1/models")
        openapi = await client.get("/openapi.json")

    assert models.status_code == 200
    assert "/v1/models" in openapi.json()["paths"]
    assert "/v1/chat/completions" in openapi.json()["paths"]


async def test_http_config_json_disables_openai_but_keeps_state_health_and_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("E2E_DISABLED_TOKEN", "disabled-secret")
    config_path = _write_config(
        tmp_path / "config.json",
        data_dir=tmp_path / "data",
        enabled=False,
        token_env="E2E_DISABLED_TOKEN",
    )
    app = api.create_app(load_config(config_path))

    async with _running_client(app) as client:
        health = await client.get("/health")
        state = await client.get("/state/v1/affect", headers={"X-Companion-Token": "disabled-secret"})
        denied = await client.get("/state/v1/affect")
        models = await client.get("/v1/models")
        completions = await client.post("/v1/chat/completions", json={"messages": []})
        openapi = await client.get("/openapi.json")

    assert health.status_code == 200
    assert state.status_code == 200
    assert denied.status_code == 401
    assert models.status_code == 404
    assert completions.status_code == 404
    assert openapi.status_code == 404

    _write_config(tmp_path / "config.json", data_dir=tmp_path / "data", enabled=False)
    public_app = api.create_app(load_config(config_path))
    async with _running_client(public_app) as client:
        openapi = await client.get("/openapi.json")
    assert openapi.status_code == 200
    assert "/v1/models" not in openapi.json()["paths"]
    assert "/v1/chat/completions" not in openapi.json()["paths"]


async def test_http_config_json_reenable_after_restart(tmp_path, monkeypatch):
    async def provider(request, cfg, path, body):
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(api_openai, "_upstream_request", provider)
    config_path = _write_config(tmp_path / "config.json", data_dir=tmp_path / "data", enabled=False)
    first = api.create_app(load_config(config_path))
    async with _running_client(first) as client:
        assert (await client.get("/v1/models")).status_code == 404

    _write_config(config_path, data_dir=tmp_path / "data", enabled=True)
    second = api.create_app(load_config(config_path))
    async with _running_client(second) as client:
        assert (await client.get("/v1/models")).status_code == 200


async def test_http_proxy_messages_persist_across_restart_from_config_json(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path / "config.json", data_dir=tmp_path / "data", enabled=True)

    async def provider(request, cfg, path, body):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "persisted answer"}}]},
        )

    monkeypatch.setattr(api_openai, "_upstream_request", provider)
    first = api.create_app(load_config(config_path))
    async with _running_client(first) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test", "messages": [{"role": "user", "content": "persisted question"}]},
        )
    assert response.status_code == 200

    second = api.create_app(load_config(config_path))
    async with _running_client(second) as client:
        restored = await client.post("/state/v1/memory/search", json={"query": "persisted", "limit": 10})
        messages = []
        for result in restored.json()["results"]:
            for item in result["messages"]:
                message = await client.get(f"/state/v1/messages/{item['id']}")
                messages.append(message.json())

    assert restored.status_code == 200
    texts = {item["text"] for item in messages}
    assert "persisted question" in texts
    assert "persisted answer" in texts


def test_http_config_json_rejects_non_boolean_openai_enabled(tmp_path):
    config_path = _write_config(tmp_path / "config.json", data_dir=tmp_path / "data", enabled="true")

    with pytest.raises(ValueError, match=r"api_openai\.enabled must be bool"):
        load_config(config_path)


async def test_http_storage_disabled_returns_503_on_state_and_proxy_routes(config):
    config.storage.enabled = False
    config.upstream.base_url = "https://provider.invalid/v1"
    app = api.create_app(config)
    async with _running_client(app) as client:
        state = await client.post("/state/v1/context", json={"query": ""})
        proxy = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hello"}]},
        )
        health = await client.get("/health")
        affect = await client.get("/state/v1/affect")
    assert (state.status_code, proxy.status_code) == (503, 503)
    assert state.json() == proxy.json() == {"detail": "built-in message storage is disabled"}
    assert (health.status_code, affect.status_code) == (200, 200)


async def test_http_openapi_version_follows_the_package(config):
    from importlib import metadata

    app = api.create_app(config)
    async with _running_client(app) as client:
        document = await client.get("/openapi.json")
    assert document.json()["info"]["version"] == metadata.version("companion-state-gateway")
