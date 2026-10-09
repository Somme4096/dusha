from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from dusha import api, emotions
from dusha import prompts as prompts_module
from dusha.config import load_config


def _start(tmp_path, raw, *, text=None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    path = home / "config.json"
    path.write_text(text if text is not None else json.dumps({"data_dir": "data", **raw}), encoding="utf-8")
    return api.create_app(load_config(path))


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _emotions_file(tmp_path, mutate=None, name="emotions.json", version="custom"):
    snapshot = emotions.default_emotions()
    snapshot["emotion_version"] = version
    if mutate:
        mutate(snapshot)
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    return path


def _prompts_file(tmp_path, mutate, name="prompts.json"):
    snapshot = prompts_module.default_prompts()
    snapshot.pop("schema_version")
    snapshot.pop("prompts_version")
    mutate(snapshot)
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("body", "pattern"),
    [
        ('{"host": "a", "host": "b"}', "duplicate key"),
        ('{"host": "a", "nonsense": 1}', "unknown top-level field"),
        ('{"memory": {"bogus": 1}}', r"unknown field\(s\) under memory"),
        ('{"memory": {"retrieval_mode": "lexical"}}', r"unknown field\(s\) under memory"),
        ('{"affect": {"bogus": 1}}', r"unknown field\(s\) under affect"),
        ('{"emotions": {"bogus": 1}}', r"unknown field\(s\) under emotions"),
        ('{"upstream": {"timeout_seconds": NaN}}', "non-finite"),
        ('{"host": "a" // jsonc comment\n}', "invalid JSON"),
        ('{"port": "abc"}', "port must be int"),
        ('{"proactive": {"enabled": "yes"}}', "enabled must be bool"),
        ('{"memory": {"recent_messages": true}}', "recent_messages must be int"),
        ('{"decision": {"increment": -1}}', "decision.increment"),
        ('{"decision": {"increment": 0}}', "decision.increment"),
        ('{"decision": {"increment": "fast"}}', "increment"),
        ('{"decision": {"bogus": 1}}', r"unknown field\(s\) under decision"),
        ('{"affect": {"emotions_path": "x.json"}}', "Use emotions.path"),
        ('{"affect": {"mood_follow_hours": 0}}', "mood_follow_hours must be positive"),
        ('{"affect": {"dimensions": {"bogus": {"neutral": 0.1}}}}', "unknown dimension"),
        ('{"affect": {"dimensions": {"fear": {"neutral": 0.1, "bogus": 2}}}}', "unknown field"),
        ('{"proactive": {"thresholds": {"fear": 5.0}}}', "within value_range"),
        ('{"proactive": {"thresholds": {"play": 0.5}}}', "without a proactive trigger"),
        ('{"proactive": {"quiet_start_hour": 99}}', "proactive.quiet_start_hour must be at least 0"),
        ('{"proactive": {"poll_interval_seconds": -5}}', "proactive.poll_interval_seconds"),
        ('{"evergreen": {"max_items": -3}}', "evergreen.max_items"),
        ('{"upstream": {"timeout_seconds": 0}}', "upstream.timeout_seconds must be above 0"),
        ('{"upstream": {"allowed_hosts": "*"}}', "upstream.allowed_hosts must be an array"),
        ('{"upstream": {"allowed_hosts": [1]}}', "upstream.allowed_hosts must be an array of host patterns"),
        ('{"memo": {"text_max_chars": 0}}', "memo.text_max_chars"),
        ('{"port": 70000}', "port must be"),
        ('{"timezone": "Not/AZone"}', "timezone is not a known IANA zone"),
    ],
)
def test_startup_rejects_invalid_config(tmp_path, body, pattern):
    with pytest.raises(ValueError, match=pattern):
        _start(tmp_path, None, text=body)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda s: s.update(schema_version=99), "schema_version"),
        (lambda s: s.update(emotion_version=""), "emotion_version"),
        (lambda s: s["dimensions"]["fear"].update(floor=0.5), "floor must be <="),
        (lambda s: s["dimensions"]["fear"].update(tau=0), "tau must be positive"),
        (lambda s: s["dimensions"]["fear"].update(neutral=True), "finite number"),
        (lambda s: s["negative_dimensions"].append("bogus"), "contains unknown label"),
        (lambda s: s["silence"]["rules"]["dejection"].update(rate_per_hour=-1), "must not be negative"),
        (lambda s: s["silence"]["rules"].update(bogus={"rate_per_hour": 1, "cap": 1}), "unknown dimension"),
        (lambda s: s["silence"]["rules"]["longing"].update(bogus=1), "unknown field"),
        (lambda s: s["affect"].update(bogus=0.04), "unknown field"),
        (lambda s: s["prompt"].update(always_show={"bogus": 0.1}), "unknown dimension"),
        (lambda s: s["proactive"]["triggers"][0].update(dimension="bogus"), "unknown dimension"),
        (lambda s: s["proactive"]["triggers"][0].update(reason=""), "non-empty string"),
        (lambda s: s["appraisal"].update(fallback_label="bogus"), "unknown prototype"),
        (lambda s: s["appraisal"].update(min_similarity=1), "min_similarity"),
        (lambda s: s["appraisal"]["prototypes"]["cold"]["deltas"].update(bogus=0.1), "unknown dimension"),
        (lambda s: s["appraisal"]["prototypes"]["cold"].update(text=" "), "non-empty string"),
        (lambda s: s["affect"].update(mood_follow_hours=0), "mood_follow_hours must be positive"),
        (lambda s: s["prompt"].update(top_n=0), "top_n must be positive"),
        (lambda s: s["prompt"].update(level_high=0.4, level_elevated=0.6), "level_high"),
        (lambda s: s["proactive"]["triggers"][1].update(threshold=5.0), "within value_range"),
        (lambda s: s.update(value_range={"min": 1.0, "max": 1.0}), "must be below value_range.max"),
        (lambda s: s["decision"].update(increment=1.5), "value_range span"),
        (lambda s: s["dimensions"]["fear"].update(increment=0), "value_range span"),
        (lambda s: s.pop("decision"), "decision must be an object"),
        (lambda s: s.update(bogus_section={}), "unknown emotions field"),
    ],
)
def test_startup_rejects_invalid_emotions_file(tmp_path, mutate, message):
    path = _emotions_file(tmp_path, mutate)
    with pytest.raises(ValueError, match=message):
        _start(tmp_path, {"emotions": {"path": str(path)}})


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('{"emotion_version": "a", "emotion_version": "b"}', "duplicate key"),
        ('{"schema_version": 3, "emotion_version": "x", "impact_scale": NaN}', "non-finite"),
        (None, "emotions file not found"),
    ],
)
def test_startup_rejects_unreadable_emotions_file(tmp_path, text, message):
    path = tmp_path / "emotions.json"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        _start(tmp_path, {"emotions": {"path": str(path)}})


