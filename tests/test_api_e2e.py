from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx

from dusha import api
from dusha.config import load_config


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
    config.api_token = "e2e-secret"
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


async def test_http_openapi_version_follows_the_package(config):
    from importlib import metadata

    app = api.create_app(config)
    async with _running_client(app) as client:
        document = await client.get("/openapi.json")
    assert document.json()["info"]["version"] == metadata.version("dusha")


async def test_http_memo_limits_follow_config_and_default_data_dir_sits_beside_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    path = home / "config.json"
    path.write_text(
        json.dumps({"plugins": {"memo": {"max_items": 1, "text_max_chars": 12, "reason_max_chars": 5}}}),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.data_dir == home / "data"
    app = api.create_app(config)
    async with _running_client(app) as client:
        long = await client.post("/state/v1/memo/add", json={"text": "thirteen char"})
        first = await client.post("/state/v1/memo/add", json={"text": "first memo"})
        second = await client.post("/state/v1/memo/add", json={"text": "second memo"})
        context = await client.post("/state/v1/context", json={"query": ""})
        wordy = await client.post(
            f"/state/v1/memo/{first.json()['memo']['id']}/done", json={"reason": "too long"}
        )
    assert (long.status_code, first.status_code, second.status_code) == (422, 200, 200)
    assert "exceeds 12 characters" in long.json()["detail"]
    assert "first memo" in context.json()["injection"]
    assert "second memo" not in context.json()["injection"]
    assert wordy.status_code == 422
    assert not (elsewhere / "data").exists()


async def test_http_health_reports_the_companion_it_serves(tmp_path, config):
    home = tmp_path / "dusha" / "luna"
    home.mkdir(parents=True)
    (home / "config.json").write_text("{}", encoding="utf-8")
    async with _running_client(api.create_app(load_config(home=home))) as client:
        named = await client.get("/health")
    async with _running_client(api.create_app(config)) as client:
        unnamed = await client.get("/health")
    assert named.json()["companion"] == "luna"
    assert unnamed.json()["companion"] == ""
