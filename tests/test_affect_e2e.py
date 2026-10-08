from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest

from dusha import api
from dusha import prompts as prompts_module
from dusha.config import PromptsConfig
from dusha.timeutil import isoformat, utc_now

NEUTRAL = {
    "anxiety": 0.2, "contentment": 0.35, "dejection": 0.15, "elation": 0.2, "fatigue": 0.2, "fear": 0.0,
    "intimacy": 0.35, "irritability": 0.15, "jealousy": 0.22, "longing": 0.3, "lust": 0.3, "play": 0.25,
    "possessiveness": 0.3, "protectiveness": 0.25, "seeking": 0.25, "vitality": 0.5,
}


@asynccontextmanager
async def _running(config):
    app = api.create_app(config)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            client.app = app
            yield client


def _appraising(config, stub, increment):
    config.embedding.base_url = stub.url
    config.embedding.model = "stub"
    config.decision.increment = increment
    return config


async def _say(client, content, external_id=""):
    response = await client.post(
        "/state/v1/messages",
        json={"harness": "e2e", "conversation_id": "affect", "role": "user", "content": content,
              "external_id": external_id},
    )
    assert response.status_code == 200
    return response.json()


def _backdate(client, hours):
    past = isoformat(utc_now() - timedelta(hours=hours))
    with client.app.state.service.database.connect() as db:
        db.execute(
            "UPDATE affect_state SET last_user_message_at=?, last_updated_at=? WHERE id=1", (past, past)
        )


async def _affect(client):
    return (await client.get("/state/v1/affect")).json()


async def _description(client):
    response = await client.post("/state/v1/context", json={"query": ""})
    return response.json()["context"]["emotion"]["description"]


async def test_http_affect_starts_at_neutral_and_stays_there_without_a_decision_source(config):
    async with _running(config) as client:
        initial = await _affect(client)
        stored = await _say(client, "hello")
        after = await _affect(client)
        description = await _description(client)
    assert initial["base"] == initial["mood"] == NEUTRAL
    assert stored["affect"] is None
    assert after["base"] == pytest.approx(NEUTRAL, abs=0.001)
    assert description == "Affect is near its usual baseline."


async def test_http_appraisal_raises_one_dimension_by_the_increment_up_to_the_range(config, embedding_stub):
    embedding_stub.says["I am scared."] = "fear_general"
    async with _running(_appraising(config, embedding_stub, 0.1)) as client:
        first = await _say(client, "I am scared.", "scared-1")
        calls = embedding_stub.calls
        duplicate = await _say(client, "I am scared.", "scared-1")
        assert embedding_stub.calls == calls
        for _ in range(12):
            last = await _say(client, "I am scared.")
    assert first["affect"]["emotion"] == "fear"
    assert first["affect"]["increment"] == 0.1
    assert first["affect"]["state"]["base"]["fear"] == pytest.approx(0.1, abs=0.001)
    assert first["affect"]["state"]["base"]["anxiety"] == pytest.approx(0.2, abs=0.001)
    assert duplicate["duplicate"] is True
    assert duplicate["id"] == first["id"]
    assert duplicate["affect"] is None
    assert last["affect"]["state"]["base"]["fear"] == 1.0


async def test_http_unmatched_message_falls_back_to_the_neutral_prototype(config, embedding_stub):
    async with _running(_appraising(config, embedding_stub, 0.1)) as client:
        stored = await _say(client, "The parcel arrives Tuesday")
        state = await _affect(client)
    assert stored["affect"]["emotion"] == "contentment"
    assert state["base"]["fear"] == 0


async def test_http_embedding_outage_keeps_ingest_working_and_cools_down(config, embedding_stub):
    embedding_stub.fail = True
    async with _running(_appraising(config, embedding_stub, 0.1)) as client:
        first = await _say(client, "I am scared.")
        calls = embedding_stub.calls
        second = await _say(client, "Still scared.")
        state = await _affect(client)
    assert first["affect"] is None
    assert second["affect"] is None
    assert calls == 1
    assert embedding_stub.calls == calls
    assert state["base"]["fear"] == 0


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (5, {"longing": 0.5, "anxiety": 0.3, "seeking": 0.35, "dejection": 0.15}),
        (7, {"longing": 0.58, "anxiety": 0.34, "seeking": 0.37, "dejection": 0.22}),
    ],
)
async def test_http_silence_drifts_toward_caps_and_respects_the_gate(config, hours, expected):
    async with _running(config) as client:
        await _say(client, "hello")
        _backdate(client, hours)
        state = await _affect(client)
    assert {name: state["base"][name] for name in expected} == pytest.approx(expected, abs=0.01)


async def test_http_decision_survives_restart_then_decays_on_two_timescales(config, embedding_stub):
    embedding_stub.says["I am scared."] = "fear_general"
    _appraising(config, embedding_stub, 0.7)
    async with _running(config) as client:
        stored = await _say(client, "I am scared.")
    assert stored["affect"]["state"]["base"]["fear"] == pytest.approx(0.7, abs=0.001)
    async with _running(config) as client:
        persisted = await _affect(client)
        _backdate(client, 24)
        later = await _affect(client)
    assert persisted["base"]["fear"] == pytest.approx(0.7, abs=0.01)
    assert later["base"]["fear"] == pytest.approx(0.5047, abs=0.01)
    assert later["mood"]["fear"] == pytest.approx(0.4982, abs=0.01)
    assert later["base"]["anxiety"] == pytest.approx(0.38, abs=0.01)
    assert later["base"]["contentment"] == pytest.approx(0.35, abs=0.01)


@pytest.mark.parametrize(
    ("presentation", "expected"),
    [
        ({}, "Affect: fear is high."),
        ({"level_high": "HIGH", "prefix": "STATE: ", "suffix": "!"}, "STATE: fear is HIGH!"),
        ({"level_connector": " IS "}, "Affect: fear IS high."),
        ({"level_connector": ""}, "Affect: fearhigh."),
    ],
)
async def test_http_affect_line_follows_the_presentation_prompts(
    tmp_path, config, embedding_stub, presentation, expected
):
    snapshot = prompts_module.default_prompts()
    overlay = {"affect_presentation": {**snapshot["affect_presentation"], **presentation}}
    path = tmp_path / "prompts.json"
    path.write_text(json.dumps(overlay), encoding="utf-8")
    config.prompts = PromptsConfig(path=str(path))
    embedding_stub.says["I am scared."] = "fear_general"
    async with _running(_appraising(config, embedding_stub, 1.0)) as client:
        await _say(client, "I am scared.")
        description = await _description(client)
    assert description == expected
