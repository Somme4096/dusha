from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest

from dusha import api
from dusha.config import AffectConfig, PromptsConfig, load_config
from dusha.timeutil import isoformat, utc_now

CUSTOM = {
    "schema_version": 3,
    "emotion_version": "custom-1",
    "value_range": {"min": 0.0, "max": 1.0},
    "dimensions": {
        "warmth": {"neutral": 0.4, "floor": 0.1, "tau": 6, "description": "Affection toward the user"},
        "worry": {"neutral": 0.2, "floor": 0.0, "tau": 6},
        "spark": {"neutral": 0.0, "floor": 0.0, "tau": 6},
    },
    "negative_dimensions": ["worry"],
    "silence": {"rules": {"worry": {"rate_per_hour": 0.1, "cap": 0.5}}},
    "proactive_sent_deltas": {"worry": -0.1},
    "impact_scale": 2.0,
    "decision": {"increment": 0.1},
    "mood_follow_gain": {"min": 0.25, "max": 2.5, "factor": 4.0},
    "prompt": {
        "top_n": 2,
        "deviation_threshold": 0.08,
        "always_show": {"spark": 0.0},
        "level_high": 0.7,
        "level_elevated": 0.5,
    },
    "affect": {"mood_follow_hours": 12.0, "mood_return_hours": 72.0},
    "proactive": {"triggers": [{"dimension": "worry", "threshold": 0.4, "reason": "worried"}]},
}

@asynccontextmanager
async def _running_client(app):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _emotions(config, tmp_path, write_json, snapshot, name="emotions.json"):
    config.affect = AffectConfig(emotions_path=str(write_json(tmp_path / name, snapshot)))
    config.proactive.thresholds = {}
    config.proactive.poll_interval_seconds = 3600
    return config


async def _silent_since(app, client, past):
    response = await client.post(
        "/state/v1/messages",
        json={
            "harness": "e2e",
            "conversation_id": "custom",
            "route": "e2e:custom",
            "role": "user",
            "content": "hello from the past",
            "occurred_at": isoformat(past),
        },
    )
    assert response.status_code == 200
    with app.state.service.database.connect() as db:
        db.execute(
            "UPDATE affect_state SET last_user_message_at=?, last_updated_at=? WHERE id=1",
            (isoformat(past), isoformat(past)),
        )


async def test_http_renamed_dimension_set_drives_affect_context_and_proactive(
    tmp_path, config, write_json
):
    app = api.create_app(_emotions(config, tmp_path, write_json, CUSTOM))
    async with _running_client(app) as client:
        await _silent_since(app, client, utc_now() - timedelta(hours=4))
        affect = (await client.get("/state/v1/affect")).json()
        assert set(affect["base"]) == {"warmth", "worry", "spark"}
        assert affect["base"]["worry"] == pytest.approx(0.6, abs=0.01)
        context = (await client.post("/state/v1/context", json={"query": ""})).json()
        description = context["context"]["emotion"]["description"]
        assert "worry is elevated" in description
        assert "spark is noticeable" in description
        event = (await client.post("/state/v1/proactive/evaluate")).json()["event"]
        assert event["reason"] == "worried"
        assert event["ruling_feeling"]["dimension"] == "worry"
        polled = await client.get("/state/v1/proactive/events", params={"consumer": "e2e"})
        ack = await client.post(
            f"/state/v1/proactive/events/{polled.json()['events'][0]['id']}/ack",
            json={"consumer": "e2e", "outcome": "sent", "text": "are you there"},
        )
        assert ack.status_code == 200
        assert (await client.get("/state/v1/affect")).json()["base"]["worry"] < affect["base"]["worry"]

    renamed = json.loads(json.dumps(CUSTOM).replace("spark", "glow"))
    restarted = api.create_app(_emotions(config, tmp_path, write_json, renamed, "renamed.json"))
    async with _running_client(restarted) as client:
        affect = (await client.get("/state/v1/affect")).json()
        assert set(affect["base"]) == {"warmth", "worry", "glow"}
        assert affect["base"]["glow"] == 0.0
        context = await client.post("/state/v1/context", json={"query": ""})
        assert context.status_code == 200
        assert "spark" not in context.json()["context"]["emotion"]["description"]


