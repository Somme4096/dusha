from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from dusha import api
from dusha.timeutil import isoformat, utc_now

ROUTE = "discord-main:FriendMessage:123"


@asynccontextmanager
async def _running(config):
    config.proactive.poll_interval_seconds = 3600
    app = api.create_app(config)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            client.app = app
            yield client


async def _say(client, content="I will be away for a while."):
    response = await client.post(
        "/state/v1/messages",
        json={"harness": "astrbot", "conversation_id": ROUTE, "route": ROUTE, "role": "user",
              "content": content},
    )
    assert response.status_code == 200
    return response.json()


async def _silent_for(client, hours):
    await _say(client)
    past = isoformat(utc_now() - timedelta(hours=hours))
    with client.app.state.service.database.connect() as db:
        db.execute(
            "UPDATE affect_state SET last_user_message_at=?, last_updated_at=? WHERE id=1", (past, past)
        )


async def _evaluate(client):
    response = await client.post("/state/v1/proactive/evaluate")
    assert response.status_code == 200
    return response.json()["event"]


async def _poll(client, consumer="astrbot"):
    response = await client.get(
        "/state/v1/proactive/events", params={"consumer": consumer, "harness": "astrbot", "limit": 1}
    )
    assert response.status_code == 200
    return [event["id"] for event in response.json()["events"]]


def _ack(client, event_id, outcome, consumer="astrbot", **extra):
    return client.post(
        f"/state/v1/proactive/events/{event_id}/ack", json={"consumer": consumer, "outcome": outcome, **extra}
    )


async def test_http_event_is_leased_once_survives_restart_and_starts_the_cooldown(config):
    async with _running(config) as client:
        await _silent_for(client, 5)
        event = await _evaluate(client)
        assert event["reason"] == "silence"
        assert event["target"]["route"] == ROUTE
        assert await _evaluate(client) is None
        assert await _poll(client) == [event["id"]]
    async with _running(config) as client:
        assert await _poll(client, consumer="other") == []
        stolen = await _ack(client, event["id"], "sent", consumer="other", text="Not mine.")
        assert stolen.status_code == 409
        longing_before = (await client.get("/state/v1/affect")).json()["base"]["longing"]
        sent = await _ack(client, event["id"], "sent", text="Checking in.")
        assert sent.status_code == 200
        assert await _evaluate(client) is None
        state = (await client.get("/state/v1/affect")).json()
        search = await client.post("/state/v1/memory/search", json={"query": "Checking"})
    assert state["unanswered_proactive"] == 1
    assert state["base"]["longing"] < longing_before
    archived = [message for hit in search.json()["results"] for message in hit["messages"]]
    assert ("assistant", "Checking in.") in [(item["role"], item["text"]) for item in archived]


async def test_http_user_reply_cancels_the_unsent_event_and_resets_the_unanswered_count(config):
    async with _running(config) as client:
        await _silent_for(client, 5)
        first = await _evaluate(client)
        await _poll(client)
        await _ack(client, first["id"], "sent", text="Checking in.")
        assert (await client.get("/state/v1/affect")).json()["unanswered_proactive"] == 1
        await _say(client, "I am back.")
        state = (await client.get("/state/v1/affect")).json()
    assert state["unanswered_proactive"] == 0


async def test_http_quiet_hours_block_a_due_event(config):
    hour = datetime.now(ZoneInfo(config.timezone)).hour
    config.proactive.quiet_start_hour = (hour - 1) % 24
    config.proactive.quiet_end_hour = (hour + 2) % 24
    async with _running(config) as client:
        await _silent_for(client, 5)
        assert await _evaluate(client) is None


async def test_http_failed_delivery_waits_for_the_retry_delay(tmp_path, config):
    async with _running(config) as client:
        await _silent_for(client, 5)
        event = await _evaluate(client)
        assert await _poll(client) == [event["id"]]
        assert (await _ack(client, event["id"], "failed", error="network")).status_code == 200
        assert await _poll(client) == []

    config.data_dir = tmp_path / "no-delay"
    config.proactive.retry_delay_minutes = 0
    async with _running(config) as client:
        await _silent_for(client, 5)
        event = await _evaluate(client)
        await _poll(client)
        await _ack(client, event["id"], "failed", error="network")
        assert await _poll(client) == [event["id"]]
        assert (await _ack(client, event["id"], "release")).status_code == 200
        assert await _poll(client) == [event["id"]]


async def test_http_raised_fear_wins_over_silence_as_the_reason(config, embedding_stub):
    config.embedding.base_url = embedding_stub.url
    config.embedding.model = "stub"
    config.decision.increment = 1.0
    embedding_stub.says["I will be away for a while."] = "fear_general"
    async with _running(config) as client:
        await _silent_for(client, 4)
        event = await _evaluate(client)
    assert event["reason"] == "fear"
    assert event["ruling_feeling"]["dimension"] == "fear"
