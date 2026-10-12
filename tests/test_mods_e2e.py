from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from dusha import api
from dusha.config import AffectConfig, AppConfig, FeaturesConfig, ModConfig, ModsConfig, load_config

HELPER_SOURCE = '''\
def choose(emotions):
    return "fear" if "fear" in emotions else None
'''

PROBE_SOURCE = '''\
import json, os, pathlib, subprocess, sys, time

from helper import choose

CHILD = "import pathlib, sys, time\\n" \\
    "pathlib.Path(sys.argv[1]).write_text('started')\\n" \\
    "time.sleep(1.5)\\n" \\
    "pathlib.Path(sys.argv[2]).write_text('alive')\\n"


def _seen(options):
    path = pathlib.Path(options["marker"])
    return json.loads(path.read_text()) if path.exists() else {}


def _note(options, key, value):
    if "marker" not in options:
        return
    seen = _seen(options)
    seen.setdefault(key, []).append(value)
    pathlib.Path(options["marker"]).write_text(json.dumps(seen))


def start(options):
    _note(options, "start", {
        "database_path": options["database_path"],
        "cwd": os.getcwd(),
        "python": sys.executable,
        "env": {
            name: os.getenv(name)
            for name in ("PYTHONPATH", "VIRTUAL_ENV", "GATEWAY_SECRET", "SHARED_VALUE")
        },
    })


def on_message(message, options):
    if message["text"] == options.get("fail_on"):
        raise RuntimeError("configured delivery failure")
    _note(options, "messages", {
        "id": message["id"], "role": message["role"], "text": message["text"],
        "harness": message["harness"], "conversation": message["external_conversation_id"],
    })


def on_context(request, options):
    if options.get("context_fail"):
        raise RuntimeError("configured context failure")
    _note(options, "context", {
        "query": request.query, "harness": request.harness,
        "conversation_id": request.conversation_id, "max_chars": request.max_chars,
    })
    return {
        "text": options.get("context_text", "PROBE_CONTEXT"),
        "records": [{"source": "probe", "text": "PROBE_RECORD " + request.query}],
    }


def decide(request, options):
    mode = options.get("mode", "choose")
    _note(options, "decide", {
        "message": request.message, "instruction": request.instruction,
        "emotions": sorted(request.emotions), "state": request.state["base"],
    })
    if mode == "abstain":
        return None
    if mode == "unknown":
        return {"emotion": "missing"}
    if mode == "deltas":
        return {"deltas": options["deltas"]}
    if mode == "both":
        return {"emotion": choose(request.emotions), "deltas": options["deltas"]}
    if mode == "crash":
        raise RuntimeError("boom")
    if mode == "sleep_once" and len(_seen(options).get("decide", [])) == 1:
        time.sleep(options["seconds"])
    if mode == "stdout_flood":
        print("x" * 2000000)
    if mode == "descendant":
        subprocess.Popen([sys.executable, "-c", CHILD, options["started"], options["alive"]])
        while not os.path.exists(options["started"]):
            time.sleep(0.01)
        return None
    return {"emotion": choose(request.emotions)}


def tick(options):
    _note(options, "tick", time.time())
    return {"ticked": True}


def status(options):
    return {"source": "probe", "healthy": True}


def close(options):
    _note(options, "close", True)
'''