async def test_http_proactive_stays_quiet_below_every_trigger(tmp_path, config, write_json):
    quiet = json.loads(json.dumps(CUSTOM))
    quiet["proactive"]["triggers"][0]["threshold"] = 0.9
    app = api.create_app(_emotions(config, tmp_path, write_json, quiet))
    async with _running_client(app) as client:
        await _silent_since(app, client, utc_now() - timedelta(hours=4))
        evaluated = await client.post("/state/v1/proactive/evaluate")
        assert evaluated.status_code == 200
        assert evaluated.json()["event"] is None


def test_startup_rejects_schema_2_emotions_file(tmp_path, config, write_json):
    with pytest.raises(ValueError, match="schema_version 2 is not supported"):
        api.create_app(_emotions(config, tmp_path, write_json, {**CUSTOM, "schema_version": 2}))


def _scaled(value):
    if isinstance(value, dict):
        return {key: _scaled(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scaled(item) for item in value]
    return value * 100 if isinstance(value, float) else value


async def _decide(app, emotion, now):
    return app.state.service.affect.record_user_message(
        message="x", source_message_id=None, decider=lambda **_: emotion, instruction="", now=now
    )


async def test_http_value_range_scales_the_whole_engine(tmp_path, config, write_json):
    wide = {name: _scaled(CUSTOM[name]) for name in ("dimensions", "silence", "proactive", "decision")}
    for name, params in CUSTOM["dimensions"].items():
        wide["dimensions"][name]["tau"] = params["tau"]
    wide["silence"]["rules"]["worry"] = {"rate_per_hour": 10.0, "cap": 50.0}
    wide["prompt"] = {**_scaled(CUSTOM["prompt"]), "top_n": 2}
    wide = {**CUSTOM, **wide, "value_range": {"min": 0.0, "max": 100.0}}
    wide["proactive_sent_deltas"] = {"worry": -10.0}

    async def run(snapshot, name):
        config.data_dir = tmp_path / name
        app = api.create_app(_emotions(config, tmp_path, write_json, snapshot, f"{name}.json"))
        async with _running_client(app) as client:
            past = utc_now() - timedelta(hours=4)
            await _silent_since(app, client, past)
            for _ in range(3):
                decision = await _decide(app, "warmth", past)
            affect = (await client.get("/state/v1/affect")).json()
            context = (await client.post("/state/v1/context", json={"query": ""})).json()
            event = (await client.post("/state/v1/proactive/evaluate")).json()["event"]
            polled = await client.get("/state/v1/proactive/events", params={"consumer": "e2e"})
            await client.post(
                f"/state/v1/proactive/events/{polled.json()['events'][0]['id']}/ack",
                json={"consumer": "e2e", "outcome": "sent"},
            )
            after = (await client.get("/state/v1/affect")).json()
        return decision["increment"], affect, context["context"]["emotion"]["description"], event, after

    unit_step, unit, unit_text, unit_event, unit_after = await run(CUSTOM, "unit")
    wide_step, scaled, wide_text, wide_event, wide_after = await run(wide, "wide")
    assert (unit_step, wide_step) == (0.1, 10.0)
    assert unit_text == wide_text
    assert unit_event["reason"] == wide_event["reason"] == "worried"
    for layer in ("base", "mood"):
        for name in CUSTOM["dimensions"]:
            assert scaled[layer][name] == pytest.approx(unit[layer][name] * 100, abs=0.5)
            assert wide_after[layer][name] == pytest.approx(unit_after[layer][name] * 100, abs=0.5)
    assert scaled["base"]["warmth"] > 55


async def test_http_silence_drift_never_lowers_a_raised_feeling(tmp_path, config, write_json):
    snapshot = json.loads(json.dumps(CUSTOM))
    snapshot["dimensions"]["worry"]["increment"] = 0.7
    snapshot["dimensions"]["worry"]["tau"] = 10000
    app = api.create_app(_emotions(config, tmp_path, write_json, snapshot))
    async with _running_client(app) as client:
        past = utc_now() - timedelta(minutes=2)
        await _silent_since(app, client, past)
        decision = await _decide(app, "worry", past)
        assert decision["increment"] == 0.7
        assert decision["state"]["base"]["worry"] == pytest.approx(0.9, abs=0.001)
        assert (await _decide(app, "warmth", past))["increment"] == 0.1
        affect = (await client.get("/state/v1/affect")).json()
        assert affect["base"]["worry"] == pytest.approx(0.9, abs=0.01)


def test_startup_rejects_config_increment_beyond_range(tmp_path, config, write_json):
    config.decision.increment = 2.0
    with pytest.raises(ValueError, match="decision.increment.*value_range span"):
        api.create_app(_emotions(config, tmp_path, write_json, CUSTOM))


def test_config_increment_overrides_file_default_but_not_dimension(tmp_path, config, write_json):
    snapshot = json.loads(json.dumps(CUSTOM))
    snapshot["dimensions"]["worry"]["increment"] = 0.3
    config.decision.increment = 0.2
    affect = api.create_app(_emotions(config, tmp_path, write_json, snapshot)).state.service.affect
    assert (affect.increment_for("warmth"), affect.increment_for("worry")) == (0.2, 0.3)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda s: s["silence"]["rules"].update(longing={"rate_per_hour": 0.1, "cap": 0.1}), "silence.rules"),
        (lambda s: s["prompt"].update(always_show={"fear": 0.1}), "prompt.always_show.fear"),
        (lambda s: s["proactive"]["triggers"][0].update(dimension="longing"), "proactive.triggers"),
    ],
)
def test_startup_rejects_rule_for_missing_dimension(tmp_path, config, write_json, mutate, message):
    broken = json.loads(json.dumps(CUSTOM))
    mutate(broken)
    with pytest.raises(ValueError, match=f"{message}.*unknown dimension"):
        api.create_app(_emotions(config, tmp_path, write_json, broken))