@pytest.mark.parametrize(
    ("mutate", "pattern"),
    [
        (lambda s: s.update(bogus_slot={}), "unknown prompts slot"),
        (lambda s: s["companion_state"].update(bogus_key="x"), "unknown field"),
        (lambda s: s.update(companion_state={"affect_instruction": "only this"}), "incomplete"),
        (lambda s: s.update(decision_instruction=5), "must be a string"),
        (lambda s: s.update(proactive_generation_instruction=5), "must be an object"),
        (lambda s: s["proactive_generation_instruction"].update(variants=[]), "must be a non-empty list"),
        (lambda s: s["proactive_generation_instruction"].update(variants=["  "]), "non-empty string"),
    ],
)
def test_startup_rejects_malformed_prompts_overlay(tmp_path, mutate, pattern):
    path = _prompts_file(tmp_path, mutate)
    with pytest.raises(ValueError, match=pattern):
        _start(tmp_path, {"prompts": {"path": str(path)}})


async def test_http_example_config_starts_and_serves(tmp_path):
    raw = json.loads((Path(__file__).parents[1] / "config.example.json").read_text(encoding="utf-8"))
    app = _start(tmp_path, raw)
    async with _client(app) as client:
        health = await client.get("/health")
        context = await client.post("/state/v1/context", json={"query": ""})
    assert health.json()["status"] == "ok"
    assert health.json()["upstream_configured"] is False
    assert context.status_code == 200


@pytest.mark.parametrize("absolute", [False, True])
async def test_http_config_paths_resolve_beside_the_config_file_or_stay_absolute(tmp_path, absolute):
    base = tmp_path / "elsewhere" if absolute else tmp_path / "home"
    base.mkdir()
    (base / "identity.md").write_text("IDENTITY_SENTINEL", encoding="utf-8")
    _emotions_file(base, lambda s: s["dimensions"]["fear"].update(neutral=0.25))
    _prompts_file(base, lambda s: s["companion_state"].update(affect_instruction="PROMPT_SENTINEL"))

    def located(name):
        return str(base / name) if absolute else name

    app = _start(tmp_path, {
        "data_dir": located("store"),
        "identity_prompt": {"path": located("identity.md")},
        "emotions": {"path": located("emotions.json")},
        "prompts": {"path": located("prompts.json")},
    })
    async with _client(app) as client:
        injection = (await client.post("/state/v1/context", json={"query": ""})).json()["injection"]
        affect = (await client.get("/state/v1/affect")).json()
    assert injection.startswith("IDENTITY_SENTINEL")
    assert "PROMPT_SENTINEL" in injection
    assert affect["base"]["fear"] == 0.25
    assert (base / "store" / "state.sqlite3").is_file()


async def test_http_config_override_beats_the_custom_emotions_file(tmp_path):
    path = _emotions_file(
        tmp_path, lambda s: s["dimensions"]["fear"].update(neutral=0.25), version="0.2.0-custom"
    )
    pinned = {"path": str(path), "expected_version": "0.2.0-custom"}
    with pytest.raises(ValueError, match="version mismatch"):
        _start(tmp_path, {"emotions": {"path": str(path), "expected_version": "other"}})
    async with _client(_start(tmp_path, {"emotions": pinned})) as client:
        from_file = (await client.get("/state/v1/affect")).json()
    patch = {"dimensions": {"fear": {"neutral": 0.05}}}
    override = {"data_dir": "overridden", "emotions": pinned, "affect": patch}
    async with _client(_start(tmp_path, override)) as client:
        overridden = (await client.get("/state/v1/affect")).json()
    assert from_file["base"]["fear"] == 0.25
    assert overridden["base"]["fear"] == 0.05


@pytest.mark.parametrize("version", [4, 6])
async def test_startup_rejects_a_database_from_another_schema_version(tmp_path, version):
    import sqlite3

    async with _client(_start(tmp_path, {})) as client:
        assert (await client.get("/health")).json()["database"] == "ok"
    with sqlite3.connect(tmp_path / "home" / "data" / "state.sqlite3") as db:
        db.execute(f"PRAGMA user_version={version}")
    with pytest.raises(RuntimeError, match=f"schema version {version} is incompatible"):
        _start(tmp_path, {})