@pytest.fixture(scope="module")
def mods(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("mods-workspace")
    (root / "other").mkdir()
    (root / "pyproject.toml").write_text('[tool.uv.workspace]\nmembers = ["other"]\n', encoding="utf-8")
    suite = root / "mods" / "probe"
    suite.mkdir(parents=True)
    (suite / "README.md").write_text("probe mod\n", encoding="utf-8")
    (suite / "main.py").write_text(PROBE_SOURCE, encoding="utf-8")
    (suite / "helper.py").write_text(HELPER_SOURCE, encoding="utf-8")
    (suite / "pyproject.toml").write_text(
        '[project]\nname = "probe"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n', encoding="utf-8"
    )
    subprocess.run(["uv", "--no-config", "lock", "--directory", str(suite)], check=True)
    shutil.copytree(suite, root / "mods" / "probe_two")
    return root / "mods"


@asynccontextmanager
async def _client(config: AppConfig):
    app = api.create_app(config)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _config(tmp_path: Path, mods: Path, modules: dict[str, ModConfig] | None = None, **options) -> AppConfig:
    return AppConfig(
        data_dir=tmp_path / "data",
        affect=AffectConfig(increment=0.25),
        mods=ModsConfig(
            dir=str(mods),
            tick_interval_seconds=1,
            modules=modules if modules is not None else {"probe": ModConfig(options=options)},
        ),
    )


def _marker(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


async def _post(
    client: httpx.AsyncClient, content: str = "hello", role: str = "user", conversation: str = "one"
) -> dict:
    response = await client.post(
        "/state/v1/messages",
        json={"harness": "e2e", "conversation_id": conversation, "role": role, "content": content},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _base(client: httpx.AsyncClient) -> dict:
    response = await client.get("/state/v1/affect")
    assert response.status_code == 200, response.text
    return response.json()["base"]


async def _context(client: httpx.AsyncClient, query: str = "lantern", conversation: str = "one") -> dict:
    response = await client.post(
        "/state/v1/context", json={"harness": "e2e", "conversation_id": conversation, "query": query}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _post_expecting_no_decision(config: AppConfig) -> float:
    async with _client(config) as client:
        before = await _base(client)
        started = time.monotonic()
        stored = await _post(client)
        elapsed = time.monotonic() - started
        after = await _base(client)
    assert stored["affect"] is None
    assert after == pytest.approx(before, abs=1e-3)
    return elapsed


def _cli(config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    return subprocess.run(
        [sys.executable, "-m", "dusha.cli", "--config", str(config), *args],
        capture_output=True, text=True, env=env, check=False,
    )


async def test_http_mod_sees_every_hook_with_the_documented_arguments(tmp_path, mods, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "gateway-path")
    monkeypatch.setenv("VIRTUAL_ENV", "gateway-venv")
    marker = tmp_path / "seen.json"
    async with _client(_config(tmp_path, mods, marker=str(marker))) as client:
        before = await _base(client)
        stored = await _post(client, "hello")
        await _post(client, "sure thing", role="assistant")
        await _post(client, "tool output", role="tool", conversation="two")
        context = await _context(client, "lantern")
        health = await client.get("/health")
        status = await client.get("/state/v1/mods")
    assert stored["affect"]["emotion"] == "fear"
    assert stored["affect"]["increment"] == pytest.approx(0.25)
    assert stored["affect"]["state"]["base"]["fear"] == pytest.approx(before["fear"] + 0.25, abs=1e-3)

    block = context["context"]["blocks"]["probe"]
    assert block["source"] == "probe" and block["text"] == "PROBE_CONTEXT"
    assert block["records"] == [{"source": "probe", "text": "PROBE_RECORD lantern"}]
    assert "<mod_context>" in context["injection"] and "PROBE_RECORD lantern" in context["injection"]
    assert context["injection"].index("<mod_context>") < context["injection"].index("<affect>")
    assert "memory_id" not in json.dumps(block)

    assert health.json()["mods"] == ["probe"]
    assert status.json() == {
        "probe": {
            "loaded": True,
            "exports": ["close", "decide", "on_context", "on_message", "start", "status", "tick"],
            "error": "",
            "status": {"source": "probe", "healthy": True},
        }
    }

    seen = _marker(marker)
    [start] = seen["start"]
    assert start["database_path"].endswith("state.sqlite3")
    assert start["cwd"] == str(mods / "probe")
    assert start["python"].startswith(str(mods / "probe" / ".venv"))
    assert start["env"]["PYTHONPATH"] is None and start["env"]["VIRTUAL_ENV"] is None
    assert [(item["role"], item["text"], item["conversation"]) for item in seen["messages"]] == [
        ("user", "hello", "one"), ("assistant", "sure thing", "one"), ("tool", "tool output", "two"),
    ]
    assert [item["id"] for item in seen["messages"]] == sorted(item["id"] for item in seen["messages"])
    assert seen["context"] == [
        {"query": "lantern", "harness": "e2e", "conversation_id": "one", "max_chars": 3000}
    ]
    [decision] = seen["decide"]
    assert decision["message"] == "hello" and decision["instruction"].strip()
    assert set(decision["emotions"]) == set(before) and set(decision["state"]) == set(before)
    assert seen["close"] == [True]


async def test_http_delivery_cursor_replays_a_failed_message_in_order(tmp_path, mods):
    marker = tmp_path / "seen.json"
    config = _config(tmp_path, mods, marker=str(marker), fail_on="second")
    async with _client(config) as client:
        await _post(client, "first")
        await _post(client, "second")
        await _post(client, "third")
    assert [item["text"] for item in _marker(marker)["messages"]] == ["first"]

    async with _client(_config(tmp_path, mods, marker=str(marker))) as client:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(_marker(marker).get("messages", [])) < 3:
            await asyncio.sleep(0.1)
        await _post(client, "fourth")
    seen = _marker(marker)
    assert [item["text"] for item in seen["messages"]] == ["first", "second", "third", "fourth"]
    assert seen["tick"]


async def test_http_context_block_respects_its_cap_and_falls_back_on_failure(tmp_path, mods):
    marker = tmp_path / "seen.json"
    capped = {"probe": ModConfig(context_max_chars=500, options={"context_text": "x" * 4000})}
    async with _client(_config(tmp_path, mods, capped)) as client:
        await _post(client, "VAULT_RECENT_MARKER")
        fact = await client.post("/state/v1/evergreen/facts", json={"key": "vault.key", "text": "VAULT_FACT"})
        assert fact.status_code == 200
        context = await _context(client)
    block = context["context"]["blocks"]["probe"]
    assert len(block["text"]) == 500 and "records" not in block
    assert "VAULT_FACT" in context["injection"] and "VAULT_RECENT_MARKER" in context["injection"]

    failing = {"probe": ModConfig(options={"context_fail": True})}
    async with _client(_config(tmp_path, mods, failing)) as client:
        context = await _context(client)
    assert "probe" not in context["context"]["blocks"]
    assert "VAULT_RECENT_MARKER" in context["injection"]

    silent = {"probe": ModConfig(context_max_chars=0, options={"marker": str(marker)})}
    async with _client(_config(tmp_path, mods, silent)) as client:
        context = await _context(client)
    assert "probe" not in context["context"]["blocks"]
    assert "context" not in _marker(marker)


@pytest.mark.parametrize(
    ("mode", "logged"),
    [
        ("abstain", None),
        ("unknown", "decision returned invalid emotion: 'missing'"),
        ("crash", "probe decide failed: RuntimeError: boom"),
        ("stdout_flood", "probe decide failed: RuntimeError: mod output limit exceeded"),
    ],
)
async def test_http_decision_failures_leave_affect_unchanged(tmp_path, mods, caplog, mode, logged):
    caplog.set_level(logging.WARNING, logger="dusha")
    await _post_expecting_no_decision(_config(tmp_path, mods, mode=mode))
    if logged is None:
        assert "probe" not in caplog.text
    else:
        assert logged in caplog.text
    if mode == "crash":
        assert "probe stderr: RuntimeError: boom" in caplog.text


async def test_http_deltas_apply_after_the_decision_sees_the_earlier_state(tmp_path, mods, caplog):
    caplog.set_level(logging.WARNING, logger="dusha")
    marker = tmp_path / "seen.json"
    deltas = {"elation": 0.3, "bogus": 0.1}
    async with _client(_config(tmp_path, mods, marker=str(marker), mode="deltas", deltas=deltas)) as client:
        before = await _base(client)
        stored = await _post(client, "I love lanterns")
        after = await _base(client)
    assert stored["affect"] is None
    assert after["elation"] > before["elation"]
    assert "decision deltas name unknown emotion dimension(s): ['bogus']" in caplog.text

    async with _client(_config(tmp_path, mods, marker=str(marker), mode="both", deltas=deltas)) as client:
        before = await _base(client)
        stored = await _post(client, "I love lanterns again")
    assert stored["affect"]["emotion"] == "fear"
    assert stored["affect"]["state"]["base"]["elation"] > before["elation"]
    assert _marker(marker)["decide"][-1]["state"]["elation"] == pytest.approx(before["elation"], abs=1e-3)


async def test_http_two_mods_run_in_order_first_emotion_wins_and_deltas_merge(tmp_path, mods):
    modules = {
        "probe_two": ModConfig(options={"mode": "deltas", "deltas": {"elation": 0.3}}),
        "probe": ModConfig(options={"mode": "choose"}),
    }
    async with _client(_config(tmp_path, mods, modules)) as client:
        before = await _base(client)
        stored = await _post(client)
        context = await _context(client)
        health = await client.get("/health")
    assert stored["affect"]["emotion"] == "fear"
    assert stored["affect"]["state"]["base"]["elation"] > before["elation"]
    assert list(context["context"]["blocks"]) == ["probe_two", "probe", "affect"]
    assert health.json()["mods"] == ["probe_two", "probe"]


async def test_http_timeout_stops_the_mod_and_the_next_call_restarts_it(tmp_path, mods, caplog):
    caplog.set_level(logging.WARNING, logger="dusha")
    marker = tmp_path / "seen.json"
    options = {"marker": str(marker), "mode": "sleep_once", "seconds": 30}
    modules = {"probe": ModConfig(timeout_seconds=1.0, options=options)}
    async with _client(_config(tmp_path, mods, modules)) as client:
        started = time.monotonic()
        first = await _post(client, "slow")
        elapsed = time.monotonic() - started
        stopped = await client.get("/state/v1/mods")
        second = await _post(client, "quick")
        running = await client.get("/state/v1/mods")
    assert first["affect"] is None and elapsed < 10
    assert stopped.json()["probe"]["loaded"] is False
    assert "probe decide failed: mod timed out" in caplog.text
    assert "probe is not running, restarting" in caplog.text
    assert second["affect"]["emotion"] == "fear"
    assert running.json()["probe"]["loaded"] is True
    assert len(_marker(marker)["start"]) == 2


async def test_http_descendant_process_is_stopped_after_close(tmp_path, mods):
    started, alive = tmp_path / "started", tmp_path / "alive"
    config = _config(tmp_path, mods, mode="descendant", started=str(started), alive=str(alive))
    await _post_expecting_no_decision(config)
    assert started.exists()
    await asyncio.sleep(2.0)
    assert not alive.exists()


@pytest.mark.parametrize(
    ("passthrough", "expected"),
    [
        ([], {"GATEWAY_SECRET": None, "SHARED_VALUE": None}),
        (["SHARED_VALUE"], {"GATEWAY_SECRET": None, "SHARED_VALUE": "shared"}),
    ],
)
async def test_http_mod_sees_only_the_passthrough_environment(
    tmp_path, mods, monkeypatch, passthrough, expected
):
    monkeypatch.setenv("GATEWAY_SECRET", "secret")
    monkeypatch.setenv("SHARED_VALUE", "shared")
    marker = tmp_path / "seen.json"
    modules = {"probe": ModConfig(env_passthrough=passthrough, options={"marker": str(marker)})}
    async with _client(_config(tmp_path, mods, modules)) as client:
        stored = await _post(client)
    assert stored["affect"]["emotion"] == "fear"
    env = _marker(marker)["start"][0]["env"]
    assert {name: env[name] for name in expected} == expected


@pytest.mark.parametrize(
    ("name", "logged"),
    [
        ("missing", "invalid mod: missing or escaping README.md"),
        ("escaping", "invalid mod: missing or escaping uv.lock"),
        ("absent", "mod not found"),
    ],
)
async def test_http_invalid_mod_directory_is_reported_and_the_gateway_keeps_running(
    tmp_path, caplog, name, logged
):
    caplog.set_level(logging.WARNING, logger="dusha")
    broken = tmp_path / "mods"
    (broken / "missing").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    suite = broken / "escaping"
    suite.mkdir()
    (suite / "README.md").write_text("suite\n", encoding="utf-8")
    (suite / "main.py").write_text("def decide(request, options):\n    return {'emotion': 'fear'}\n")
    (suite / "pyproject.toml").write_text("[project]\nname='x'\nversion='0.1.0'\n", encoding="utf-8")
    (suite / "uv.lock").symlink_to(outside / "uv.lock")
    config = _config(tmp_path, broken, {name: ModConfig()})
    async with _client(config) as client:
        stored = await _post(client)
        status = await client.get("/state/v1/mods")
    assert stored["affect"] is None
    assert f"failed to load mod {name!r}" in caplog.text and logged in caplog.text
    report = status.json()[name]
    assert report["loaded"] is False and report["exports"] == [] and logged in report["error"]


async def test_http_decide_mod_is_idle_when_affect_is_off(tmp_path, mods, caplog):
    caplog.set_level(logging.WARNING, logger="dusha")
    marker = tmp_path / "seen.json"
    config = _config(tmp_path, mods, marker=str(marker))
    config.features = FeaturesConfig(affect=False, proactive=False)
    async with _client(config) as client:
        stored = await _post(client)
        affect = await client.get("/state/v1/affect")
    assert stored["affect"] is None and affect.status_code == 503
    seen = _marker(marker)
    assert "decide" not in seen and len(seen["messages"]) == 1
    assert "probe" not in caplog.text


def test_cli_mods_status_reads_the_relative_mods_dir(tmp_path, mods):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({
            "data_dir": str(tmp_path / "data"),
            "mods": {"dir": os.path.relpath(mods, tmp_path), "modules": {"probe": {"options": {}}}},
        }),
        encoding="utf-8",
    )
    result = _cli(config_path, "mods", "status")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)["probe"]
    assert report["loaded"] is True and report["status"] == {"source": "probe", "healthy": True}
    reindex = _cli(config_path, "memory", "reindex")
    assert reindex.returncode == 0, reindex.stderr
    assert json.loads(reindex.stdout) == {"status": "rebuilt"}


def test_config_rejects_bad_mod_names_fields_and_env_passthrough(tmp_path):
    with pytest.raises(ValueError, match="plain directory name"):
        ModsConfig(modules={"../outside": ModConfig()})
    with pytest.raises(ValueError, match="mod.env_passthrough"):
        ModConfig(env_passthrough=["OK", ""])
    with pytest.raises(ValueError, match="mod.timeout_seconds must be above 0"):
        ModConfig(timeout_seconds=0)
    for raw, message in [
        ({"mods": {"modules": {"probe": {"bogus": 1}}}}, r"unknown field\(s\) under mods.modules.probe"),
        ({"mods": {"modules": {"probe": 1}}}, "mods.modules.probe must be an object"),
        ({"mods": {"tick_interval_seconds": 0}}, "mods.tick_interval_seconds must be at least 1"),
        ({"memory_plugin": {"module": "x"}}, r"unknown top-level field\(s\)"),
        ({"decision": {"module": "x"}}, r"unknown top-level field\(s\)"),
    ]:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"data_dir": str(tmp_path / "data"), **raw}), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            load_config(path)
