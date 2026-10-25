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
        ('{"plugins": {"memory": {"bogus": 1}}}', r"unknown field\(s\) under plugins.memory"),
        ('{"plugins": {"memory": {"retrieval_mode": "x"}}}', r"unknown field\(s\) under plugins.memory"),
        ('{"plugins": {"affect": {"bogus": 1}}}', r"unknown field\(s\) under plugins.affect"),
        ('{"plugins": {"affect": {"emotions": {"bogus": 1}}}}', r"under plugins.affect.emotions"),
        ('{"embedding": {"timeout_seconds": NaN}}', "non-finite"),
        ('{"host": "a" // jsonc comment\n}', "invalid JSON"),
        ('{"port": "abc"}', "port must be int"),
        ('{"plugins": {"proactive": {"disabled": "yes"}}}', "plugins.proactive.disabled must be bool"),
        ('{"plugins": {"memory": {"recent_messages": true}}}', "recent_messages must be int"),
        ('{"plugins": {"affect": {"increment": -1}}}', "plugins.affect.increment"),
        ('{"plugins": {"affect": {"increment": 0}}}', "plugins.affect.increment"),
        ('{"plugins": {"affect": {"increment": "fast"}}}', "increment"),
        ('{"mods": {"bogus": 1}}', r"unknown top-level field\(s\)"),
        ('{"plugins": {"affect": {"emotions_path": "x.json"}}}', "Use plugins.affect.emotions.path"),
        ('{"plugins": {"affect": {"mood_follow_hours": 0}}}', "mood_follow_hours must be positive"),
        ('{"plugins": {"affect": {"dimensions": {"bogus": {"neutral": 0.1}}}}}', "unknown dimension"),
        ('{"plugins": {"affect": {"dimensions": {"fear": {"neutral": 0.1, "bogus": 2}}}}}', "unknown field"),
        ('{"plugins": {"proactive": {"thresholds": {"fear": 5.0}}}}', "within value_range"),
        ('{"plugins": {"proactive": {"thresholds": {"play": 0.5}}}}', "without a proactive trigger"),
        ('{"plugins": {"proactive": {"quiet_start_hour": 99}}}', "plugins.proactive.quiet_start_hour"),
        ('{"plugins": {"proactive": {"poll_interval_seconds": -5}}}', "plugins.proactive.poll_interval"),
        ('{"plugins": {"evergreen": {"max_items": -3}}}', "plugins.evergreen.max_items"),
        ('{"embedding": {"timeout_seconds": 0}}', "embedding.timeout_seconds must be above 0"),
        ('{"embedding": {"allowed_hosts": ["*"]}}', r"unknown field\(s\) under embedding"),
        ('{"upstream": {"base_url": "https://a.invalid"}}', r"\['upstream'\] moved to proxy\.upstreams"),
        ('{"api_openai": {"enabled": true}}', r"\['api_openai'\] moved to proxy\.upstreams"),
        ('{"api_token_env": "COMPANION_TOKEN"}', "unknown top-level field"),
        ('{"api_token": 42}', "api_token must be str"),
        ('{"plugins": {"memo": {"text_max_chars": 0}}}', "plugins.memo.text_max_chars"),
        ('{"port": 70000}', "port must be"),
        ('{"timezone": "Not/AZone"}', "timezone is not a known IANA zone"),
        ('{"features": {"affect": false}}', r"unknown top-level field\(s\)"),
        ('{"plugins": {"identity": {"path": ""}}}', "plugins.identity.path is empty"),
        ('{"plugins": {"identity": {"bogus": 1}}}', r"unknown field\(s\) under plugins.identity"),
        ('{"plugins": "all"}', "plugins must be an object"),
        ('{"storage": {"enabled": true}}', "unknown top-level field"),
    ],
)
def test_startup_rejects_invalid_config(tmp_path, body, pattern):
    with pytest.raises(ValueError, match=pattern):
        _start(tmp_path, None, text=body)