async def test_http_config_overrides_patch_silence_and_thresholds(tmp_path, write_json):
    path = write_json(tmp_path / "config.json", {
        "data_dir": "data",
        "affect": {"silence": {"longing": {"rate_per_hour": 0.2}}},
        "proactive": {"thresholds": {"fear": 0.9, "longing": 0.31}, "poll_interval_seconds": 3600},
    })
    config = load_config(path)
    app = api.create_app(config)
    triggers = app.state.service.affect.emotions["proactive"]["triggers"]
    assert [item["threshold"] for item in triggers] == [0.9, 0.31]
    async with _running_client(app) as client:
        await _silent_since(app, client, utc_now() - timedelta(hours=1))
        affect = (await client.get("/state/v1/affect")).json()
        assert affect["base"]["longing"] == pytest.approx(0.5, abs=0.01)


async def test_http_custom_framing_and_block_delimiters_render(tmp_path, config, write_json):
    overlay = {
        "proactive_framing": {
            "ruling": "FEEL {dimension}={value} from {neutral}.",
            "ladder": ["ONLY STAGE"],
            "approach": "DO {variant}",
            "context_query": "sentinel query",
        },
        "context_blocks": {
            "memo_open_delimiter": "[memo]",
            "memo_close_delimiter": "[/memo]",
            "memory_open_delimiter": "[mem]",
            "memory_close_delimiter": "[/mem]",
        },
    }
    config.prompts = PromptsConfig(path=str(write_json(tmp_path / "prompts.json", overlay)))
    config.proactive.poll_interval_seconds = 3600
    app = api.create_app(config)
    async with _running_client(app) as client:
        await _silent_since(app, client, utc_now() - timedelta(hours=4))
        assert (await client.post("/state/v1/memo/add", json={"text": "water the plant"})).status_code == 200
        context = (await client.post("/state/v1/context", json={"query": ""})).json()
        assert "[memo]" in context["injection"] and "<memo_notes>" not in context["injection"]
        event = (await client.post("/state/v1/proactive/evaluate")).json()["event"]
        lines = event["generation_instruction"].split("\n")
        assert lines[1].startswith("FEEL longing=")
        assert lines[2] == "ONLY STAGE"
        assert lines[3] == f"DO {event['generation_variant']}"
        assert event["ladder_stage"] == 0


@pytest.mark.parametrize(
    ("slot", "message"),
    [
        ({"ruling": "{bogus}", "ladder": ["a"], "approach": "", "context_query": ""}, "invalid placeholder"),
        ({"ruling": "", "ladder": [], "approach": "", "context_query": ""}, "non-empty list"),
    ],
)
def test_startup_rejects_invalid_framing(tmp_path, config, write_json, slot, message):
    config.prompts = PromptsConfig(
        path=str(write_json(tmp_path / "prompts.json", {"proactive_framing": slot}))
    )
    with pytest.raises(ValueError, match=message):
        api.create_app(config)
