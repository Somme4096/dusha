from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx

from companion_gateway import api
from companion_gateway.config import PromptsConfig
from companion_gateway.timeutil import isoformat, utc_now


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@asynccontextmanager
async def _running_client(app):
    async with app.router.lifespan_context(app):
        async with _client(app) as client:
            yield client


def _backdate(app, past):
    service = app.state.service
    with service.database.connect() as db:
        db.execute(
            "UPDATE affect_state SET last_user_message_at=?, last_updated_at=? WHERE id=1",
            (isoformat(past), isoformat(past)),
        )


async def _ingest(client, occurred_at, *, harness="e2e-time", conversation="conv1"):
    response = await client.post(
        "/state/v1/messages",
        json={
            "harness": harness,
            "conversation_id": conversation,
            "route": f"{harness}:{conversation}",
            "role": "user",
            "content": "hello from the past",
            "occurred_at": isoformat(occurred_at),
        },
    )
    assert response.status_code == 200
    return response.json()


async def test_http_proactive_instruction_includes_elapsed_time(config):
    config.proactive.poll_interval_seconds = 3600
    app = api.create_app(config)
    async with _running_client(app) as client:
        past = utc_now() - timedelta(hours=4, minutes=5)
        await _ingest(client, past)
        _backdate(app, past)
        evaluated = await client.post("/state/v1/proactive/evaluate")
        assert evaluated.status_code == 200
        event = evaluated.json()["event"]
        assert event is not None
        assert "{TIME}" not in event["generation_instruction"]
        assert "4 hours 5 minutes" in event["generation_instruction"]
        assert "paused, not ongoing" in event["generation_instruction"]
        assert event["silence_text"] == "4 hours 5 minutes"
        assert event["silence_minutes"] >= 240
        assert event["last_user_message_at"] is not None

        polled = await client.get(
            "/state/v1/proactive/events", params={"consumer": "e2e", "limit": 1}
        )
        assert polled.status_code == 200
        events = polled.json()["events"]
        assert len(events) == 1
        assert events[0]["id"] == event["id"]
        assert "4 hours 5 minutes" in events[0]["generation_instruction"]

    second = api.create_app(config)
    async with _running_client(second) as client:
        again = await client.post("/state/v1/proactive/evaluate")
        assert again.status_code == 200
        assert again.json()["event"] is None
        ack = await client.post(
            f"/state/v1/proactive/events/{event['id']}/ack",
            json={"consumer": "e2e", "outcome": "sent", "text": "hey, still here"},
        )
        assert ack.status_code == 200


async def test_http_proactive_evaluate_returns_null_when_silence_too_short(config):
    config.proactive.poll_interval_seconds = 3600
    app = api.create_app(config)
    async with _running_client(app) as client:
        await _ingest(client, utc_now(), conversation="fresh")
        evaluated = await client.post("/state/v1/proactive/evaluate")
        assert evaluated.status_code == 200
        assert evaluated.json()["event"] is None


async def test_http_proactive_custom_time_template_renders(tmp_path, config):
    config.proactive.poll_interval_seconds = 3600
    snapshot = api.create_app(config).state.service.prompts
    snapshot["proactive_generation_instruction"] = "After {TIME}, ping the user."
    path = tmp_path / "prompts.json"
    overlay = {k: v for k, v in snapshot.items() if k != "schema_version"}
    overlay.pop("prompts_version", None)
    path.write_text(json.dumps(overlay), encoding="utf-8")
    config.prompts = PromptsConfig(path=str(path))
    app = api.create_app(config)
    async with _running_client(app) as client:
        past = utc_now() - timedelta(hours=2)
        await _ingest(client, past, conversation="custom")
        _backdate(app, past)
        evaluated = await client.post("/state/v1/proactive/evaluate")
        assert evaluated.status_code == 200
        event = evaluated.json()["event"]
        assert event is not None
        assert event["generation_instruction"] == "After 2 hours, ping the user."