@pytest.mark.parametrize(
    ("raw", "present", "expected"),
    [
        ({}, True, "HOME_IDENTITY"),
        ({}, False, ""),
        ({"plugins": {"identity": {"disabled": True}}}, True, ""),
        ({"plugins": {"identity": {"disabled": True, "path": ""}}}, True, ""),
        ({"plugins": {"identity": {"path": "named.md"}}}, True, "NAMED_IDENTITY"),
    ],
)
async def test_identity_defaults_to_identity_md_in_the_companion_home(tmp_path, raw, present, expected):
    home = tmp_path / "home"
    home.mkdir()
    (home / "named.md").write_text("NAMED_IDENTITY", encoding="utf-8")
    if present:
        (home / "identity.md").write_text("HOME_IDENTITY", encoding="utf-8")
    async with _client(_start(tmp_path, raw)) as client:
        context = (await client.post("/state/v1/context", json={"query": ""})).json()["context"]
    assert context["blocks"].get("identity", {}).get("text", "") == expected


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
        _start(tmp_path, {"plugins": {"affect": {"emotions": {"path": str(path)}}}})


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
        _start(tmp_path, {"plugins": {"affect": {"emotions": {"path": str(path)}}}})


@pytest.mark.parametrize(
    ("mutate", "pattern"),
    [
        (lambda s: s.update(bogus_slot={}), "unknown prompts slot"),
        (lambda s: s["companion_state"].update(bogus_key="x"), "unknown field"),
        (lambda s: s.update(companion_state={"memory_instruction": "only this"}), "incomplete"),
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
    assert context.status_code == 200


@pytest.mark.parametrize("absolute", [False, True])
async def test_http_config_paths_resolve_beside_the_config_file_or_stay_absolute(tmp_path, absolute):
    base = tmp_path / "elsewhere" if absolute else tmp_path / "home"
    base.mkdir()
    (base / "identity.md").write_text("IDENTITY_SENTINEL", encoding="utf-8")
    _emotions_file(base, lambda s: s["dimensions"]["fear"].update(neutral=0.25))
    _prompts_file(base, lambda s: s["affect"].update(instruction="PROMPT_SENTINEL"))

    def located(name):
        return str(base / name) if absolute else name

    app = _start(tmp_path, {
        "data_dir": located("store"),
        "plugins": {
            "identity": {"path": located("identity.md")},
            "affect": {"emotions": {"path": located("emotions.json")}},
        },
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
        mismatched = {"path": str(path), "expected_version": "other"}
        _start(tmp_path, {"plugins": {"affect": {"emotions": mismatched}}})
    async with _client(_start(tmp_path, {"plugins": {"affect": {"emotions": pinned}}})) as client:
        from_file = (await client.get("/state/v1/affect")).json()
    patch = {"dimensions": {"fear": {"neutral": 0.05}}}
    override = {"data_dir": "overridden", "plugins": {"affect": {"emotions": pinned, **patch}}}
    async with _client(_start(tmp_path, override)) as client:
        overridden = (await client.get("/state/v1/affect")).json()
    assert from_file["base"]["fear"] == 0.25
    assert overridden["base"]["fear"] == 0.05


@pytest.mark.parametrize("version", [4, 7])
async def test_startup_rejects_a_database_from_another_schema_version(tmp_path, version):
    import sqlite3

    async with _client(_start(tmp_path, {})) as client:
        assert (await client.get("/health")).json()["database"] == "ok"
    with sqlite3.connect(tmp_path / "home" / "data" / "state.sqlite3") as db:
        db.execute(f"PRAGMA user_version={version}")
    with pytest.raises(RuntimeError, match=f"schema version {version} is incompatible"):
        _start(tmp_path, {})


async def test_startup_moves_a_schema_5_database_off_its_sha256_column(tmp_path):
    import sqlite3

    message = {"harness": "e2e", "conversation_id": "old", "role": "user", "content": "Kept across upgrade."}
    async with _client(_start(tmp_path, {})) as client:
        first = (await client.post("/state/v1/messages", json=message)).json()["id"]
    path = tmp_path / "home" / "data" / "state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE messages ADD COLUMN sha256 TEXT NOT NULL DEFAULT ''")
        db.execute("PRAGMA user_version=5")
    async with _client(_start(tmp_path, {})) as client:
        kept = await client.get(f"/state/v1/messages/{first}")
        added = await client.post("/state/v1/messages", json={**message, "content": "Stored after."})
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        version = db.execute("PRAGMA user_version").fetchone()[0]
    assert kept.json()["text"] == "Kept across upgrade."
    assert added.status_code == 200
    assert "sha256" not in columns and version == 6
